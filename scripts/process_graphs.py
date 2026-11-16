"""Shared rendering of openEO process graphs from their Jinja2 templates.

Each model directory keeps a ``process_graph.json.j2`` template with the graph
structure, ``udf.py`` with the user-defined function and ``config.json`` with
the model metadata: its display ``name``, ``description``, ``runtime``,
badge ``tags``, ``parameters``, ``publication`` and ``links``. The template
receives

- ``udf_json`` -- the UDF source as a JSON string literal, read from ``udf.py``
- ``config`` / ``config_json`` -- the parsed ``config.json`` and the same
  mapping as a JSON literal; the template copies it into its ``model`` entry so
  the published graph is independent of the separate config file
- ``runtime_url`` -- the dependency-archive URL, with ``#feature_deps``
- ``process_graph_url`` -- where the rendered graph is published
- ``model`` / ``runtime`` -- the directory name and runtime from ``config.json``

Publication URLs are themselves Jinja2 templates so a plain GitHub Pages
deployment and a Zenodo release produce the same layout from one base URL:

    normal publish   https://danlooo.github.io/fair-geo-fm/main
    Zenodo release   https://zenodo.org/records/<record-id>/files

The rendered ``process_graph.json`` is generated from the template on demand
and in CI; it is not committed. ``build`` renders it and verifies the runtime
reference; the publish workflows write it for their target base URL.

Model documentation pages are rendered from the shared ``models/model.md.j2``
template; released refs keep a ``README.md.j2`` next to the model instead, which
takes precedence. Either template is rendered with the same base URL so the
copy-paste examples point at the graph
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

import json
import subprocess
from pathlib import Path

import jinja2

# Root of the documentation site. Kept in sync with
# docs/.vitepress/config.mjs (base: "/fair-geo-fm/") and with
# scripts/prepare-docs.mjs.
SITE_BASE_URL = "https://danlooo.github.io/fair-geo-fm"
# Where ``publish`` serves the default branch.
DEFAULT_BASE_URL = f"{SITE_BASE_URL}/main"
# Concept (root) DOI of the fair-geo-fm record. Every release is published as a
# new version of this record, so the concept DOI always resolves to the latest
# version. Kept in sync with DEFAULT_CONCEPT_DOI in scripts/publish-release.py
# and DOCS_ZENODO_CONCEPT_ID in scripts/prepare-docs.mjs.
CONCEPT_DOI = "10.5281/zenodo.24193626"

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
    ``description``, ``runtime``, badge ``tags`` and the ``parameters``,
    ``publication`` and ``links`` sections of its documentation page.
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
    for key in ("name", "description", "runtime"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            problems.append(f"{config_file}: '{key}' must be a non-empty string")
    tags = config.get("tags", [])
    if not isinstance(tags, list) or any(
        not isinstance(tag, str) or not tag.strip() for tag in tags
    ):
        problems.append(f"{config_file}: 'tags' must be a list of non-empty strings")

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
            if not isinstance(parameter.get("required"), bool):
                problems.append(f"{where}.required must be a boolean")

    publication = config.get("publication")
    if not isinstance(publication, dict):
        problems.append(f"{config_file}: 'publication' must be an object")
    else:
        for key in ("citation", "abstract"):
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
    of that runtime. A model without ``config.json`` yields no problem here;
    ``validate_model_config`` reports the missing file.
    """
    name = runtime_name(process_dir)
    if not name:
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

    archives = (graph.get("default_job_options") or {}).get("udf-dependency-archives") or []
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


def render(process_dir: Path, base_url: str) -> tuple[str, dict, str, str]:
    """Render ``process_graph.json.j2`` for ``base_url``.

    Returns ``(text, graph, graph_url, archive_url)``. ``text`` is canonical
    JSON (two-space indent, trailing newline), so it can be compared with the
    committed file.
    """
    process_dir = process_dir.resolve()
    template_path = process_dir / "process_graph.json.j2"
    if not template_path.is_file():
        raise SystemExit(
            f"{template_path} not found; process graphs are rendered from "
            "process_graph.json.j2"
        )
    udf_path = process_dir / "udf.py"
    if not udf_path.is_file():
        raise SystemExit(f"{udf_path} not found")

    model = process_dir.name
    config = model_config(process_dir)
    runtime = config.get("runtime")
    runtime = runtime.strip() if isinstance(runtime, str) else ""
    if not runtime:
        raise SystemExit(
            f"{process_dir / 'config.json'} is required and must set 'runtime'; "
            "the template references {{ runtime_url }}"
        )

    archive_url = runtime_url(base_url, runtime)
    graph_url = process_graph_url(base_url, model)

    environment = jinja2.Environment(keep_trailing_newline=True, autoescape=False)
    rendered = environment.from_string(
        template_path.read_text(encoding="utf-8")
    ).render(
        udf_json=json.dumps(udf_path.read_text(encoding="utf-8"), ensure_ascii=False),
        # The graph copies these into its ``model`` entry, so the published
        # process_graph.json is self-contained and does not read config.json.
        config=config,
        config_json=json.dumps(config, indent=2, ensure_ascii=False),
        runtime_url=archive_url,
        process_graph_url=graph_url,
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


def render_readme(
    process_dir: Path,
    base_url: str,
    docs_base_url: str,
    release: bool = False,
    release_version: str | None = None,
    doi: str | None = None,
) -> tuple[str, str]:
    """Render a model's documentation page for ``base_url``.

    The page comes from the model's own ``README.md.j2`` when present (released
    refs), otherwise from the shared ``model.md.j2`` in the parent directory.
    ``base_url`` locates the model's published files (the process graph),
    ``docs_base_url`` the version's documentation page. The page title,
    description, badge tags, parameters, publication and links come from the
    model's ``config.json``, passed as ``config``.
    ``release_version`` and
    ``doi`` describe the release the page advertises (its header links both):
    they default to the newest ``v*`` tag and to the concept DOI for a release
    render. A preview render (``release=False``) has no DOI, so the template's
    ``{% if doi %}`` gate omits the header; only tagged releases advertise one.
    ``release`` is exposed to templates that gate development notices on it; it
    defaults to the branch (preview) rendering.
    Returns ``(text, readme_url)``. Unknown template variables are an error.
    """
    process_dir = process_dir.resolve()
    template_path = process_dir / "README.md.j2"
    if not template_path.is_file():
        template_path = process_dir.parent / "model.md.j2"
    if not template_path.is_file():
        raise SystemExit(
            f"{process_dir / 'README.md.j2'} and "
            f"{process_dir.parent / 'model.md.j2'} not found; model pages are "
            "rendered from one of these templates"
        )

    model = process_dir.name
    advertised = release_version or latest_release(process_dir.parent.parent) or "main"
    environment = jinja2.Environment(
        keep_trailing_newline=True,
        autoescape=False,
        undefined=jinja2.StrictUndefined,
    )
    text = environment.from_string(
        template_path.read_text(encoding="utf-8")
    ).render(
        base_url=base_url.rstrip("/"),
        # The parsed config.json is the model's single source of truth; the
        # page title, description, badge tags, input parameters, publication
        # and links come from it rather than being duplicated in the template.
        config=model_config(process_dir),
        model=model,
        release=release,
        process_graph_url=process_graph_url(base_url, model),
        docs_url=f"{docs_base_url.rstrip('/')}/models/{model}/",
        release_version=advertised,
        release_url=f"{SITE_BASE_URL}/{advertised}/models/{model}/",
        # Only a release has a DOI; a preview build gets no release header.
        doi=CONCEPT_DOI if doi is None and release else (doi or ""),
    )
    return text, readme_url(base_url, model)
