#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["jinja2"]
# ///
"""Render a model's ``process_graph.json`` from ``process_graph.json.j2``.

``udf.py`` is the source of truth for the UDF code and
``process_graph.json.j2`` is the source of truth for the graph structure and
its published URLs. A remote process definition has to carry the UDF as inline
source, so the template receives it as ``udf_json``; the archive URL is
rendered from the runtime named in the model's ``config.json`` plus the target
base URL. ``config.json`` also holds the model's display name and badge tags
for the catalogue page, so it is validated here as well.

Two publication targets share the same layout:

    GitHub Pages (normal publish)
        https://danlooo.github.io/fair-geo-fm/main/models/<model>/process_graph.json
        https://danlooo.github.io/fair-geo-fm/main/runtimes/<runtime>.zip
    Zenodo (manual release)
        https://zenodo.org/records/<record-id>/files/models/<model>/process_graph.json
        https://zenodo.org/records/<record-id>/files/runtimes/<runtime>.zip

``udf-dependency-archives`` entries always carry the ``#feature_deps`` suffix.

Usage:
    uv run scripts/build-process-graph.py models/tessera_v2
    uv run scripts/build-process-graph.py models/tessera_v2 --check
    uv run scripts/build-process-graph.py models/tessera_v2 \\
        --base-url https://zenodo.org/records/123456/files \\
        --output /tmp/release/models/tessera_v2/process_graph.json

When the model has a ``README.md.j2``, its template is rendered as well so the
build catches unknown variables before the docs or release pipeline does; the
rendered page itself is not written here.

``--check`` writes nothing. It always renders the template (validating the
graph structure, the inlined UDF and the runtime reference) and exits non-zero
when an existing ``process_graph.json`` no longer matches the template. A
missing generated file is fine: ``process_graph.json`` is rendered from the
template rather than committed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from process_graphs import (
    DEFAULT_BASE_URL,
    render,
    render_readme,
    validate_model_config,
    validate_runtime_reference,
)


def build(process_dir: Path, base_url: str, check: bool, output: Path | None) -> int:
    graph_path = process_dir / "process_graph.json"
    text, graph, graph_url, archive_url = render(process_dir, base_url)

    problems = validate_model_config(process_dir) + validate_runtime_reference(
        process_dir, graph, archive_url
    )
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1

    print(f"process graph: {graph_url}")
    print(f"runtime archive: {archive_url}")

    # The model page is rendered by the docs and release pipelines, not here,
    # but rendering it validates its template variables during the build.
    if (process_dir / "README.md.j2").is_file():
        _readme, readme_url = render_readme(process_dir, base_url, base_url)
        print(f"model README: {readme_url}")

    if check:
        # process_graph.json is generated from the template, so a fresh
        # checkout has no copy to compare. Rendering above already validated
        # the template, the inlined UDF and the runtime reference.
        if not graph_path.is_file():
            print(f"{graph_path} is generated from the template; nothing to compare")
            return 0
        # Tolerate a missing or extra trailing newline in the file on disk.
        if graph_path.read_text(encoding="utf-8").rstrip("\n") != text.rstrip("\n"):
            print(
                f"{graph_path} is out of date; run "
                f"uv run {Path(sys.argv[0]).name} {process_dir}",
                file=sys.stderr,
            )
            return 1
        print(f"{graph_path} is up to date")
        return 0

    target = output or graph_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "process_dir", type=Path,
        help="process directory containing process_graph.json.j2 and udf.py",
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL,
        help=f"URL the model and runtimes are published under (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--output", type=Path,
        help="write the rendered graph here instead of <process_dir>/process_graph.json",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="render without writing; fail if an existing process_graph.json is stale",
    )
    args = parser.parse_args()
    raise SystemExit(build(args.process_dir, args.base_url, args.check, args.output))


if __name__ == "__main__":
    main()
