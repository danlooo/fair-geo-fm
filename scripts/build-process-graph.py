#!/usr/bin/env python3
"""Inline a process directory's ``udf.py`` into its ``process_graph.json``.

A remote process definition has to carry the UDF as inline source: a relative
path in ``udf`` is resolved against the *user's* server-side workspace, not the
repository, and URL UDFs are backend-specific. So ``udf.py`` is the source of
truth for the code, ``process_graph.json`` is the source of truth for the graph
structure, and this script keeps the ``udf`` argument of the ``run_udf`` node in
sync with the file.

If the directory also has a ``runtime.txt``, it holds the one
``udf-dependency-archives`` URL the process needs, complete with its ``#``
fragment (e.g. ``https://.../py311-torch231.zip#feature_deps``). The script
requires that URL to be listed verbatim in
``default_job_options["udf-dependency-archives"]`` and the asset name in it
(``py311-torch231``) to match a directory under ``runtimes/``, so the runtime
directory, the published archive and the process graph cannot drift apart.

Usage:
    uv run scripts/build-process-graph.py models/tessera_v2
    uv run scripts/build-process-graph.py models/tessera_v2 --check

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


def runtime_name(url: str) -> str:
    """Return the runtime directory name an archive URL maps to.

    ``.../py311-torch231.zip#feature_deps`` -> ``py311-torch231``.
    """
    path = url.split("#", 1)[0].rstrip("/")
    name = path.rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".zip") else name


def check_runtime_reference(process_dir: Path, graph: dict) -> list[str]:
    """Validate ``runtime.txt`` against ``runtimes/`` and the job options."""
    runtime_file = process_dir / "runtime.txt"
    if not runtime_file.is_file():
        return []

    urls = [line.strip() for line in runtime_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(urls) != 1:
        return [f"{runtime_file}: expected exactly one dependency archive URL, found {urls}"]
    archive_url = urls[0]

    problems = []
    name = runtime_name(archive_url)
    repo_root = process_dir.resolve().parent.parent
    if not (repo_root / "runtimes" / name).is_dir():
        problems.append(
            f"{runtime_file} refers to runtime {name!r}, but runtimes/{name}/ does not exist"
        )

    archives = (graph.get("default_job_options") or {}).get("udf-dependency-archives") or []
    if not archives:
        problems.append(f"{runtime_file}: default_job_options has no udf-dependency-archives")
    elif archive_url not in archives:
        problems.append(
            f"{runtime_file} lists {archive_url!r}, which is not in "
            f"default_job_options['udf-dependency-archives']: {archives}"
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
