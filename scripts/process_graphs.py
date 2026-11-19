"""Shared rendering of openEO process graphs from their Jinja2 templates.

Each model directory keeps a ``process_graph.json.j2`` template with the graph
structure, ``config.json`` with the model metadata (its display ``name``,
``description``, optional ``runtime``, its model properties, ``parameters``,
``publication`` and ``links``) and, for models that run a UDF, ``udf.py`` with
the user-defined function. A graph built only from built-in openEO processes
needs neither ``udf.py`` nor a runtime. The template receives

- ``udf_json`` -- the UDF source as a JSON string literal, read from ``udf.py``
  (``""`` when the model has no UDF)
- ``config`` / ``config_json`` -- the parsed ``config.json`` and the same
  mapping as a JSON literal; the template copies it into its ``model`` entry so
  the published graph is independent of the separate config file
- ``runtime_url`` -- the dependency-archive URL, with ``#feature_deps``;
  empty when ``config.json`` declares no runtime
- ``process_graph_url`` -- where the rendered graph is published
- ``version`` -- the release the graph belongs to, or ``main`` for a branch
  render, so the graph identifies itself
- ``zenodo_url`` -- the public Zenodo record page serving the graph's release;
  empty for a staging render, which the template omits entirely
- ``model`` / ``runtime`` -- the directory name and runtime from ``config.json``

Publication URLs are themselves Jinja2 templates so a plain GitHub Pages
deployment and a Zenodo release produce the same layout from one base URL:

    normal publish   https://danlooo.github.io/fair-geo-fm/main
    Zenodo release   https://zenodo.org/records/<record-id>/files

The rendered ``process_graph.json`` is generated from the template on demand
and in CI; it is not committed. ``build`` renders it and verifies the runtime
reference; the publish workflows write it for their target base URL.

Model documentation pages are rendered from the shared ``models/model.md.j2``
template with the same base URL so the copy-paste examples point at the graph
of the same publication target. For a Zenodo release the files live under the
record URL while the page itself stays on the documentation site, so
``render_readme`` takes the documentation base URL separately. The page header
also advertises the release it documents through ``release_version``,
``release_url`` and ``doi``, so every release page links its own version record
and DOI instead of a hardcoded one. A preview render has no DOI and therefore
no header.

This module is imported by ``build-process-graph.py`` and
``publish-release.py``; it is not a standalone script.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path

import jinja2

# Root of the documentation site. Kept in sync with
# docs/.vitepress/config.mjs (base: "/fair-geo-fm/") and with
# scripts/prepare-docs.mjs.
SITE_BASE_URL = "https://danlooo.github.io/fair-geo-fm"
# Where ``publish-staging`` serves the default branch.
DEFAULT_BASE_URL = f"{SITE_BASE_URL}/main"
# External STAC browser. Its /external/ route takes the target URL without the
# scheme, so model pages can link the Item in the browser instead of raw JSON.
STAC_BROWSER_BASE = "https://browser.moregeo.it/external/"
# Concept (root) DOI of the fair-geo-fm record. Every release is published as a
# new version of this record, so the concept DOI always resolves to the latest
# version. Kept in sync with DEFAULT_CONCEPT_DOI in scripts/publish-release.py
# and DOCS_ZENODO_CONCEPT_ID in scripts/prepare-docs.mjs.
CONCEPT_DOI = "10.5281/zenodo.24275707"

# Every model's documentation and graph examples should point at the same
# place and year, so the site shows one consistent region everywhere. The
# values stay in each ``config.json`` (a model directory is self-contained and
# its config is published verbatim), and this mapping is the single source the
# build validates them against.
SHARED_EXAMPLES: dict[str, object] = {
    "spatial_extent": {
        "west": 11.55,
        "south": 50.88,
        "east": 11.65,
        "north": 50.96,
    },
    "temporal_extent": ["2025-01-01", "2026-01-01"],
}

# How a model is distributed: an archive of precomputed embedding tiles, or
# weights the process graph loads to compute embeddings on demand.
MODEL_TYPES = {"frozen embeddings", "weights"}

RUNTIME_URL_TEMPLATE = jinja2.Template(
    "{{ base_url }}/runtimes/{{ runtime }}.zip#feature_deps"
)
PROCESS_GRAPH_URL_TEMPLATE = jinja2.Template(
    "{{ base_url }}/models/{{ model }}/process_graph.json"
)
README_URL_TEMPLATE = jinja2.Template(
    "{{ base_url }}/models/{{ model }}/README.md"
)


def model_config(process_dir: Path) -> dict:
    """The model's ``config.json``, or an empty mapping when absent.

    The file is the single source of truth for the model's display ``name``,
    ``description``, ``runtime``, ``type``, ``architecture``, ``input_sources``,
    ``spatial_extent``/``temporal_extent``, ``spatial_resolution``/
    ``temporal_resolution``, ``dimensions`` (optionally with
    ``dimensions_label``) and
    the ``parameters``, ``publication`` and ``links`` sections of its
    documentation page.
    """
    config_file = process_dir / "config.json"
    if not config_file.is_file():
        return {}
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"{config_file}: invalid JSON: {error}") from error
    if not isinstance(config, dict):
        raise SystemExit(f"{config_file}: expected a JSON object")
    return config


def model_tags(config: dict) -> list[str]:
    """Badge tags derived from the model properties.

    The tags summarize the properties table on the catalogue card, so deriving
    them from the same fields keeps the two in sync. A ``tags`` key in
    ``config.json`` is ignored; ``validate_model_config`` rejects it.
    Mirrors ``autoTags`` in ``scripts/prepare-docs.mjs``.
    """
    tags: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str) and value.strip() and value.strip() not in tags:
            tags.append(value.strip())

    add(config.get("type"))
    add(config.get("dimensions_label"))
    dimensions = config.get("dimensions")
    if (
        isinstance(dimensions, list)
        and dimensions
        and all(
            isinstance(dimension, int) and not isinstance(dimension, bool)
            for dimension in dimensions
        )
    ):
        add(f"{max(dimensions)}-dim")
    add(config.get("spatial_resolution"))
    add(config.get("temporal_resolution"))
    return tags


_R_IDENTIFIER = re.compile(r"^[A-Za-z.][A-Za-z0-9._]*$")


def example_literals(value: object) -> dict[str, str]:
    """Render one JSON example value as a literal for every usage language.

    ``config.json`` stores a parameter's example once, as plain JSON, so the
    documentation template can show the same value in the Web editor (JSON),
    Python and R snippets. JSON object/array syntax is also valid Python; R
    needs its own ``list(...)`` form, hence the derivation.
    """
    return {
        "example_json": json.dumps(
            value, ensure_ascii=False, separators=(", ", ": ")
        ),
        "example_python": _python_literal(value),
        "example_r": _r_literal(value),
    }


def _python_literal(value: object) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{_python_literal(key)}: {_python_literal(item)}"
            for key, item in value.items()
        ) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_python_literal(item) for item in value) + "]"
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return repr(value)


def _r_literal(value: object) -> str:
    if isinstance(value, dict):
        return "list(" + ", ".join(
            f"{_r_name(key)} = {_r_literal(item)}"
            for key, item in value.items()
        ) + ")"
    if isinstance(value, list):
        return "list(" + ", ".join(_r_literal(item) for item in value) + ")"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return repr(value)


def _r_name(key: str) -> str:
    return key if _R_IDENTIFIER.match(key) else f"`{key}`"


def documented_parameters(config: dict) -> list[dict]:
    """The openEO process parameters for the graph's top-level ``parameters``.

    ``config.json`` keeps one ``example`` per parameter and the parameter's
    openEO ``schema``; the graph ships the example the official way, as
    ``schema.examples``, so clients and validators see it without reading the
    site config. Examples are copied into a fresh schema, leaving ``config``
    (which the graph also embeds under ``model``) untouched.
    """
    parameters = []
    for parameter in config.get("parameters", []):
        # Validation reports malformed entries; skip them here so rendering
        # does not fail before the build can.
        if not isinstance(parameter, dict):
            continue
        entry = {
            "name": parameter.get("name", ""),
            "description": parameter.get("description", ""),
        }
        if not parameter.get("required"):
            entry["optional"] = True
            # Optional parameters are referenced by the graph, so a default is
            # what makes omitting them work; the ``default`` in config.json is
            # the markdown display string and cannot be used here.
            if "default_value" in parameter:
                entry["default"] = copy.deepcopy(parameter["default_value"])
        schema = copy.deepcopy(parameter.get("schema") or {})
        if "example" in parameter:
            schema["examples"] = [copy.deepcopy(parameter["example"])]
        entry["schema"] = schema
        parameters.append(entry)
    return parameters


def validate_parameter_usage(graph: dict) -> list[str]:
    """Check that every declared process parameter is actually referenced.

    A top-level ``parameters`` entry that no node reads is dead metadata, so
    the graph must consume each one through ``{"from_parameter": ...}``.
    """
    declared = [
        parameter.get("name")
        for parameter in graph.get("parameters") or []
        if isinstance(parameter, dict)
    ]
    used: set[object] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            reference = node.get("from_parameter")
            if isinstance(reference, str):
                used.add(reference)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(graph.get("process_graph"))
    return [
        f"parameter {name!r} is declared but never referenced with from_parameter"
        for name in declared
        if name not in used
    ]


def runtime_name(process_dir: Path) -> str:
    """The single runtime named by ``config.json``, or ``""`` when absent."""
    runtime = model_config(process_dir).get("runtime")
    return runtime.strip() if isinstance(runtime, str) else ""


def validate_model_config(process_dir: Path) -> list[str]:
    """Validate the model metadata in ``config.json``.

    The README template reads every field, so a missing or mistyped one would
    either break the render or publish an empty section.
    """
    config_file = process_dir / "config.json"
    if not config_file.is_file():
        return [
            f"{config_file} is required; it names the runtime and describes "
            "the model in the catalogue"
        ]

    config = model_config(process_dir)
    problems = []
    for key in ("name", "description"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            problems.append(f"{config_file}: '{key}' must be a non-empty string")
    # The runtime is optional: a graph built only from built-in openEO
    # processes needs no dependency archive.
    if "runtime" in config and (
        not isinstance(config["runtime"], str) or not config["runtime"].strip()
    ):
        problems.append(
            f"{config_file}: 'runtime' must be a non-empty string when set"
        )
    if "warning" in config and (
        not isinstance(config["warning"], str) or not config["warning"].strip()
    ):
        problems.append(
            f"{config_file}: 'warning' must be a non-empty string when set"
        )
    if "tags" in config:
        problems.append(
            f"{config_file}: 'tags' is generated from the model properties; "
            "remove it"
        )

    # The properties table on the model page renders these four fields, so a
    # missing or mistyped one would either break the page or misstate coverage.
    if config.get("type") not in MODEL_TYPES:
        problems.append(
            f"{config_file}: 'type' must be one of {sorted(MODEL_TYPES)}"
        )
    architecture = config.get("architecture")
    if not isinstance(architecture, str) or not architecture.strip():
        problems.append(
            f"{config_file}: 'architecture' must be a non-empty string"
        )
    # The same value is published as mlm:architecture in the STAC Item; keep
    # the page tag and the Item from disagreeing.
    stac_file = process_dir / "stac.json"
    if isinstance(architecture, str) and stac_file.is_file():
        try:
            stac = json.loads(stac_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stac = {}
        expected = (stac.get("properties") or {}).get("mlm:architecture")
        if expected is not None and expected != architecture:
            problems.append(
                f"{config_file}: 'architecture' must match stac.json "
                f"mlm:architecture {expected!r}"
            )
    sources = config.get("input_sources")
    if (
        not isinstance(sources, list)
        or not sources
        or any(not isinstance(source, str) or not source.strip() for source in sources)
    ):
        problems.append(
            f"{config_file}: 'input_sources' must be a non-empty list of "
            "non-empty strings"
        )
    bbox = config.get("spatial_extent")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in bbox
        )
    ):
        problems.append(
            f"{config_file}: 'spatial_extent' must be a [west, south, east, north] "
            "array of numbers"
        )
    elif not (-180 <= bbox[0] <= bbox[2] <= 180 and -90 <= bbox[1] <= bbox[3] <= 90):
        problems.append(
            f"{config_file}: 'spatial_extent' must satisfy "
            "-180 <= west <= east <= 180 and -90 <= south <= north <= 90"
        )
    temporal = config.get("temporal_extent")
    if (
        not isinstance(temporal, list)
        or len(temporal) != 2
        or any(not isinstance(value, str) or not value.strip() for value in temporal)
    ):
        problems.append(
            f"{config_file}: 'temporal_extent' must be a [start, end] array of "
            "non-empty strings"
        )
    elif temporal[0] > temporal[1]:
        problems.append(f"{config_file}: 'temporal_extent' start is after its end")
    resolution = config.get("spatial_resolution")
    if not isinstance(resolution, str) or not resolution.strip():
        problems.append(
            f"{config_file}: 'spatial_resolution' must be a non-empty string"
        )
    temporal_resolution = config.get("temporal_resolution")
    if (
        not isinstance(temporal_resolution, str)
        or not temporal_resolution.strip()
    ):
        problems.append(
            f"{config_file}: 'temporal_resolution' must be a non-empty string"
        )
    dimensions = config.get("dimensions")
    if (
        not isinstance(dimensions, list)
        or not dimensions
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in dimensions
        )
    ):
        problems.append(
            f"{config_file}: 'dimensions' must be a non-empty list of positive "
            "integers"
        )
    elif dimensions != sorted(set(dimensions)):
        problems.append(
            f"{config_file}: 'dimensions' must be unique and in ascending order"
        )
    if "dimensions_label" in config and (
        not isinstance(config["dimensions_label"], str)
        or not config["dimensions_label"].strip()
    ):
        problems.append(
            f"{config_file}: 'dimensions_label' must be a non-empty string when set"
        )

    parameters = config.get("parameters")
    if not isinstance(parameters, list):
        problems.append(f"{config_file}: 'parameters' must be a list")
    else:
        for index, parameter in enumerate(parameters):
            where = f"{config_file}: parameters[{index}]"
            if not isinstance(parameter, dict):
                problems.append(f"{where} must be an object")
                continue
            for key in ("name", "default", "description"):
                value = parameter.get(key)
                if not isinstance(value, str) or not value.strip():
                    problems.append(f"{where}.{key} must be a non-empty string")
            # The graph declares every parameter in its official openEO
            # ``parameters`` array from this JSON Schema.
            if not isinstance(parameter.get("schema"), dict):
                problems.append(f"{where}.schema must be an object")
            required = parameter.get("required")
            if not isinstance(required, bool):
                problems.append(f"{where}.required must be a boolean")
            # The usage examples render one argument per required parameter
            # from this value, so a required parameter without one cannot be
            # documented.
            if required and "example" not in parameter:
                problems.append(
                    f"{where}.example is required for a required parameter"
                )
            # Keep the shared spatial and temporal example uniform across
            # models; the values live in each config.json, so without this
            # check they drift one model at a time.
            shared = SHARED_EXAMPLES.get(parameter.get("name"))
            if shared is not None and parameter.get("example") != shared:
                problems.append(
                    f"{where}.example must match the shared example {shared!r}"
                )
            # The graph passes every parameter by reference, so an optional
            # parameter needs a real default to fall back to when omitted.
            if required is False and "default_value" not in parameter:
                problems.append(
                    f"{where}.default_value is required for an optional parameter"
                )

    publication = config.get("publication")
    if not isinstance(publication, dict):
        problems.append(f"{config_file}: 'publication' must be an object")
    else:
        for key in ("title", "url", "citation", "abstract"):
            value = publication.get(key)
            if not isinstance(value, str) or not value.strip():
                problems.append(
                    f"{config_file}: publication.{key} must be a non-empty string"
                )

    links = config.get("links")
    if not isinstance(links, list):
        problems.append(f"{config_file}: 'links' must be a list")
    else:
        for index, link in enumerate(links):
            where = f"{config_file}: links[{index}]"
            if not isinstance(link, dict):
                problems.append(f"{where} must be an object")
                continue
            for key in ("title", "url"):
                value = link.get(key)
                if not isinstance(value, str) or not value.strip():
                    problems.append(f"{where}.{key} must be a non-empty string")
    return problems


def process_graph_url(base_url: str, model: str) -> str:
    return PROCESS_GRAPH_URL_TEMPLATE.render(
        base_url=base_url.rstrip("/"), model=model
    )


def runtime_url(base_url: str, runtime: str) -> str:
    return RUNTIME_URL_TEMPLATE.render(
        base_url=base_url.rstrip("/"), runtime=runtime
    )


def readme_url(base_url: str, model: str) -> str:
    return README_URL_TEMPLATE.render(
        base_url=base_url.rstrip("/"), model=model
    )




def validate_runtime_reference(
    process_dir: Path, graph: dict, expected_archive_url: str | None = None
) -> list[str]:
    """Validate the ``runtime`` from ``config.json`` against ``runtimes/``.

    Also checks that the graph's ``default_job_options`` reference the archive
    of that runtime. A model without a runtime is fine, but it must not declare
    ``udf-dependency-archives``; a model without ``config.json`` yields no
    problem here because ``validate_model_config`` reports the missing file.
    """
    name = runtime_name(process_dir)
    archives = (graph.get("default_job_options") or {}).get(
        "udf-dependency-archives"
    ) or []
    if not name:
        if archives:
            return [
                f"{process_dir / 'config.json'}: declares "
                "udf-dependency-archives but sets no 'runtime'"
            ]
        return []

    if any(character.isspace() for character in name):
        return [
            f"{process_dir / 'config.json'}: expected a single runtime name, "
            f"found {name!r}"
        ]

    problems = []
    repo_root = process_dir.resolve().parent.parent
    if not (repo_root / "runtimes" / name).is_dir():
        problems.append(
            f"{process_dir / 'config.json'} refers to runtime {name!r}, "
            f"but runtimes/{name}/ does not exist"
        )

    if not archives:
        problems.append(
            f"{process_dir / 'config.json'}: default_job_options has no udf-dependency-archives"
        )
    elif expected_archive_url is not None and expected_archive_url not in archives:
        problems.append(
            f"default_job_options['udf-dependency-archives'] must use "
            f"{{{{ runtime_url }}}}, expected {expected_archive_url!r}, found: {archives}"
        )
    else:
        suffix = f"/runtimes/{name}.zip#feature_deps"
        if not any(archive.endswith(suffix) for archive in archives):
            problems.append(
                f"{process_dir / 'config.json'} names runtime {name!r}, but no URL in "
                f"default_job_options['udf-dependency-archives'] ends with {suffix!r}: "
                f"{archives}"
            )
    return problems


def render(
    process_dir: Path,
    base_url: str,
    version: str | None = None,
    zenodo_url: str | None = None,
) -> tuple[str, dict, str, str]:
    """Render ``process_graph.json.j2`` for ``base_url``.

    Returns ``(text, graph, graph_url, archive_url)``. ``text`` is canonical
    JSON (two-space indent, trailing newline), so it can be compared with the
    committed file.

    ``version`` and ``zenodo_url`` label the release the graph belongs to and
    are rendered into the graph so it identifies itself. ``version`` defaults
    to ``main`` and ``zenodo_url`` stays empty for a staging render, so the
    template omits it; ``publish-release.py`` passes the release tag and that
    version record instead.
    """
    process_dir = process_dir.resolve()
    template_path = process_dir / "process_graph.json.j2"
    if not template_path.is_file():
        raise SystemExit(
            f"{template_path} not found; process graphs are rendered from "
            "process_graph.json.j2"
        )
    # Models built only from built-in processes have no udf.py and no runtime.
    # The template still receives both variables, as empty values, so a shared
    # template can omit them.
    udf_path = process_dir / "udf.py"
    udf_source = udf_path.read_text(encoding="utf-8") if udf_path.is_file() else ""

    model = process_dir.name
    config = model_config(process_dir)
    runtime = config.get("runtime")
    runtime = runtime.strip() if isinstance(runtime, str) else ""

    archive_url = runtime_url(base_url, runtime) if runtime else ""
    graph_url = process_graph_url(base_url, model)
    version = version or "main"
    zenodo_url = zenodo_url or ""

    environment = jinja2.Environment(keep_trailing_newline=True, autoescape=False)
    rendered = environment.from_string(
        template_path.read_text(encoding="utf-8")
    ).render(
        udf_json=json.dumps(udf_source, ensure_ascii=False),
        # The graph copies these into its ``model`` entry, so the published
        # process_graph.json is self-contained and does not read config.json.
        config=config,
        config_json=json.dumps(config, indent=2, ensure_ascii=False),
        # Official openEO parameters, with each parameter's example moved into
        # its schema as ``examples`` (the openEO/JSON Schema way to ship them).
        parameters_json=json.dumps(
            documented_parameters(config), ensure_ascii=False
        ),
        runtime_url=archive_url,
        process_graph_url=graph_url,
        version=version,
        zenodo_url=zenodo_url,
        model=model,
        runtime=runtime,
    )

    graph = json.loads(rendered)
    text = json.dumps(graph, indent=2, ensure_ascii=False) + "\n"
    return text, graph, graph_url, archive_url


def latest_release(repo_root: Path) -> str:
    """The newest ``v*`` tag in ``repo_root``, or ``""`` when there is none.

    The tag is only used to label a rendered page that is not itself a release
    (the staging build). A shallow CI checkout may not fetch tags at all; the
    caller then falls back to the branch name.
    """
    try:
        output = subprocess.run(
            ["git", "tag", "--list", "v*", "--sort=version:refname"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""
    tags = [line for line in output.splitlines() if line.strip()]
    return tags[-1] if tags else ""


def stac_browser_url(url: str) -> str:
    """Wrap a STAC file URL in the external STAC browser's /external/ route.

    ``https://host/path`` becomes ``https://browser.../external/host/path``, so
    readers land in the browser instead of downloading raw JSON.
    """
    return (
        f"{STAC_BROWSER_BASE}"
        f"{url.removeprefix('https://').removeprefix('http://')}"
    )


ZENODO_FILES_URL = re.compile(
    r"^(?P<site>https?://[^/]+)/records/(?P<id>[^/]+)/files/?$"
)


def stac_document_url(base_url: str, path: str) -> str:
    """URL a browser-based reader can fetch for the STAC document ``path``.

    On the staging site the document is an ordinary file below ``stac/``. On a
    Zenodo record the plain file route sends no ``Access-Control-Allow-Origin``
    header, so STAC Browser cannot read it; the API's ``/content`` route can.
    """
    base_url = base_url.rstrip("/")
    if match := ZENODO_FILES_URL.match(base_url):
        return (
            f"{match['site']}/api/records/{match['id']}/files/stac/"
            f"{path}/content"
        )
    return f"{base_url}/stac/{path}"


def stac_item_id(process_dir: Path) -> str:
    """The STAC Item id for a model directory.

    ``stac.json`` names the generated Item; without it, the directory name is
    the fallback. Kept in sync with ``prepare-docs.mjs``, which reads the same
    field for the documentation links.
    """
    stac_file = process_dir / "stac.json"
    if not stac_file.is_file():
        return process_dir.name
    try:
        stac = json.loads(stac_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"{stac_file}: invalid JSON: {error}") from None
    item_id = stac.get("id") if isinstance(stac, dict) else None
    return item_id if isinstance(item_id, str) and item_id else process_dir.name


def render_readme(
    process_dir: Path,
    base_url: str,
    docs_base_url: str,
    release: bool = False,
    release_version: str | None = None,
    doi: str | None = None,
    site: bool = False,
) -> tuple[str, str]:
    """Render a model's documentation page for ``base_url``.

    The page comes from the shared ``model.md.j2`` in the parent directory.
    ``base_url`` locates the model's published files (the process graph),
    ``docs_base_url`` the version's documentation page. The page title,
    description, parameters, publication and links come from the model's
    ``config.json``, passed as ``config``; the badge tags are derived from its
    properties by ``model_tags``.
    ``release_version`` and
    ``doi`` describe the release the page advertises (its header links both):
    they default to the newest ``v*`` tag and to the concept DOI for a release
    render. A preview render (``release=False``) has no DOI, so the template's
    ``{% if doi %}`` gate omits the header; only tagged releases advertise one.
    ``release`` is exposed to templates that gate development notices on it; it
    defaults to the branch (preview) rendering.
    The page also links the generated STAC catalog and collection (raw JSON)
    and the Item through the external STAC browser; all live under
    ``base_url``/stac, and the Item id comes from ``stac.json``.
    Returns ``(text, readme_url)``. Unknown template variables are an error.
    """
    process_dir = process_dir.resolve()
    template_path = process_dir.parent / "model.md.j2"
    if not template_path.is_file():
        raise SystemExit(
            f"{template_path} not found; model pages are rendered from model.md.j2"
        )

    model = process_dir.name
    advertised = release_version or latest_release(process_dir.parent.parent) or "main"
    # The STAC tree lives next to the model files in the same publication
    # target, so the page's STAC links follow the process-graph base URL.
    stac_base = f"{base_url.rstrip('/')}/stac"
    item_id = stac_item_id(process_dir)
    environment = jinja2.Environment(
        keep_trailing_newline=True,
        autoescape=False,
        undefined=jinja2.StrictUndefined,
    )
    # ``warning`` and ``dimensions_label`` are optional in config.json, but the
    # page template reads them under StrictUndefined; default them so models
    # without one still render.
    config = {"warning": "", "dimensions_label": "", **model_config(process_dir)}
    # The badges are derived from the properties, never hand-maintained.
    config["tags"] = model_tags(config)
    text = environment.from_string(
        template_path.read_text(encoding="utf-8")
    ).render(
        base_url=base_url.rstrip("/"),
        # The parsed config.json is the model's single source of truth; the
        # page title, description, input parameters, publication and links
        # come from it rather than being duplicated in the template. The badge
        # tags are derived from it just above.
        config=config,
        # The usage examples show the required parameters only, filled with
        # the per-language example literals from config.json, so the page
        # stays in sync with the parameter table.
        required_parameters=[
            {**parameter, **example_literals(parameter.get("example"))}
            for parameter in config.get("parameters", [])
            if parameter.get("required")
        ],
        model=model,
        release=release,
        # A README served from Zenodo has no Vue components; the site renderer
        # (scripts/prepare-docs.mjs) sets this to embed the process graph.
        site=site,
        process_graph_url=process_graph_url(base_url, model),
        docs_url=f"{docs_base_url.rstrip('/')}/models/{model}/",
        release_version=advertised,
        release_url=f"{SITE_BASE_URL}/{advertised}/models/{model}/",
        stac_catalog_url=f"{stac_base}/catalog.json",
        stac_collection_url=(
            f"{stac_base}/collections/fair-geo-fm-models/collection.json"
        ),
        # The browser must fetch the Item from a CORS-enabled URL on Zenodo;
        # the raw catalog/collection links above stay plain downloadable files.
        stac_item_url=stac_browser_url(
            stac_document_url(
                base_url,
                f"collections/fair-geo-fm-models/items/{item_id}.json",
            )
        ),
        # Only a release has a DOI; a preview build gets no release header.
        doi=CONCEPT_DOI if doi is None and release else (doi or ""),
    )
    return text, readme_url(base_url, model)
