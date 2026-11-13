#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["jinja2"]
# ///
"""Upload a model release (process graphs, UDF code, runtimes) to Zenodo.

Zenodo records can hold many files, which is what the published layout needs:

    https://doi.org/<doi>/models/tessera_v2/process_graph.json
    https://doi.org/<doi>/runtimes/py311-torch231.zip

That rules out Zenodo's automatic GitHub integration, which only archives a
repository snapshot under its own paths. This script creates a deposition,
reserves its DOI before uploading, renders every process graph with the DOI as
the base URL, uploads all files through the bucket API, and publishes the
record last so a partially uploaded record never goes live.

The REST API used here is documented at <https://developers.zenodo.org/>.
The token needs the ``deposit:write`` and ``deposit:actions`` scopes and is
read from ``ZENODO_ACCESS_TOKEN`` by default.

Usage:
    uv run scripts/publish-release.py \\
        --title "fair-geo-fm v0.2" --version v0.2 \\
        --creators-json '[{"name":"Loos, Daniel"}]'

Runtime archives (``runtimes/*/dependencies.zip``) must already be built, e.g.
with ``scripts/create-python-dependency-archive.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from process_graphs import render, validate_runtime_reference

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CREATORS = [{"name": "Loos, Daniel"}]


def api_request(method: str, url: str, token: str, *, body: dict | None = None,
                data: bytes | None = None, content_type: str | None = None) -> dict:
    """Call the Zenodo API and return the decoded JSON response."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        content_type = "application/json"
    if data is not None:
        headers["Content-Type"] = content_type or "application/octet-stream"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise SystemExit(f"{method} {url} failed: {error.code} {error.reason}\n{detail}")
    return json.loads(payload) if payload else {}


def stage_files(base_url: str, staging: Path) -> list[tuple[str, Path]]:
    """Render graphs and collect code/runtime files as (Zenodo key, path)."""
    staged: list[tuple[str, Path]] = []

    models = REPO_ROOT / "models"
    for process_dir in sorted(path for path in models.iterdir() if path.is_dir()):
        template = process_dir / "process_graph.json.j2"
        if not template.is_file():
            continue
        text, graph, graph_url, archive_url = render(process_dir, base_url)
        problems = validate_runtime_reference(process_dir, graph, archive_url)
        if problems:
            raise SystemExit("\n".join(problems))
        target = staging / "models" / process_dir.name / "process_graph.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"rendered {graph_url} -> {target.relative_to(staging)}")
        print(f"  runtime archive: {archive_url}")
        staged.append((target.relative_to(staging).as_posix(), target))

        for name in ("udf.py", "example.py", "runtime.txt"):
            source = process_dir / name
            if source.is_file():
                relative = f"models/{process_dir.name}/{name}"
                staged.append((relative, source))

    runtimes = REPO_ROOT / "runtimes"
    for runtime_dir in sorted(path for path in runtimes.iterdir() if path.is_dir()):
        archive = runtime_dir / "dependencies.zip"
        if not archive.is_file():
            raise SystemExit(
                f"{archive} not found; build it with "
                f"scripts/create-python-dependency-archive.py before releasing"
            )
        staged.append((f"runtimes/{runtime_dir.name}.zip", archive))

    return staged


def upload(bucket: str, key: str, source: Path, token: str) -> None:
    url = f"{bucket.rstrip('/')}/{urllib.parse.quote(key, safe='/')}"
    size = source.stat().st_size
    print(f"uploading {key} ({size} bytes)")
    api_request("PUT", url, token, data=source.read_bytes())


def write_github_output(values: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for name, value in values.items():
            fh.write(f"{name}={value}\n")


def write_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--title", required=True, help="Zenodo record title")
    parser.add_argument("--version", required=True, help="version string, e.g. the release tag")
    parser.add_argument("--description", default="", help="Zenodo record description")
    parser.add_argument(
        "--creators-json", default=json.dumps(DEFAULT_CREATORS),
        help='Zenodo creators as JSON, e.g. \'[{"name":"Loos, Daniel"}]\'',
    )
    parser.add_argument(
        "--api-url", default=os.environ.get("ZENODO_API_URL", "https://zenodo.org/api"),
        help="Zenodo API base (use https://sandbox.zenodo.org/api for a dry run)",
    )
    parser.add_argument(
        "--token-env", default="ZENODO_ACCESS_TOKEN",
        help="environment variable holding the Zenodo access token",
    )
    parser.add_argument(
        "--deposition-id", type=int,
        help="reuse an existing unpublished deposition instead of creating one",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="render and upload, then delete the draft instead of publishing",
    )
    args = parser.parse_args()

    token = os.environ.get(args.token_env)
    if not token:
        raise SystemExit(f"{args.token_env} is not set")

    try:
        creators = json.loads(args.creators_json)
    except json.JSONDecodeError as error:
        raise SystemExit(f"--creators-json is not valid JSON: {error}")
    if not isinstance(creators, list) or not creators:
        raise SystemExit("--creators-json must be a non-empty JSON list")

    description = args.description.strip()
    provenance = (
        f"Released from https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'danlooo/fair-geo-fm')}"
        f"/tree/{args.version}."
    )
    description = f"{description}\n\n{provenance}" if description else provenance

    metadata = {
        "title": args.title,
        "version": args.version,
        "upload_type": "dataset",
        "description": description,
        "creators": creators,
        "publication_date": date.today().isoformat(),
        "access_right": "open",
        "keywords": ["openEO", "geospatial foundation models", "embeddings"],
        "prereserve_doi": True,
    }

    api = args.api_url.rstrip("/")
    if args.deposition_id:
        deposition = api_request(
            "GET", f"{api}/deposit/depositions/{args.deposition_id}", token
        )
    else:
        deposition = api_request(
            "POST", f"{api}/deposit/depositions", token, body={"metadata": metadata}
        )

    deposition_id = deposition["id"]
    doi = (deposition.get("metadata") or {}).get("prereserve_doi", {}).get("doi")
    if not doi:
        raise SystemExit(
            "Zenodo did not return a pre-reserved DOI; refusing to render URLs "
            "that would change after publication"
        )
    base_url = f"https://doi.org/{doi}"
    print(f"deposition {deposition_id}, reserved DOI {doi}")

    staging = Path(tempfile.mkdtemp(prefix="zenodo-release-"))
    try:
        staged = stage_files(base_url, staging)
        bucket = deposition["links"]["bucket"]
        for key, source in staged:
            upload(bucket, key, source, token)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if args.dry_run:
        api_request("DELETE", f"{api}/deposit/depositions/{deposition_id}", token)
        print(f"dry run: deleted draft deposition {deposition_id}")
        published_doi = doi
        record_url = base_url
    else:
        published = api_request(
            "POST", f"{api}/deposit/depositions/{deposition_id}/actions/publish", token
        )
        published_doi = published.get("doi") or doi
        record_url = published.get("links", {}).get("record_html") or f"https://doi.org/{published_doi}"
        print(f"published {published_doi}")

    files = "\n".join(
        f"- [{'https://doi.org/' + published_doi + '/' + key}]"
        f"(https://doi.org/{published_doi}/{key})"
        for key, _source in staged
    )
    write_summary(
        [
            "### Zenodo release",
            "",
            f"- deposition: `{deposition_id}`",
            f"- DOI: <https://doi.org/{published_doi}>",
            f"- record: {record_url}",
            f"- dry run: `{args.dry_run}`",
            "",
            files,
        ]
    )
    write_github_output(
        {"doi": published_doi, "deposition_id": str(deposition_id), "record_url": record_url}
    )


if __name__ == "__main__":
    main()
