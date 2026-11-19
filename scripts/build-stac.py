#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["jinja2"]
# ///
"""Generate the static STAC catalog for the models in ``models/``.

Each model directory with a ``process_graph.json.j2`` becomes one STAC Item in
the ``fair-geo-fm-models`` Collection, described with the MLM, processing,
scientific and version extensions. ``stac.json`` supplies the model-specific
MLM fields (architecture, tasks, input/output), ``config.json`` the display
metadata, and the rendered process graph the executable reference and the
weights URL.

The catalog is written to ``stac/`` by default (git-ignored):

    uv run scripts/build-stac.py
    uv run scripts/build-stac.py --check                        # validate only
    uv run scripts/build-stac.py --base-url <url> --version v0.2 --doi <doi>

``publish-release.py`` writes the same catalog into the Zenodo record, with
asset URLs pointing at the record's file base; ``publish-staging.yml`` renders the
staging catalog under ``stac/`` for the Pages site.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from stac_catalog import build_catalog, write_catalog

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://danlooo.github.io/fair-geo-fm/main"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=REPO_ROOT / "models",
        help="directory containing one subdirectory per model (default: models/)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("stac"),
        help="output directory for the catalog (default: stac/)",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"base URL of the published model files (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--docs-base-url",
        default=None,
        help="base URL of the versioned documentation (default: --base-url)",
    )
    parser.add_argument(
        "--version",
        default="main",
        help="version label stored in the Items (default: main)",
    )
    parser.add_argument(
        "--doi",
        default=None,
        help="release DOI; omitted for a staging render",
    )
    parser.add_argument(
        "--datetime",
        dest="datetime_value",
        default=None,
        help="Item datetime override, RFC 3339 (default: stac.json datetime)",
    )
    parser.add_argument(
        "--source-archive",
        default=None,
        help="key of the release source archive, relative to --base-url",
    )
    parser.add_argument(
        "--zenodo-url",
        default=None,
        help="Zenodo record page rendered into the process graph",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="build into a temporary directory and validate without writing",
    )
    args = parser.parse_args()

    files = build_catalog(
        args.models_dir,
        base_url=args.base_url,
        docs_base_url=args.docs_base_url,
        version=args.version,
        doi=args.doi,
        datetime_value=args.datetime_value,
        source_archive_key=args.source_archive,
        zenodo_url=args.zenodo_url,
    )

    if args.check:
        with tempfile.TemporaryDirectory(prefix="fair-geo-fm-stac-") as temporary:
            written = write_catalog(files, Path(temporary))
        print(f"STAC catalog valid: {len(written)} file(s)")
        return

    for path in write_catalog(files, args.output):
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
