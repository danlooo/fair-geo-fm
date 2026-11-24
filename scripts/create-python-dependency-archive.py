#!/usr/bin/env python3
"""Build an openEO UDF dependency archive from a requirements file.

Each directory under ``runtimes/`` describes one interpreter/stack combination
with a single ``requirements.txt``. This script turns that file into
``dependencies.zip``, which a process references from
``udf-dependency-archives``:

    "udf-dependency-archives": [
        "https://<host>/<path>/dependencies.zip#feature_deps"
    ]

Usage:
    uv run scripts/create-python-dependency-archive.py \\
        --requirements runtimes/py311-torch231/requirements.txt \\
        --output-file runtimes/py311-torch231/dependencies.zip

The target Python version comes from the requirements file's directory name
(``py311`` -> ``3.11``) and wheels are cross-installed for the openEO UDF
sandbox, so no local interpreter of that version is needed. An optional
``platform.txt`` next to the requirements overrides the wheel platform, and an
optional ``no-deps.txt`` installs only the pinned packages (list transitive
dependencies explicitly; used when a wheel depends on a sandbox package).
``requirements.txt`` may carry pip index directives (e.g. ``--extra-index-url``
for the CPU torch wheels); they are honoured by ``uv pip install -r``.

The archive is deterministic: entries sorted, timestamps fixed, contents at the
root (no top-level directory), which is what ``udf-dependency-archives``
extracts into the UDF working directory. Build-only trees are dropped; the
package set is printed so it can be compared with the CDSE
``torch_deps_python311.zip`` (whose sha256 differs by design: different build
metadata, timestamps and possibly transitive versions).

Only the packages in ``requirements.txt`` are included. numpy, pandas, xarray
and openeo already come from the sandbox; shipping them here would shadow the
sandbox versions because the extraction directory is prepended to ``sys.path``.

Note: adding an index to ``requirements.txt`` may require adding its host to
``.devcontainer/allowed-domains.txt``. The firewall resolves that file when the
container starts, so rebuild the devcontainer before running this script.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

REQUIREMENTS_NAME = "requirements.txt"
# Optional per-runtime override of the target platform, one line such as
# ``x86_64-manylinux_2_28``. Some wheels (onnxruntime) are not published for
# the conservative manylinux2014 default.
PLATFORM_NAME = "platform.txt"
# Optional per-runtime marker: install only the packages pinned in
# requirements.txt, without resolving their dependencies. Needed when a wheel
# depends on a sandbox package (onnxruntime depends on numpy): list the other
# transitive packages explicitly instead of shadowing the sandbox's numpy.
NO_DEPS_NAME = "no-deps.txt"

# The openEO UDF sandbox this archive targets.
UV_PLATFORM = "x86_64-manylinux2014"

# Provided by the openEO UDF sandbox; never ship them in a dependency archive.
SANDBOX_PACKAGES = {"numpy", "pandas", "xarray", "openeo", "scipy", "dask"}

# Build-only or test-only trees, dropped from every installed package.
PRUNE_DIRS = ("include", "test", "tests")


def target_python_version(requirements: Path) -> str:
    match = re.match(r"py(\d)(\d+)", requirements.parent.name)
    if not match:
        raise SystemExit(
            f"cannot derive a Python version from {str(requirements.parent)!r}; "
            "keep the file in a directory named like py311-torch231"
        )
    return f"{match.group(1)}.{match.group(2)}"


def target_platform(requirements: Path) -> str:
    """The wheel platform for this runtime, or the sandbox default.

    A ``platform.txt`` next to ``requirements.txt`` overrides it, which is
    needed when a pinned wheel has no manylinux2014 publication.
    """
    override = requirements.parent / PLATFORM_NAME
    if override.is_file():
        value = override.read_text(encoding="utf-8").strip()
        if value:
            return value
    return UV_PLATFORM


def install(
    staging: Path,
    requirements: Path,
    python_version: str,
    platform: str,
    no_deps: bool,
) -> None:
    arguments = [
        "uv", "pip", "install",
        "--target", str(staging),
        "--python-version", python_version,
        "--python-platform", platform,
        "--only-binary", ":all:",
        "-r", str(requirements),
    ]
    if no_deps:
        arguments.append("--no-deps")
    subprocess.run(arguments, check=True)


def prune(staging: Path) -> None:
    # uv leaves a lock file in the target directory.
    (staging / ".lock").unlink(missing_ok=True)
    for package in staging.iterdir():
        if not package.is_dir() or package.name.endswith((".dist-info", ".data")):
            continue
        for name in PRUNE_DIRS:
            shutil.rmtree(package / name, ignore_errors=True)
    # Console-script wrappers, not used inside a UDF.
    shutil.rmtree(staging / "bin", ignore_errors=True)
    for path in list(staging.rglob("__pycache__")):
        shutil.rmtree(path, ignore_errors=True)
    for path in staging.rglob("*.pyc"):
        path.unlink(missing_ok=True)


def dist_versions(staging: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for meta in staging.glob("*.dist-info/METADATA"):
        name = version = None
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Name: "):
                name = line[len("Name: "):].strip()
            elif line.startswith("Version: "):
                version = line[len("Version: "):].strip()
            if name and version:
                break
        if name and version:
            versions[name] = version
    return dict(sorted(versions.items()))


def check_no_sandbox_packages(versions: dict[str, str]) -> None:
    shadowed = SANDBOX_PACKAGES & {name.lower() for name in versions}
    if shadowed:
        raise SystemExit(
            "refusing to ship packages provided by the UDF sandbox: "
            + ", ".join(sorted(shadowed))
        )


def zip_tree(staging: Path, out: Path) -> int:
    files = sorted(p for p in staging.rglob("*") if p.is_file())
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(staging).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            with path.open("rb") as src:
                zf.writestr(info, src.read(), compresslevel=9)
    return len(files)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--requirements", type=Path, required=True,
        help=f"path to the {REQUIREMENTS_NAME} to install",
    )
    parser.add_argument(
        "--output-file", type=Path, required=True,
        help="zip to write",
    )
    args = parser.parse_args()

    requirements = args.requirements.resolve()
    if not requirements.is_file():
        raise SystemExit(f"{requirements} not found")
    python_version = target_python_version(requirements)

    out = args.output_file.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix=f"{requirements.parent.name}-"))
    try:
        platform = target_platform(requirements)
        no_deps = (requirements.parent / NO_DEPS_NAME).is_file()
        print(
            f"installing {requirements} for Python {python_version} / {platform}"
            + (" (no dependencies)" if no_deps else "")
        )
        install(staging, requirements, python_version, platform, no_deps)

        prune(staging)
        versions = dist_versions(staging)
        check_no_sandbox_packages(versions)

        n_files = zip_tree(staging, out)
        raw_size = sum(p.stat().st_size for p in staging.rglob("*") if p.is_file())

        print()
        print(f"files:    {n_files}")
        print(f"unzipped: {human(raw_size)}")
        print(f"zip:      {human(out.stat().st_size)}  {out}")
        print(f"sha256:   {sha256(out)}")
        print()
        print("packages:")
        for name, version in versions.items():
            print(f"  {name}=={version}")
        print()
        print("reference it as a job option:")
        print('  "udf-dependency-archives": ["https://<host>/<path>/'
              f'{out.name}#feature_deps"]')
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    main()
