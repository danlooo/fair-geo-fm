"""Shared rendering of openEO process graphs from their Jinja2 templates.

Each model directory keeps a ``process_graph.json.j2`` template with the graph
structure and ``udf.py`` with the user-defined function. The template receives

- ``udf_json`` -- the UDF source as a JSON string literal, read from ``udf.py``
- ``runtime_url`` -- the dependency-archive URL, with ``#feature_deps``
- ``process_graph_url`` -- where the rendered graph is published
- ``model`` / ``runtime`` -- the directory name and runtime name

Publication URLs are themselves Jinja2 templates so a plain GitHub Pages
deployment and a Zenodo release produce the same layout from one base URL:

    normal publish   https://danlooo.github.io/fair-geo-fm/main
    Zenodo release   https://zenodo.org/records/<record-id>/files

The rendered ``process_graph.json`` is committed for the default Pages base so
the repository and local VitePress builds can serve it. ``build`` verifies that
it matches the template; the publish workflows re-render it for their target.

This module is imported by ``build-process-graph.py`` and
``publish-release.py``; it is not a standalone script.
"""

from __future__ import annotations

import json
from pathlib import Path

import jinja2

# Where ``publish`` serves the default branch. Kept in sync with
# docs/.vitepress/config.mjs (base: "/fair-geo-fm/").
DEFAULT_BASE_URL = "https://danlooo.github.io/fair-geo-fm/main"

RUNTIME_URL_TEMPLATE = jinja2.Template(
    "{{ base_url }}/runtimes/{{ runtime }}.zip#feature_deps"
)
PROCESS_GRAPH_URL_TEMPLATE = jinja2.Template(
    "{{ base_url }}/models/{{ model }}/process_graph.json"
)


def runtime_name(process_dir: Path) -> str:
    """The single runtime named by ``runtime.txt``, or ``""`` when absent."""
    runtime_file = process_dir / "runtime.txt"
    if not runtime_file.is_file():
        return ""
    return runtime_file.read_text(encoding="utf-8").strip()


def process_graph_url(base_url: str, model: str) -> str:
    return PROCESS_GRAPH_URL_TEMPLATE.render(
        base_url=base_url.rstrip("/"), model=model
    )


def runtime_url(base_url: str, runtime: str) -> str:
    return RUNTIME_URL_TEMPLATE.render(
        base_url=base_url.rstrip("/"), runtime=runtime
    )


def validate_runtime_reference(
    process_dir: Path, graph: dict, expected_archive_url: str | None = None
) -> list[str]:
    """Validate ``runtime.txt`` against ``runtimes/`` and the job options."""
    name = runtime_name(process_dir)
    if not name:
        return []

    if any(character.isspace() for character in name):
        return [f"{process_dir / 'runtime.txt'}: expected a single runtime name, found {name!r}"]

    problems = []
    repo_root = process_dir.resolve().parent.parent
    if not (repo_root / "runtimes" / name).is_dir():
        problems.append(
            f"{process_dir / 'runtime.txt'} refers to runtime {name!r}, "
            f"but runtimes/{name}/ does not exist"
        )

    archives = (graph.get("default_job_options") or {}).get("udf-dependency-archives") or []
    if not archives:
        problems.append(
            f"{process_dir / 'runtime.txt'}: default_job_options has no udf-dependency-archives"
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
                f"{process_dir / 'runtime.txt'} names runtime {name!r}, but no URL in "
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
    runtime = runtime_name(process_dir)
    if not runtime:
        raise SystemExit(
            f"{process_dir / 'runtime.txt'} is required; the template references "
            "{{ runtime_url }}"
        )

    archive_url = runtime_url(base_url, runtime)
    graph_url = process_graph_url(base_url, model)

    environment = jinja2.Environment(keep_trailing_newline=True, autoescape=False)
    rendered = environment.from_string(
        template_path.read_text(encoding="utf-8")
    ).render(
        udf_json=json.dumps(udf_path.read_text(encoding="utf-8"), ensure_ascii=False),
        runtime_url=archive_url,
        process_graph_url=graph_url,
        model=model,
        runtime=runtime,
    )

    graph = json.loads(rendered)
    text = json.dumps(graph, indent=2, ensure_ascii=False) + "\n"
    return text, graph, graph_url, archive_url
