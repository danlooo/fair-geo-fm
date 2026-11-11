#!/usr/bin/env python3
"""Inline a process directory's ``udf.py`` into its ``process_graph.json``.

A remote process definition has to carry the UDF as inline source: a relative
path in ``udf`` is resolved against the *user's* server-side workspace, not the
repository, and URL UDFs are backend-specific. So ``udf.py`` is the source of
truth for the code, ``process_graph.json`` is the source of truth for the graph
structure, and this script keeps the ``udf`` argument of the ``run_udf`` node in
sync with the file.

Usage:
    uv run scripts/build-process-graph.py processes/tessera_v2
    uv run scripts/build-process-graph.py processes/tessera_v2 --check

``--check`` writes nothing and exits non-zero when ``process_graph.json`` is
stale, for CI or a pre-commit hook.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterator


def find_run_udf_nodes(node: object) -> Iterator[dict]:
    """Yield every ``run_udf`` node in a (nested) process graph."""
    if isinstance(node, dict):
        if node.get("process_id") == "run_udf":
            yield node
        for value in node.values():
            yield from find_run_udf_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from find_run_udf_nodes(value)


def build(process_dir: Path, check: bool) -> int:
    udf_path = process_dir / "udf.py"
    graph_path = process_dir / "process_graph.json"

    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    nodes = list(find_run_udf_nodes(graph))
    if len(nodes) != 1:
        raise SystemExit(
            f"{graph_path}: expected exactly one run_udf node, found {len(nodes)}"
        )
    nodes[0].setdefault("arguments", {})["udf"] = udf_path.read_text(encoding="utf-8")

    text = json.dumps(graph, indent=2, ensure_ascii=False) + "\n"

    if check:
        if graph_path.read_text(encoding="utf-8") != text:
            print(
                f"{graph_path} is out of date; run "
                f"uv run {Path(sys.argv[0]).name} {process_dir}",
                file=sys.stderr,
            )
            return 1
        print(f"{graph_path} is up to date")
        return 0

    graph_path.write_text(text, encoding="utf-8")
    print(f"wrote {graph_path}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "process_dir", type=Path,
        help="process directory containing udf.py and process_graph.json",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="only verify that process_graph.json is up to date",
    )
    args = parser.parse_args()
    raise SystemExit(build(args.process_dir, args.check))


if __name__ == "__main__":
    main()
