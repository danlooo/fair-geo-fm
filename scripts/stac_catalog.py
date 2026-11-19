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
for a release (``https://zenodo.org/records/<id>/files``). By default the
catalog's own links are relative, so it can be served from either location
unchanged. A release passes ``link_href`` to make them absolute, because
browser-based clients cannot fetch Zenodo's plain file route (no CORS header)
and must follow the API's ``/content`` URLs instead.

This module is imported by ``scripts/build-stac.py`` and
``scripts/publish-release.py``; it is not a standalone script.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

# Maps a document path relative to the STAC root (e.g.
# ``collections/fair-geo-fm-models/items/tessera_v2.json``) to the absolute
# URL it is published at. Callers use it when relative links cannot be fetched,
# such as Zenodo releases browsed by STAC Browser (see ``publish-release.py``).
LinkHref = Callable[[str], str]

from process_graphs import model_config, model_tags, render, validate_model_config

STAC_VERSION = "1.0.0"
MLM_SCHEMA = "https://stac-extensions.github.io/mlm/v1.6.0/schema.json"
EO_SCHEMA = "https://stac-extensions.github.io/eo/v1.1.0/schema.json"
CUBE_SCHEMA = "https://stac-extensions.github.io/datacube/v2.3.0/schema.json"
PROCESSING_SCHEMA = "https://stac-extensions.github.io/processing/v1.3.0/schema.json"
SCIENTIFIC_SCHEMA = "https://stac-extensions.github.io/scientific/v1.0.0/schema.json"
VERSION_SCHEMA = "https://stac-extensions.github.io/version/v1.2.0/schema.json"
ITEM_ASSETS_SCHEMA = "https://stac-extensions.github.io/item-assets/v1.0.0/schema.json"

# Fields that no consulted extension defines are published under the project
# prefix. They carry the values shown on the model page so a STAC client sees
# the same facts, but they are not covered by a schema yet.
PREFIX = "fairgeofm:"

# Common temporal resolutions mapped to the ISO 8601 duration the datacube
# extension expects in ``cube:dimensions.<name>.step``.
TEMPORAL_STEPS = {
    "annual": "P1Y",
    "yearly": "P1Y",
    "quarterly": "P3M",
    "monthly": "P1M",
    "weekly": "P1W",
    "daily": "P1D",
}
RESOLUTION_METERS = re.compile(
    r"^\s*(?P<value>[0-9]+(?:\.[0-9]+)?)\s*m(?:etres?|eters?)?\s*$",
    re.IGNORECASE,
)
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

CATALOG_ID = "fair-geo-fm"
COLLECTION_ID = "fair-geo-fm-models"
COLLECTION_TITLE = "FAIR geospatial foundation models"
COLLECTION_DESCRIPTION = (
    "Geospatial foundation models published by the NFDI4Earth FAIR geospatial "
    "foundation models project, ready to use through openEO user-defined "
    "processes. Each Item describes one major model version and links its "
    "process graph and, where the model has them, its weights, UDF, inference "
    "runtime and documentation."
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

    A model can download its checkpoint at runtime from a URL passed through
    ``run_udf``'s ``context``. Searching the graph avoids duplicating that URL
    in ``stac.json``; an explicit ``weights.href`` there still wins, and models
    that load precomputed embeddings have neither.
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


def rfc3339_day_bound(value: str, *, end: bool) -> str:
    """Widen a date-only config timecode to an RFC 3339 instant.

    ``config.json`` stores coverage as calendar dates; STAC wants RFC 3339.
    A full timestamp passes through unchanged.
    """
    if DATE_ONLY.match(value):
        return f"{value}T{'23:59:59' if end else '00:00:00'}Z"
    return value


def spatial_resolution_meters(value: object) -> float | None:
    """The numeric ground sample distance of a string such as ``"10 m"``."""
    if not isinstance(value, str):
        return None
    match = RESOLUTION_METERS.match(value)
    return float(match["value"]) if match else None


def temporal_step(value: object) -> str | None:
    """An ISO 8601 duration for ``cube:dimensions``, or None when unknown."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.lower() in TEMPORAL_STEPS:
        return TEMPORAL_STEPS[text.lower()]
    if re.fullmatch(r"P(?=\d)[0-9YMWD]+", text, re.IGNORECASE):
        return text.upper()
    return None


def _absolute(href: str) -> bool:
    return href.startswith(("http://", "https://"))


def _stac_href(relative: str, path: str, link_href: LinkHref | None) -> str:
    """Resolve a catalog-relative link, absolutely when the caller asks."""
    return link_href(path) if link_href else relative


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
    link_href: LinkHref | None = None,
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

    # A model that loads precomputed embeddings has no checkpoint to link. The
    # weights are optional; only describe a model asset when there is one.
    weights = stac.get("weights") or {}
    weights_href = weights.get("href") or find_weights_url(graph)
    # The graph's runtime URL carries #feature_deps, which tells openEO where
    # to extract the archive. The STAC asset points at the archive itself.
    runtime_href = archive_url.split("#", 1)[0]
    weights_asset: dict[str, Any] | None = None
    if weights_href:
        weights_asset = {
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

    # Emit the coverage and model properties the documentation page shows.
    # Core, EO and datacube fields are used where they exist; the remaining
    # values are namespaced with the project prefix until an extension
    # (e.g. the proposed ``emb:`` one) defines them.
    temporal = config.get("temporal_extent")
    cube_dimensions: dict[str, Any] | None = None
    if (
        isinstance(temporal, list)
        and len(temporal) == 2
        and all(isinstance(value, str) and value for value in temporal)
    ):
        start = rfc3339_day_bound(temporal[0], end=False)
        end = rfc3339_day_bound(temporal[1], end=True)
        properties["start_datetime"] = start
        properties["end_datetime"] = end
        dimension: dict[str, Any] = {"type": "temporal", "extent": [start, end]}
        step = temporal_step(config.get("temporal_resolution"))
        if step:
            dimension["step"] = step
        cube_dimensions = {"t": dimension}
        properties["cube:dimensions"] = cube_dimensions

    gsd = spatial_resolution_meters(config.get("spatial_resolution"))
    if gsd is not None:
        properties["gsd"] = gsd

    properties[f"{PREFIX}type"] = config["type"]
    properties[f"{PREFIX}input_sources"] = config["input_sources"]
    properties[f"{PREFIX}dimensions"] = config["dimensions"]
    properties[f"{PREFIX}spatial_resolution"] = config["spatial_resolution"]
    properties[f"{PREFIX}temporal_resolution"] = config["temporal_resolution"]
    properties[f"{PREFIX}tags"] = model_tags(config, include_multimodal=False)
    properties[f"{PREFIX}multimodal"] = config.get("multimodal") is True

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
    if input_bands and weights_asset is not None:
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
    citation = publication.get("citation")
    title = publication.get("title")
    if isinstance(citation, str) and citation.strip():
        citation = citation.strip()
        # The page renders the title separately as a link, but the citation is
        # published as plain text, so keep the title in it.
        if isinstance(title, str) and title.strip():
            citation = f"{title.strip()}. {citation}"
        properties["sci:citation"] = citation
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
        "documentation": {
            "href": docs_url,
            "type": "text/html",
            "title": f"{config['name']} documentation",
            "roles": ["metadata"],
        },
    }
    if weights_asset is not None:
        assets["model"] = weights_asset
    # udf.py and the runtime are only assets for models that run a UDF; a
    # graph built from built-in processes has neither.
    if (process_dir / "udf.py").is_file():
        assets["udf"] = {
            "href": udf_url,
            "type": "text/x-python",
            "title": f"{config['name']} UDF",
            "description": "User-defined function executed by the process graph.",
            "roles": ["code"],
        }
    if runtime_href:
        assets["runtime"] = {
            "href": runtime_href,
            "type": "application/zip",
            "title": f"{runtime} inference runtime",
            "description": (
                "Python dependency archive supplied through the openEO "
                "`udf-dependency-archives` job option."
            ),
            "roles": ["mlm:inference-runtime", "runtime"],
        }
    if source_archive_key:
        assets["source_code"] = {
            "href": f"{base_url}/{source_archive_key.lstrip('/')}",
            "type": "application/zip",
            "title": "Complete source tree of the release",
            "roles": ["mlm:source_code", "code"],
        }

    item_path = f"collections/{COLLECTION_ID}/items/{item_id}.json"
    collection_path = f"collections/{COLLECTION_ID}/collection.json"
    links: list[dict[str, Any]] = [
        {
            "rel": "self",
            "href": _stac_href(f"./{item_id}.json", item_path, link_href),
            "type": "application/geo+json",
        },
        {
            "rel": "root",
            "href": _stac_href("../../../catalog.json", "catalog.json", link_href),
            "type": "application/json",
        },
        {
            "rel": "collection",
            "href": _stac_href("../collection.json", collection_path, link_href),
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
    if input_bands or "gsd" in properties:
        stac_extensions.append(EO_SCHEMA)
    if cube_dimensions:
        stac_extensions.append(CUBE_SCHEMA)

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


def _collection(
    items: list[dict], summaries: list[dict], link_href: LinkHref | None = None
) -> dict:
    """The single models Collection, with asset templates and summaries."""
    collection_path = f"collections/{COLLECTION_ID}/collection.json"
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
                "href": _stac_href(
                    "./collection.json", collection_path, link_href
                ),
                "type": "application/json",
            },
            {
                "rel": "root",
                "href": _stac_href("../../catalog.json", "catalog.json", link_href),
                "type": "application/json",
            },
            *[
                {
                    "rel": "item",
                    "href": _stac_href(
                        f"./items/{item['id']}.json",
                        f"collections/{COLLECTION_ID}/items/{item['id']}.json",
                        link_href,
                    ),
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


def _catalog(link_href: LinkHref | None = None) -> dict:
    collection_path = f"collections/{COLLECTION_ID}/collection.json"
    return {
        "type": "Catalog",
        "stac_version": STAC_VERSION,
        "id": CATALOG_ID,
        "title": COLLECTION_TITLE,
        "description": COLLECTION_DESCRIPTION,
        "links": [
            {
                "rel": "self",
                "href": _stac_href("./catalog.json", "catalog.json", link_href),
                "type": "application/json",
            },
            {
                "rel": "root",
                "href": _stac_href("./catalog.json", "catalog.json", link_href),
                "type": "application/json",
            },
            {
                "rel": "child",
                "href": _stac_href(
                    f"./collections/{COLLECTION_ID}/collection.json",
                    collection_path,
                    link_href,
                ),
                "type": "application/json",
                "title": COLLECTION_TITLE,
            },
        ],
    }


def validate(
    files: dict[str, dict], link_href: LinkHref | None = None
) -> list[str]:
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
        start_datetime = properties.get("start_datetime")
        end_datetime = properties.get("end_datetime")
        if (start_datetime is None) != (end_datetime is None):
            problems.append(
                f"{path}: start_datetime and end_datetime must be set together"
            )
        elif start_datetime is not None and (
            not isinstance(start_datetime, str)
            or not isinstance(end_datetime, str)
            or start_datetime > end_datetime
        ):
            problems.append(
                f"{path}: start_datetime must not be after end_datetime"
            )
        gsd = properties.get("gsd")
        if gsd is not None and (
            isinstance(gsd, bool) or not isinstance(gsd, (int, float)) or gsd <= 0
        ):
            problems.append(f"{path}: properties.gsd must be a positive number")
        dimensions = properties.get("cube:dimensions")
        if dimensions is not None:
            temporal_dimension = (dimensions or {}).get("t")
            if not isinstance(temporal_dimension, dict):
                problems.append(
                    f"{path}: cube:dimensions.t must describe the temporal dimension"
                )
            elif temporal_dimension.get("type") != "temporal":
                problems.append(f"{path}: cube:dimensions.t must be temporal")
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
        # Weights and a UDF runtime are optional: a model that only loads
        # precomputed embeddings has no checkpoint, and a graph built from
        # built-in processes has no UDF or dependency archive.
        model_asset = assets.get("model")
        if model_asset is not None:
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
        if "process_graph" not in assets:
            problems.append(f"{path}: assets.process_graph is required")

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
                for band in (model_asset or {}).get("eo:bands", [])
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
            href = (
                link_href(f"collections/{COLLECTION_ID}/items/{item_id}.json")
                if link_href
                else f"./items/{item_id}.json"
            )
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

    if link_href:
        # A relative link resolves against the document URL, which for Zenodo
        # is an API /content URL; it would point at a non-document endpoint.
        for path, document in files.items():
            for link in document.get("links", []):
                if link.get("rel") in {
                    "self",
                    "root",
                    "child",
                    "collection",
                    "item",
                } and not _absolute(link.get("href", "")):
                    problems.append(
                        f"{path}: {link.get('rel')} link must be absolute when "
                        f"link_href is used, got {link.get('href')!r}"
                    )
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
    link_href: LinkHref | None = None,
) -> dict[str, dict]:
    """Return the catalog as ``{relative path: JSON document}``.

    ``base_url`` locates the published model files (process graph, UDF,
    runtime); ``docs_base_url`` locates their documentation pages. They differ
    on a release: the files live on Zenodo while the documentation stays on the
    versioned Pages site. ``rendered`` avoids rendering a graph twice when the
    caller already did it (``publish-release.py``).

    ``link_href`` maps a catalog-relative document path to the absolute URL it
    is published at. Without it the catalog uses relative links and can be
    served from any directory. Zenodo needs it: its ``/records/<id>/files/...``
    route sends no ``Access-Control-Allow-Origin`` header, so only the API's
    ``/content`` URLs can be read by browser-based clients such as STAC Browser.
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
            link_href=link_href,
        )
        items.append(item)
        summaries.append(summary)

    if problems:
        raise SystemExit("\n".join(problems))

    files: dict[str, dict] = {"catalog.json": _catalog(link_href)}
    collection_path = f"collections/{COLLECTION_ID}/collection.json"
    files[collection_path] = _collection(items, summaries, link_href)
    for item in items:
        files[f"collections/{COLLECTION_ID}/items/{item['id']}.json"] = item

    problems = validate(files, link_href)
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
