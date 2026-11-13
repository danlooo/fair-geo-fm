#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["jinja2", "requests"]
# ///
"""Upload a model release (process graphs and runtimes) to Zenodo.

Zenodo records can hold many files, which is what the published layout needs:

    https://zenodo.org/records/<record-id>/files/models/tessera_v2/process_graph.json
    https://zenodo.org/records/<record-id>/files/runtimes/py311-torch231.zip
    https://zenodo.org/records/<record-id>/files/danlooo/fair-geo-fm-v0.2.zip

The last file is the complete source tree exported with ``git archive`` from
the ref being released. It is uploaded under the same path Zenodo's GitHub
integration would use, so the record is self-contained and citable.

That rules out Zenodo's automatic GitHub integration, which only archives a
repository snapshot under its own paths. This script creates a draft record
through Zenodo's InvenioRDM API, reserves its DOI before uploading, renders
every process graph with the record's file URL as the base URL, uploads all
files, and publishes the record last so a partially uploaded record never goes
live.

The old deposition bucket API (``PUT /api/files/<bucket>/<key>``) is no longer
served by Zenodo; files are uploaded to the draft record instead, as documented
at <https://inveniordm.docs.cern.ch/reference/rest_api_drafts_records/>.

The token needs the ``deposit:write`` and ``deposit:actions`` scopes and is
read from ``ZENODO_ACCESS_TOKEN`` by default.

Usage:
    uv run scripts/publish-release.py \\
        --title "fair-geo-fm v0.2" --version v0.2 \\
        --creators-json '[{"name":"Loos, Daniel","orcid":"0000-0002-4024-4443"}]'

Runtime archives (``runtimes/*/dependencies.zip``) must already be built, e.g.
with ``scripts/create-python-dependency-archive.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import date
from pathlib import Path

import requests

from process_graphs import render, validate_runtime_reference

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CREATORS = [{"name": "Loos, Daniel", "orcid": "0000-0002-4024-4443"}]
# Zenodo serves ``application/json`` in its retired deposition shape by
# default; this media type selects the native InvenioRDM representation.
INVENIO_ACCEPT = "application/vnd.inveniordm.v1+json"


class ApiError(Exception):
    """A non-2xx response from the Zenodo API."""

    def __init__(self, method: str, url: str, status: int, body: str):
        super().__init__(f"{method} {url} failed: {status}\n{body}")
        self.method = method
        self.url = url
        self.status = status
        self.body = body


def api_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    json_body: dict | list | None = None,
    data: object | None = None,
    content_type: str | None = None,
    accept: str = "application/json",
    timeout: int = 300,
) -> dict:
    """Call the Zenodo API and return the decoded JSON response."""
    headers = {"Accept": accept}
    if content_type:
        headers["Content-Type"] = content_type

    response = session.request(
        method, url, json=json_body, data=data, headers=headers, timeout=timeout
    )
    if not response.ok:
        raise ApiError(method, url, response.status_code, response.text)
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


def draft_doi(draft: dict) -> str | None:
    """The DOI reserved for a draft, if any.

    Handles both the native InvenioRDM representation and Zenodo's legacy
    ``application/json`` shape, which exposes the DOI at the top level.
    """
    doi = (draft.get("pids") or {}).get("doi") or {}
    identifier = doi.get("identifier") if isinstance(doi, dict) else None
    return identifier or draft.get("doi") or (draft.get("metadata") or {}).get("doi")


def creators_metadata(creators: list[dict]) -> list[dict]:
    """Translate ``[{name: "Loos, Daniel", orcid: "..."}]`` to the InvenioRDM shape.

    ``orcid`` is optional and becomes a creator identifier, so Zenodo links the
    record to the ORCID profile.
    """
    translated = []
    for creator in creators:
        if "person_or_org" in creator:
            translated.append(creator)
            continue
        person = {"type": "personal", "name": creator["name"]}
        if ", " in creator["name"]:
            family_name, given_name = creator["name"].split(", ", 1)
            person["family_name"] = family_name
            person["given_name"] = given_name
        if creator.get("orcid"):
            person["identifiers"] = [
                {"identifier": creator["orcid"], "scheme": "orcid"}
            ]
        translated.append({"person_or_org": person})
    return translated


def metadata_input(meta: dict) -> dict:
    """Convert a native draft's metadata into an updateable request body."""
    payload = {
        "title": meta.get("title"),
        "publication_date": meta.get("publication_date"),
        "resource_type": {"id": (meta.get("resource_type") or {}).get("id")},
        "creators": meta.get("creators") or [],
        "publisher": meta.get("publisher") or "Zenodo",
    }
    for key in ("version", "description"):
        if meta.get(key) is not None:
            payload[key] = meta[key]
    subjects = [
        {"subject": subject["subject"]}
        for subject in meta.get("subjects") or []
        if isinstance(subject, dict) and subject.get("subject")
    ]
    if subjects:
        payload["subjects"] = subjects
    rights = [
        {"id": right["id"]}
        for right in meta.get("rights") or []
        if isinstance(right, dict) and right.get("id")
    ]
    if rights:
        payload["rights"] = rights
    return payload


def source_archive_key(version: str) -> str:
    """Zenodo file key for the source archive, e.g. ``danlooo/fair-geo-fm-v0.2.zip``."""
    slug = os.environ.get("GITHUB_REPOSITORY", "danlooo/fair-geo-fm")
    # Tags may contain slashes; keep the archive a single directory below the
    # record root, like GitHub's own source archives.
    safe_version = version.replace("/", "-")
    return f"{slug}-{safe_version}.zip"


def stage_source_archive(version: str, staging: Path) -> tuple[str, Path]:
    """Export the tracked sources at HEAD as a zip for the Zenodo record.

    ``git archive`` only includes committed files, so the archive matches the
    release ref the workflow checked out and excludes build output such as
    ``node_modules`` or ``runtimes/*/dependencies.zip``.
    """
    key = source_archive_key(version)
    prefix = f"{Path(key).stem}/"
    result = subprocess.run(
        ["git", "archive", "--format=zip", f"--prefix={prefix}", "HEAD"],
        cwd=REPO_ROOT, capture_output=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            "git archive failed; the source archive must be exported from a "
            f"git checkout:\n{result.stderr.decode('utf-8', 'replace')}"
        )
    target = staging / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(result.stdout)
    print(f"exported source archive {key} -> {target.relative_to(staging)}")
    return key, target


def stage_files(base_url: str, staging: Path, version: str) -> list[tuple[str, Path]]:
    """Render graphs and collect code/runtime/source files as (Zenodo key, path)."""
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
        # The UDF source is embedded in the rendered graph. The record keeps
        # only the graph per model; udf.py/example.py/runtime.txt stay in the
        # source archive.
        staged.append((target.relative_to(staging).as_posix(), target))

    runtimes = REPO_ROOT / "runtimes"
    for runtime_dir in sorted(path for path in runtimes.iterdir() if path.is_dir()):
        archive = runtime_dir / "dependencies.zip"
        if not archive.is_file():
            raise SystemExit(
                f"{archive} not found; build it with "
                f"scripts/create-python-dependency-archive.py before releasing"
            )
        staged.append((f"runtimes/{runtime_dir.name}.zip", archive))

    staged.append(stage_source_archive(version, staging))

    return staged


def site_url(api: str) -> str:
    """The Zenodo web root for an API base URL."""
    return api[: -len("/api")] if api.endswith("/api") else api


def record_files_url(api: str, record_id: str) -> str:
    """Base URL of a record's files, used as the rendered process-graph base."""
    return f"{site_url(api)}/records/{record_id}/files"


def file_url(api: str, record_id: str, key: str) -> str:
    """Public URL for a file in the (draft) record."""
    quoted = urllib.parse.quote(key, safe="/")
    return f"{record_files_url(api, record_id)}/{quoted}"


def upload_files(
    session: requests.Session,
    api: str,
    record_id: str,
    staged: list[tuple[str, Path]],
) -> None:
    """Initialize, upload and commit every staged file on the draft record."""
    files_url = f"{api}/records/{record_id}/draft/files"

    # A retry may target a draft that already has some of these files. Remove
    # them first so the upload can be initialized again.
    listing = api_request(session, "GET", files_url)
    existing = {entry["key"] for entry in listing.get("entries", [])}
    for key, _source in staged:
        if key in existing:
            quoted = urllib.parse.quote(key, safe="/")
            print(f"replacing existing draft file {key}")
            api_request(session, "DELETE", f"{files_url}/{quoted}")

    # One request initializes all keys; uploads then stream each file so large
    # runtime archives are never read into memory.
    api_request(
        session,
        "POST",
        files_url,
        json_body=[{"key": key} for key, _source in staged],
    )

    for key, source in staged:
        quoted = urllib.parse.quote(key, safe="/")
        content_url = f"{files_url}/{quoted}/content"
        size = source.stat().st_size
        print(f"uploading {key} ({size} bytes)")
        with source.open("rb") as handle:
            api_request(
                session,
                "PUT",
                content_url,
                data=handle,
                content_type="application/octet-stream",
                timeout=1800,
            )
        api_request(session, "POST", f"{files_url}/{quoted}/commit")


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
        help=(
            "Zenodo creators as JSON, e.g. "
            '\'[{"name": "Loos, Daniel", "orcid": "0000-0002-4024-4443"}]\''
        ),
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
        help="reuse an existing unpublished draft record instead of creating one",
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
        "description": description,
        "creators": creators_metadata(creators),
        "publication_date": date.today().isoformat(),
        "resource_type": {"id": "dataset"},
        "publisher": "Zenodo",
        # Zenodo requires a license for open records (legacy API defaulted to
        # CC0 for datasets) and uses ``subjects`` for keyword strings.
        "rights": [{"id": "cc0-1.0"}],
        "subjects": [
            {"subject": keyword}
            for keyword in ("openEO", "geospatial foundation models", "embeddings")
        ],
    }

    api = args.api_url.rstrip("/")
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "User-Agent": (
            "fair-geo-fm-publish-release/1.0 "
            f"(+https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'danlooo/fair-geo-fm')})"
        ),
    })

    try:
        if args.deposition_id:
            try:
                draft = api_request(
                    session, "GET", f"{api}/records/{args.deposition_id}/draft",
                    accept=INVENIO_ACCEPT,
                )
            except ApiError as error:
                if error.status != 404:
                    raise
                draft = {}
            # Zenodo refuses to register a DOI without a publisher. Drafts
            # created before this field was set need it added before retrying.
            if draft.get("metadata") and not draft["metadata"].get("publisher"):
                draft = api_request(
                    session, "PUT", f"{api}/records/{args.deposition_id}/draft",
                    json_body={"metadata": metadata_input(draft["metadata"])},
                    accept=INVENIO_ACCEPT,
                )
            doi = draft_doi(draft)
            if not doi:
                # A draft created through the retired deposition API can still
                # be read there, and carries its reserved DOI in its metadata.
                legacy = api_request(
                    session, "GET", f"{api}/deposit/depositions/{args.deposition_id}",
                    accept="application/json",
                )
                doi = (
                    (legacy.get("metadata") or {}).get("prereserve_doi") or {}
                ).get("doi")
        else:
            draft = api_request(
                session, "POST", f"{api}/records",
                json_body={
                    "access": {"record": "public", "files": "public"},
                    "files": {"enabled": True},
                    "metadata": metadata,
                },
                accept=INVENIO_ACCEPT,
            )
            doi = draft_doi(draft)
            if not doi:
                try:
                    draft = api_request(
                        session, "POST",
                        f"{api}/records/{draft['id']}/draft/pids/doi",
                        accept=INVENIO_ACCEPT,
                    )
                except ApiError as error:
                    # Zenodo may have reserved the DOI already; re-read it.
                    if error.status != 400 or "already exists" not in error.body:
                        raise
                    draft = api_request(
                        session, "GET", f"{api}/records/{draft['id']}/draft",
                        accept=INVENIO_ACCEPT,
                    )
                doi = draft_doi(draft)

        record_id = str(draft.get("id") or args.deposition_id)
        if not doi:
            raise SystemExit(
                "Zenodo did not return a pre-reserved DOI; refusing to publish "
                "a record without a citable identifier"
            )
        print(f"draft record {record_id}, reserved DOI {doi}")

        # Zenodo does not serve files under the DOI path (``doi.org/<doi>/...``
        # is treated as a different DOI), so render links against the record
        # itself. The DOI remains the citable identifier for the release.
        base_url = record_files_url(api, record_id)
        staging = Path(tempfile.mkdtemp(prefix="zenodo-release-"))
        try:
            staged = stage_files(base_url, staging, args.version)
            upload_files(session, api, record_id, staged)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        if args.dry_run:
            api_request(session, "DELETE", f"{api}/records/{record_id}/draft")
            print(f"dry run: deleted draft record {record_id}")
            published_doi = doi
            record_url = f"{site_url(api)}/records/{record_id}"
        else:
            published = api_request(
                session, "POST", f"{api}/records/{record_id}/draft/actions/publish",
                accept=INVENIO_ACCEPT,
            )
            published_doi = draft_doi(published) or published.get("doi") or doi
            record_url = (
                (published.get("links") or {}).get("self_html")
                or f"{site_url(api)}/records/{record_id}"
            )
            print(f"published {published_doi}")

        files = "\n".join(
            f"- [{file_url(api, record_id, key)}]({file_url(api, record_id, key)})"
            for key, _source in staged
        )
        write_summary(
            [
                "### Zenodo release",
                "",
                f"- record id: `{record_id}`",
                f"- DOI: <https://doi.org/{published_doi}>",
                f"- record: {record_url}",
                f"- dry run: `{args.dry_run}`",
                "",
                files,
            ]
        )
        write_github_output(
            {
                "doi": published_doi,
                "deposition_id": record_id,
                "record_url": record_url,
            }
        )
    except ApiError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
