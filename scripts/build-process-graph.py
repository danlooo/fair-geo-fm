#!/usr/bin/env python3
"""Inline a process directory's ``udf.py`` into its ``process_graph.json``.

A remote process definition has to carry the UDF as inline source: a relative
path in ``udf`` is resolved against the *user's* server-side workspace, not the
repository, and URL UDFs are backend-specific. So ``udf.py`` is the source of
truth for the code, ``process_graph.json`` is the source of truth for the graph
structure, and this script keeps the ``udf`` argument of the ``run_udf`` node in
sync with the file.

If the directory also has a ``runtime.txt``, it names the runtime directory under
``runtimes/`` the process depends on (e.g. ``py311-torch231``). The script then
requires that directory to exist and that at least one
``default_job_options["udf-dependency-archives"]`` URL (ignoring the ``#``
fragment) contains that name, so the interpreter and the dependency archive
cannot drift apart.

Usage:
    uv run scripts/build-process-graph.py processes/tessera_v2
    uv run scripts/build-process-graph.py processes/tessera_v2 --check

``--check`` writes nothing and exits non-zero when ``process_graph.json`` is
stale or the runtime reference is invalid, for CI or a pre-commit hook.
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


def check_runtime_reference(process_dir: Path, graph: dict) -> list[str]:
    """Validate ``runtime.txt`` against ``runtimes/`` and the job options."""
    runtime_file = process_dir / "runtime.txt"
    if not runtime_file.is_file():
        return []

    names = [line.strip() for line in runtime_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(names) != 1:
        return [f"{runtime_file}: expected exactly one runtime directory name, found {names}"]
    runtime = names[0]

    problems = []
    repo_root = process_dir.resolve().parent.parent
    if not (repo_root / "runtimes" / runtime).is_dir():
        problems.append(f"{runtime_file} names {runtime!r}, but runtimes/{runtime}/ does not exist")

    archives = (graph.get("default_job_options") or {}).get("udf-dependency-archives") or []
    if not archives:
        problems.append(f"{runtime_file}: default_job_options has no udf-dependency-archives")
    elif not any(runtime in url.split("#", 1)[0] for url in archives if isinstance(url, str)):
        problems.append(
            f"{runtime_file} names {runtime!r}, but no udf-dependency-archives URL "
            f"references it: {archives}"
        )
    return problems


def build(process_dir: Path, check: bool) -> int:
    udf_path = process_dir / "udf.py"
    graph_path = process_dir / "process_graph.json"

    graph = json.loads(graph_path.read_text(encoding="utf-8"))

    problems = check_runtime_reference(process_dir, graph)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1

    nodes = list(find_run_udf_nodes(graph))
    if len(nodes) != 1:
        raise SystemExit(
            f"{graph_path}: expected exactly one run_udf node, found {len(nodes)}"
        )
    nodes[0].setdefault("arguments", {})["udf"] = udf_path.read_text(encoding="utf-8")

    text = json.dumps(graph, indent=2, ensure_ascii=False) + "\n"

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
