"""Build a static STAC catalog from the models in ``models/``.

One STAC Item describes one model directory (one major model version), using
the STAC Machine Learning Model (MLM) extension. The catalog is generated, not
hand-maintained: ``config.json`` supplies the display name, description and
publication, the rendered ``process_graph.json`` supplies the executable
reference and the weights URL, and ``stac.json`` supplies the MLM fields that
cannot be derived from either (architecture, input/output structure, tasks).

The generated tree is::

    catalog.json
    collections/fair-geo-fm-models/collection.json
    collections/fair-geo-fm-models/items/<model>.json

Every asset URL is absolute and points at the publication target: the staging
site for a branch render (``https://.../main``) or a Zenodo record's file base
for a release (``https://zenodo.org/records/<id>/files``). The catalog itself
uses relative links, so it can be served from either location unchanged.

This module is imported by ``scripts/build-stac.py`` and
``scripts/publish-release.py``; it is not a standalone script.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from process_graphs import model_config, render, validate_model_config

STAC_VERSION = "1.0.0"
MLM_SCHEMA = "https://stac-extensions.github.io/mlm/v1.6.0/schema.json"
EO_SCHEMA = "https://stac-extensions.github.io/eo/v1.1.0/schema.json"
PROCESSING_SCHEMA = "https://stac-extensions.github.io/processing/v1.3.0/schema.json"
SCIENTIFIC_SCHEMA = "https://stac-extensions.github.io/scientific/v1.0.0/schema.json"
VERSION_SCHEMA = "https://stac-extensions.github.io/version/v1.2.0/schema.json"
ITEM_ASSETS_SCHEMA = "https://stac-extensions.github.io/item-assets/v1.0.0/schema.json"

CATALOG_ID = "fair-geo-fm"
COLLECTION_ID = "fair-geo-fm-models"
COLLECTION_TITLE = "FAIR geospatial foundation models"
COLLECTION_DESCRIPTION = (
    "Geospatial foundation models published by the NFDI4Earth FAIR geospatial "
    "foundation models project, ready to use through openEO user-defined "
    "processes. Each Item describes one major model version and links its "
    "weights, process graph, UDF, inference runtime and documentation."
)
DEFAULT_LICENSE = "CC0-1.0"
MODEL_ROLES = ["mlm:model", "mlm:weights", "data"]
REQUIRED_MLM_PROPERTIES = (
    "mlm:name",
    "mlm:architecture",
    "mlm:tasks",
    "mlm:input",
    "mlm:output",
)


def load_json(path: Path) -> dict:
    """Read a JSON object, with a readable error instead of a traceback."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"{path} not found") from None
    except json.JSONDecodeError as error:
        raise SystemExit(f"{path}: invalid JSON: {error}") from None
    if not isinstance(value, dict):
        raise SystemExit(f"{path}: expected a JSON object")
    return value


def find_weights_url(node: Any) -> str | None:
    """Find the first ``weights_url`` URL in a rendered process graph.

    TESSERA v2 downloads its checkpoint at runtime from a URL passed through
    ``run_udf``'s ``context``. Searching the graph avoids duplicating that URL
    in ``stac.json``; an explicit ``weights.href`` there still wins.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"weights_url", "weights_uri", "model_url"}:
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    return value
            found = find_weights_url(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_weights_url(value)
            if found:
                return found
    return None


def bbox_geometry(bbox: list[float]) -> dict:
    """A rectangular Polygon for a 2-D bounding box."""
    west, south, east, north = bbox
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [west, south],
                [east, south],
                [east, north],
                [west, north],
                [west, south],
            ]
        ],
    }


def _absolute(href: str) -> bool:
    return href.startswith(("http://", "https://"))


def _item(
    process_dir: Path,
    *,
    base_url: str,
    docs_base_url: str,
    version: str,
    doi: str | None,
    datetime_value: str | None,
    source_archive_key: str | None,
    zenodo_url: str | None,
    rendered: dict[str, tuple[str, dict, str, str]] | None,
) -> tuple[dict, dict]:
    """Build one model Item, plus the metadata needed for Collection summaries."""
    model = process_dir.name
    config = model_config(process_dir)
    stac_path = process_dir / "stac.json"
    if not stac_path.is_file():
        raise SystemExit(
            f"{stac_path} is required; it supplies the MLM fields that cannot "
            "be derived from config.json or process_graph.json"
        )
    stac = load_json(stac_path)

    if rendered and model in rendered:
        _text, graph, graph_url, archive_url = rendered[model]
    else:
        _text, graph, graph_url, archive_url = render(
            process_dir, base_url, version=version, zenodo_url=zenodo_url
        )

    runtime = config.get("runtime", "")
    udf_url = f"{base_url}/models/{model}/udf.py"
    docs_url = f"{docs_base_url}/models/{model}/"

    weights = stac.get("weights") or {}
    weights_href = weights.get("href") or find_weights_url(graph)
    if not weights_href:
        raise SystemExit(
            f"{stac_path}: no weights URL found; add 'weights.href' or pass "
            "the checkpoint URL through the process graph's context"
        )
    # The graph's runtime URL carries #feature_deps, which tells openEO where
    # to extract the archive. The STAC asset points at the archive itself.
    runtime_href = archive_url.split("#", 1)[0]
    weights_asset: dict[str, Any] = {
        "href": weights_href,
        "type": weights.get(
            "type", "application/octet-stream; framework=pytorch"
        ),
        "title": weights.get("title", f"{config['name']} weights"),
        "description": weights.get(
            "description", "Checkpoint used for inference by the process graph."
        ),
        "roles": MODEL_ROLES,
        "mlm:artifact_type": weights.get("mlm:artifact_type", "torch.save"),
    }

    properties: dict[str, Any] = {
        "title": stac.get("title", config["name"]),
        "description": config["description"],
        "datetime": datetime_value or stac.get("datetime"),
        "version": version,
        "mlm:name": config["name"],
    }
    properties.update(stac.get("properties") or {})

    # MLM requires every band named in mlm:input to have a definition in the
    # Item. The eo extension supplies that; put it on the model asset, as in
    # the MLM example item.
    input_bands: list[str] = []
    model_inputs = properties.get("mlm:input")
    if isinstance(model_inputs, list):
        for model_input in model_inputs:
            for band in (model_input or {}).get("bands") or []:
                name = band if isinstance(band, str) else (band or {}).get("name")
                if isinstance(name, str) and name and name not in input_bands:
                    input_bands.append(name)
    if input_bands:
        weights_asset["eo:bands"] = [{"name": name} for name in input_bands]

    processing = stac.get("processing") or {}
    lineage = processing.get("lineage") or (
        f"Inference is implemented as the openEO user-defined process "
        f"{graph.get('id', model)}."
    )
    properties["processing:lineage"] = lineage
    properties["processing:expression"] = {
        "format": "openeo-process-graph",
        "expression": graph_url,
        "description": "openEO process graph that produces the embeddings.",
    }
    if isinstance(processing.get("software"), dict) and processing["software"]:
        properties["processing:software"] = processing["software"]

    publication = config.get("publication") or {}
    if isinstance(publication.get("citation"), str) and publication["citation"].strip():
        properties["sci:citation"] = publication["citation"]
    if isinstance(stac.get("publications"), list) and stac["publications"]:
        properties["sci:publications"] = stac["publications"]
    if doi:
        properties["sci:doi"] = doi

    bbox = stac.get("bbox") or [-180.0, -90.0, 180.0, 90.0]
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise SystemExit(
            f"{stac_path}: bbox must be a 2-D [west, south, east, north] array"
        )
    item_id = stac.get("id") or model

    assets: dict[str, Any] = {
        "model": weights_asset,
        "process_graph": {
            "href": graph_url,
            "type": "application/json",
            "title": f"{config['name']} process graph",
            "description": (
                "openEO user-defined process. Pass this URL as the "
                "`namespace` of a `datacube_from_process` call."
            ),
            "roles": ["process", "metadata"],
        },
        "udf": {
            "href": udf_url,
            "type": "text/x-python",
            "title": f"{config['name']} UDF",
            "description": "User-defined function executed by the process graph.",
            "roles": ["code"],
        },
        "runtime": {
            "href": runtime_href,
            "type": "application/zip",
            "title": f"{runtime} inference runtime",
            "description": (
                "Python dependency archive supplied through the openEO "
                "`udf-dependency-archives` job option."
            ),
            "roles": ["mlm:inference-runtime", "runtime"],
        },
        "documentation": {
            "href": docs_url,
            "type": "text/html",
            "title": f"{config['name']} documentation",
            "roles": ["metadata"],
        },
    }
    if source_archive_key:
        assets["source_code"] = {
            "href": f"{base_url}/{source_archive_key.lstrip('/')}",
            "type": "application/zip",
            "title": "Complete source tree of the release",
            "roles": ["mlm:source_code", "code"],
        }

    links: list[dict[str, Any]] = [
        {
            "rel": "self",
            "href": f"./{item_id}.json",
            "type": "application/geo+json",
        },
        {
            "rel": "root",
            "href": "../../../catalog.json",
            "type": "application/json",
        },
        {
            "rel": "collection",
            "href": "../collection.json",
            "type": "application/json",
        },
        {
            "rel": "processing-expression",
            "href": graph_url,
            "type": "application/json",
            "title": "openEO process graph",
        },
        {
            "rel": "related",
            "href": docs_url,
            "type": "text/html",
            "title": "Model documentation",
        },
    ]
    if doi:
        links.append(
            {
                "rel": "cite-as",
                "href": f"https://doi.org/{doi}",
                "type": "text/html",
                "title": "Release DOI",
            }
        )

    stac_extensions = [
        MLM_SCHEMA,
        PROCESSING_SCHEMA,
        SCIENTIFIC_SCHEMA,
        VERSION_SCHEMA,
    ]
    if input_bands:
        stac_extensions.append(EO_SCHEMA)

    item = {
        "type": "Feature",
        "stac_version": STAC_VERSION,
        "stac_extensions": stac_extensions,
        "id": item_id,
        "collection": COLLECTION_ID,
        "bbox": bbox,
        "geometry": bbox_geometry(bbox),
        "properties": properties,
        "links": links,
        "assets": assets,
    }
    summary = {
        "mlm:name": config["name"],
        "mlm:tasks": properties.get("mlm:tasks", []),
        "mlm:framework": properties.get("mlm:framework"),
        "version": version,
        "license": stac.get("license"),
        "keywords": stac.get("keywords", []),
    }
    return item, summary


def _collection(items: list[dict], summaries: list[dict]) -> dict:
    """The single models Collection, with asset templates and summaries."""
    tasks = sorted(
        {task for summary in summaries for task in summary.get("mlm:tasks", [])}
    )
    frameworks = sorted(
        {
            summary["mlm:framework"]
            for summary in summaries
            if summary.get("mlm:framework")
        }
    )
    licenses = {
        summary["license"] for summary in summaries if summary.get("license")
    }
    license_value = (
        licenses.pop()
        if len(licenses) == 1
        else ("various" if licenses else DEFAULT_LICENSE)
    )
    keywords = sorted(
        {"geospatial foundation models", "Earth observation", "embeddings", "openEO"}
        | {
            keyword
            for summary in summaries
            for keyword in summary.get("keywords", [])
            if isinstance(keyword, str) and keyword
        }
    )
    collection: dict[str, Any] = {
        "type": "Collection",
        "stac_version": STAC_VERSION,
        "stac_extensions": [ITEM_ASSETS_SCHEMA, MLM_SCHEMA],
        "id": COLLECTION_ID,
        "title": COLLECTION_TITLE,
        "description": COLLECTION_DESCRIPTION,
        "license": license_value,
        "keywords": keywords,
        "extent": {
            "spatial": {"bbox": [[-180.0, -90.0, 180.0, 90.0]]},
            "temporal": {
                "interval": [["2015-01-01T00:00:00Z", None]]
            },
        },
        "links": [
            {
                "rel": "self",
                "href": "./collection.json",
                "type": "application/json",
            },
            {
                "rel": "root",
                "href": "../../catalog.json",
                "type": "application/json",
            },
            *[
                {
                    "rel": "item",
                    "href": f"./items/{item['id']}.json",
                    "type": "application/geo+json",
                    "title": item["properties"]["title"],
                }
                for item in items
            ],
        ],
        "item_assets": {
            "model": {
                "type": "application/octet-stream; framework=pytorch",
                "title": "Model weights or checkpoint",
                "roles": MODEL_ROLES,
            },
            "process_graph": {
                "type": "application/json",
                "title": "openEO process graph",
                "roles": ["process", "metadata"],
            },
            "udf": {
                "type": "text/x-python",
                "title": "User-defined function",
                "roles": ["code"],
            },
            "runtime": {
                "type": "application/zip",
                "title": "Inference runtime",
                "roles": ["mlm:inference-runtime", "runtime"],
            },
            "documentation": {
                "type": "text/html",
                "title": "Model documentation",
                "roles": ["metadata"],
            },
        },
        "summaries": {
            "mlm:name": sorted({summary["mlm:name"] for summary in summaries}),
            "mlm:tasks": tasks,
            "version": sorted({summary["version"] for summary in summaries}),
        },
    }
    if frameworks:
        collection["summaries"]["mlm:framework"] = frameworks
    return collection


def _catalog() -> dict:
    return {
        "type": "Catalog",
        "stac_version": STAC_VERSION,
        "id": CATALOG_ID,
        "title": COLLECTION_TITLE,
        "description": COLLECTION_DESCRIPTION,
        "links": [
            {
                "rel": "self",
                "href": "./catalog.json",
                "type": "application/json",
            },
            {
                "rel": "root",
                "href": "./catalog.json",
                "type": "application/json",
            },
            {
                "rel": "child",
                "href": f"./collections/{COLLECTION_ID}/collection.json",
                "type": "application/json",
                "title": COLLECTION_TITLE,
            },
        ],
    }


def validate(files: dict[str, dict]) -> list[str]:
    """Check the generated catalog for the errors that make it unusable.

    This is intentionally a structural check, not a full JSON Schema
    validation: the build runs offline, while the extension schemas live on the
    web. It covers the fields consumers and the MLM contract rely on.
    """
    problems: list[str] = []
    items: dict[str, dict] = {}
    collection_path = f"collections/{COLLECTION_ID}/collection.json"

    for path, document in files.items():
        if not path.startswith(f"collections/{COLLECTION_ID}/items/"):
            continue
        item_id = document.get("id", path)
        if item_id in items:
            problems.append(f"{path}: duplicate Item id {item_id!r}")
        items[item_id] = document

        if document.get("collection") != COLLECTION_ID:
            problems.append(f"{path}: collection is not {COLLECTION_ID!r}")
        if not document.get("geometry"):
            problems.append(f"{path}: geometry is required")
        bbox = document.get("bbox")
        if not isinstance(bbox, list) or len(bbox) not in (4, 6):
            problems.append(f"{path}: bbox must have 4 or 6 numbers")
        properties = document.get("properties") or {}
        if not properties.get("datetime"):
            problems.append(f"{path}: properties.datetime is required")
        for key in REQUIRED_MLM_PROPERTIES:
            if key not in properties:
                problems.append(f"{path}: properties.{key} is required")
        tasks = properties.get("mlm:tasks")
        if not isinstance(tasks, list) or not tasks:
            problems.append(f"{path}: mlm:tasks must be a non-empty array")
        inputs = properties.get("mlm:input")
        if not isinstance(inputs, list) or not inputs:
            problems.append(f"{path}: mlm:input must be a non-empty array")
        else:
            for index, model_input in enumerate(inputs):
                structure = (model_input or {}).get("input") or {}
                for key in ("shape", "dim_order", "data_type"):
                    if key not in structure:
                        problems.append(
                            f"{path}: mlm:input[{index}].input.{key} is required"
                        )
        outputs = properties.get("mlm:output")
        if not isinstance(outputs, list) or not outputs:
            problems.append(f"{path}: mlm:output must be a non-empty array")
        else:
            for index, model_output in enumerate(outputs):
                if not (model_output or {}).get("tasks"):
                    problems.append(
                        f"{path}: mlm:output[{index}].tasks is required"
                    )
                result = (model_output or {}).get("result") or {}
                for key in ("shape", "dim_order", "data_type"):
                    if key not in result:
                        problems.append(
                            f"{path}: mlm:output[{index}].result.{key} is required"
                        )

        assets = document.get("assets") or {}
        model_asset = assets.get("model") or {}
        if "mlm:model" not in (model_asset.get("roles") or []):
            problems.append(f"{path}: assets.model needs the mlm:model role")
        if not model_asset.get("mlm:artifact_type"):
            problems.append(
                f"{path}: assets.model.mlm:artifact_type is required"
            )
        for name, asset in assets.items():
            href = asset.get("href", "")
            if not _absolute(href):
                problems.append(
                    f"{path}: assets.{name}.href must be an absolute URL, "
                    f"got {href!r}"
                )
        for name in ("process_graph", "udf", "runtime"):
            if name not in assets:
                problems.append(f"{path}: assets.{name} is required")

        declared_bands: list[str] = []
        if isinstance(inputs, list):
            for model_input in inputs:
                bands = (model_input or {}).get("bands") or []
                for band in bands:
                    name = (
                        band if isinstance(band, str) else (band or {}).get("name")
                    )
                    if name and name not in declared_bands:
                        declared_bands.append(name)
                if bands:
                    dim_order = ((model_input or {}).get("input") or {}).get(
                        "dim_order", []
                    )
                    if "bands" not in dim_order:
                        problems.append(
                            f"{path}: mlm:input.input.dim_order must include "
                            "'bands' when mlm:input.bands is non-empty"
                        )
        if declared_bands:
            if EO_SCHEMA not in (document.get("stac_extensions") or []):
                problems.append(
                    f"{path}: stac_extensions must include {EO_SCHEMA} for "
                    "mlm:input bands"
                )
            eo_band_names = {
                band.get("name")
                for band in model_asset.get("eo:bands", [])
                if isinstance(band, dict)
            }
            for band in declared_bands:
                if band not in eo_band_names:
                    problems.append(
                        f"{path}: assets.model.eo:bands lacks {band!r} "
                        "referenced by mlm:input"
                    )

        if MLM_SCHEMA not in (document.get("stac_extensions") or []):
            problems.append(f"{path}: stac_extensions must include {MLM_SCHEMA}")

    collection = files.get(collection_path)
    if collection is None:
        problems.append(f"missing {collection_path}")
    else:
        item_links = {
            link.get("href")
            for link in collection.get("links", [])
            if link.get("rel") == "item"
        }
        for item_id in items:
            href = f"./items/{item_id}.json"
            if href not in item_links:
                problems.append(
                    f"{collection_path}: missing item link {href}"
                )
        if not collection.get("license"):
            problems.append(f"{collection_path}: license is required")
        if not collection.get("extent"):
            problems.append(f"{collection_path}: extent is required")

    if not items:
        problems.append("catalog contains no Items")
    for required in ("catalog.json", collection_path):
        if required not in files:
            problems.append(f"missing {required}")
    return problems


def build_catalog(
    models_root: Path,
    *,
    base_url: str,
    docs_base_url: str | None = None,
    version: str = "main",
    doi: str | None = None,
    datetime_value: str | None = None,
    source_archive_key: str | None = None,
    zenodo_url: str | None = None,
    rendered: dict[str, tuple[str, dict, str, str]] | None = None,
) -> dict[str, dict]:
    """Return the catalog as ``{relative path: JSON document}``.

    ``base_url`` locates the published model files (process graph, UDF,
    runtime); ``docs_base_url`` locates their documentation pages. They differ
    on a release: the files live on Zenodo while the documentation stays on the
    versioned Pages site. ``rendered`` avoids rendering a graph twice when the
    caller already did it (``publish-release.py``).
    """
    base_url = base_url.rstrip("/")
    docs_base_url = (docs_base_url or base_url).rstrip("/")
    models_root = models_root.resolve()

    items: list[dict] = []
    summaries: list[dict] = []
    problems: list[str] = []

    for process_dir in sorted(path for path in models_root.iterdir() if path.is_dir()):
        if not (process_dir / "process_graph.json.j2").is_file():
            continue
        problems.extend(validate_model_config(process_dir))
        if not (process_dir / "udf.py").is_file():
            problems.append(
                f"{process_dir}/udf.py is required; the STAC Item links it"
            )
        item, summary = _item(
            process_dir,
            base_url=base_url,
            docs_base_url=docs_base_url,
            version=version,
            doi=doi,
            datetime_value=datetime_value,
            source_archive_key=source_archive_key,
            zenodo_url=zenodo_url,
            rendered=rendered,
        )
        items.append(item)
        summaries.append(summary)

    if problems:
        raise SystemExit("\n".join(problems))

    files: dict[str, dict] = {"catalog.json": _catalog()}
    collection_path = f"collections/{COLLECTION_ID}/collection.json"
    files[collection_path] = _collection(items, summaries)
    for item in items:
        files[f"collections/{COLLECTION_ID}/items/{item['id']}.json"] = item

    problems = validate(files)
    if problems:
        raise SystemExit("\n".join(problems))
    return files


def write_catalog(files: dict[str, dict], output_dir: Path) -> list[Path]:
    """Write the catalog files below ``output_dir`` and return their paths."""
    written: list[Path] = []
    for relative, document in files.items():
        path = output_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written
