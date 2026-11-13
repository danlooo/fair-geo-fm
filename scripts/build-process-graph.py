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
rendered from ``runtime.txt`` plus the target base URL.

Two publication targets share the same layout:

    GitHub Pages (normal publish)
        https://danlooo.github.io/fair-geo-fm/main/models/<model>/process_graph.json
        https://danlooo.github.io/fair-geo-fm/main/runtimes/<runtime>.zip
    Zenodo (manual release)
        https://doi.org/<doi>/models/<model>/process_graph.json
        https://doi.org/<doi>/runtimes/<runtime>.zip

``udf-dependency-archives`` entries always carry the ``#feature_deps`` suffix.

Usage:
    uv run scripts/build-process-graph.py models/tessera_v2
    uv run scripts/build-process-graph.py models/tessera_v2 --check
    uv run scripts/build-process-graph.py models/tessera_v2 \\
        --base-url https://doi.org/10.5281/zenodo.123456 \\
        --output /tmp/release/models/tessera_v2/process_graph.json

``--check`` writes nothing and exits non-zero when the committed
``process_graph.json`` is stale or the runtime reference is invalid, for CI or
a pre-commit hook.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from process_graphs import DEFAULT_BASE_URL, render, validate_runtime_reference


def build(process_dir: Path, base_url: str, check: bool, output: Path | None) -> int:
    graph_path = process_dir / "process_graph.json"
    text, graph, graph_url, archive_url = render(process_dir, base_url)

    problems = validate_runtime_reference(process_dir, graph, archive_url)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1

    print(f"process graph: {graph_url}")
    print(f"runtime archive: {archive_url}")

    if check:
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
        help="only verify that process_graph.json is up to date",
    )
    args = parser.parse_args()
    raise SystemExit(build(args.process_dir, args.base_url, args.check, args.output))


if __name__ == "__main__":
    main()
