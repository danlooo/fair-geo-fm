#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "jinja2",
#     "netcdf4",
#     "numpy",
#     "openeo>=0.41.0",
#     "requests",
#     "xarray",
# ]
# ///
"""Run every model's documented openEO example on CDSE as a release smoke test.

For each ``models/*/`` directory the process graph is rendered from its
template, the example values from ``config.json`` are substituted for the
process parameters, and the graph is submitted as a batch job to
``openeo.dataspace.copernicus.eu``. The job result is downloaded and checked
for the expected embedding dimensions and some finite values, so a release
cannot publish a model whose example does not run.

Authentication uses the OIDC client credentials flow, which is the only
non-interactive option offered by CDSE (``/credentials/basic`` is not
available). Provide the client id and secret through the environment:

    OPENEO_AUTH_CLIENT_ID
    OPENEO_AUTH_CLIENT_SECRET
    OPENEO_AUTH_PROVIDER_ID   (optional; CDSE is the only provider)

Use a CDSE service account (or a Sentinel Hub dashboard OAuth client) as
described in
<https://documentation.dataspace.copernicus.eu/APIs/openEO/authentication/client_credentials.html>.
The service account has its own job list and processing credits, so ask CDSE
support to link them to the project account.

The graph is submitted inline instead of through its ``namespace`` URL for a
release: a draft Zenodo record is private and its files cannot be fetched by
the CDSE back-end. ``--base-url`` therefore only controls the public runtime
archive URL rendered into ``udf-dependency-archives``; the staging site
(``https://danlooo.github.io/fair-geo-fm/main``) is reachable during the
release workflow.

``--client`` tests the usage examples a model page documents instead of the
inline graph. The snippets are read from the page rendered by ``model.md.j2``,
so a broken documented example fails the test:

- ``web``: submit the Web editor's process-graph JSON, which resolves the
  model through its ``namespace`` URL (the ``remote-process-definition``
  extension that CDSE supports).
- ``python``: run the page's Python snippet with the official Python client in
  a subprocess.
- ``r``: run the page's R snippet with ``Rscript`` and the official R client.

Only the interactive login is adapted for the non-interactive CI run: the
Python snippet's ``authenticate_oidc()`` picks up the client-credentials flow
from ``OPENEO_AUTH_METHOD``, and the R snippet's bare ``login()`` is replaced
with the equivalent ``login(config = list(grant_type = "client_credentials"...))``
because the R client has no environment-driven client-credentials mode.
Everything else runs exactly as documented.

The ``web``, ``python`` and ``r`` examples resolve the graph over the network,
so ``--base-url`` must point at a publication target that is readable by the
client and the back-end. Use the staging site (the default) for a release test;
a draft Zenodo record is private, and staging is overwritten on every deploy,
so this checks the deployed example rather than the working-tree template.

Failed jobs are kept (their logs and results remain on CDSE for inspection);
successful inline jobs are deleted unless ``--keep-jobs`` is given. Jobs
created by the Python and R snippets are left on CDSE, because the snippets do
not expose their job ids.

Usage:
    OPENEO_AUTH_CLIENT_ID=... OPENEO_AUTH_CLIENT_SECRET=... \\
        uv run scripts/test-models.py

    uv run scripts/test-models.py --model tessera_v1 --keep-jobs

    uv run scripts/test-models.py --model alphaearth \\
        --client web --client python --client r
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import numpy as np
import openeo
import requests
import xarray as xr
from process_graphs import DEFAULT_BASE_URL, SITE_BASE_URL, render, render_readme

DEFAULT_BACKEND = "https://openeo.dataspace.copernicus.eu"
REPO_ROOT = Path(__file__).resolve().parent.parent
TERMINAL_STATUSES = {"finished", "error", "canceled"}
SPATIAL_DIMENSIONS = ("x", "y", "lon", "lat")
# Client names and the Markdown code-fence language of their documented example.
CLIENTS = {"web": "json", "python": "python", "r": "r"}

# The matrix runs all models in parallel against the same service account, so
# the backend answers some calls with ``429 Too Many Requests``. The client's
# default retry policy does not cover that: urllib3 only retries idempotent
# methods, and starting a job is a POST. Allow every method and honour the
# ``Retry-After`` header the backend sends.
RATE_LIMIT_RETRY = {
    "total": 5,
    "backoff_factor": 2.0,
    "status_forcelist": [429, 502, 503, 504],
    "allowed_methods": None,
}


def connect(backend: str):
    """Return an authenticated connection using client credentials."""
    client_id = os.environ.get("OPENEO_AUTH_CLIENT_ID")
    client_secret = os.environ.get("OPENEO_AUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit(
            "OPENEO_AUTH_CLIENT_ID and OPENEO_AUTH_CLIENT_SECRET must be set"
        )

    connection = openeo.connect(backend, retry=RATE_LIMIT_RETRY)
    arguments = {"client_id": client_id, "client_secret": client_secret}
    if provider_id := os.environ.get("OPENEO_AUTH_PROVIDER_ID"):
        arguments["provider_id"] = provider_id
    connection.authenticate_oidc_client_credentials(**arguments)

    account = connection.describe_account()
    print(f"authenticated as {account.get('user_id', 'unknown user')}")
    return connection


def discover_models(repo_root: Path, selected: list[str] | None) -> list[Path]:
    """Model directories that have both a config and a graph template."""
    models = [
        path
        for path in sorted((repo_root / "models").iterdir())
        if path.is_dir()
        and (path / "config.json").is_file()
        and (path / "process_graph.json.j2").is_file()
    ]
    if selected:
        known = {path.name for path in models}
        unknown = sorted(set(selected) - known)
        if unknown:
            raise SystemExit(f"unknown model(s): {', '.join(unknown)}")
        models = [path for path in models if path.name in selected]
    if not models:
        raise SystemExit("no model directories with a process graph template found")
    return models


def parameter_values(config: dict) -> dict:
    """The example values the documentation shows for a model.

    Required parameters carry an ``example``; optional ones a
    ``default_value``. The values are published in the process graph's
    parameter schemas as well, but reading them from ``config.json`` keeps the
    smoke test on the same values a user copies from the model page.
    """
    values = {}
    for parameter in config.get("parameters", []):
        name = parameter["name"]
        if "example" in parameter:
            values[name] = parameter["example"]
        elif "default_value" in parameter:
            values[name] = parameter["default_value"]
        elif parameter.get("required"):
            raise ValueError(f"required parameter {name!r} has no example")
    return values


def substitute_parameters(node, values: dict):
    """Replace ``from_parameter`` nodes for the process's own parameters.

    ``from_parameter`` is also used inside callback process graphs, where it
    refers to the callback's ``x``/``value``/``data`` argument rather than a
    process parameter. Only names present in ``values`` are substituted, so
    those callback references are left untouched.
    """
    if isinstance(node, dict):
        if set(node) == {"from_parameter"} and node["from_parameter"] in values:
            return copy.deepcopy(values[node["from_parameter"]])
        return {key: substitute_parameters(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [substitute_parameters(value, values) for value in node]
    return node


def wait_for_job(job, timeout_seconds: float, poll_interval: float) -> None:
    """Start a job and wait until it reaches a terminal state."""
    started = time.monotonic()
    job.start()
    last_status = None
    while True:
        status = job.status()
        if status != last_status:
            elapsed = int(time.monotonic() - started)
            print(f"  job {job.job_id}: {status} after {elapsed}s")
            last_status = status
        if status in TERMINAL_STATUSES:
            break
        if time.monotonic() - started > timeout_seconds:
            raise TimeoutError(
                f"job {job.job_id} did not finish within {timeout_seconds / 60:.0f} minutes"
            )
        time.sleep(poll_interval)

    if status != "finished":
        print(f"  job {job.job_id} did not finish; fetching its logs")
        try:
            entries = job.logs(audience="all")
        except Exception as error:  # noqa: BLE001 - logs are diagnostic only
            print(f"  could not fetch job logs with audience=all: {error}")
            try:
                entries = job.logs()
            except Exception as fallback_error:  # noqa: BLE001
                print(f"  could not fetch job logs: {fallback_error}")
                entries = []
        for entry in entries:
            print(f"  log: {entry}")
        raise RuntimeError(f"job {job.job_id} finished with status {status!r}")


def band_count(dataset) -> tuple[int, str]:
    """Number of embedding bands in a CDSE NetCDF result.

    The raster writer may expose bands as a ``bands``/``band`` dimension, as a
    coordinate, or (as the CDSE NetCDF writer does for these graphs) as one
    data variable per band with only ``t``/``y``/``x`` dimensions. Accept all
    three representations.
    """
    for name in ("bands", "band"):
        if name in dataset.sizes:
            return dataset.sizes[name], f"{name!r} dimension"
        if name in dataset.coords:
            return dataset.coords[name].size, f"{name!r} coordinate"

    spatial = [
        variable
        for variable in dataset.data_vars.values()
        if sum(name in variable.dims for name in SPATIAL_DIMENSIONS) >= 2
        and variable.dtype.kind in "fiu"
    ]
    if spatial:
        return len(spatial), "one data variable per band"
    raise RuntimeError(
        f"cannot determine the band count; dims={dict(dataset.sizes)}, "
        f"data variables={list(dataset.data_vars)[:10]}"
    )


def download_results(job, download_dir: Path, attempts: int = 4, delay: float = 30.0):
    """Download the job results, retrying transient connection failures.

    Large results are fetched in ranges, but a connection that breaks while a
    response body is streamed surfaces as ``ChunkedEncodingError``/
    ``IncompleteRead`` and is not retried by urllib3 (only failures at request
    time are). Retry here; the client truncates the target file on each
    attempt, so a partial download is not resumed or appended to.
    """
    for attempt in range(1, attempts + 1):
        try:
            return job.get_results().download_files(target=download_dir)
        except requests.exceptions.RequestException as error:
            if attempt == attempts:
                raise
            pause = delay * 2 ** (attempt - 1)
            print(
                f"  download attempt {attempt}/{attempts} failed: {error}; "
                f"retrying in {pause:.0f}s"
            )
            time.sleep(pause)


def download_and_verify(
    job, download_dir: Path, config: dict, *, download_attempts: int = 4
) -> Path:
    """Download the result and check the embedding shape and values."""
    download_dir.mkdir(parents=True, exist_ok=True)
    files = download_results(job, download_dir, attempts=download_attempts)
    result = next(
        (
            path
            for path in files
            if path.suffix.lower() in {".nc", ".nc4", ".netcdf"}
        ),
        None,
    )
    if result is None:
        raise RuntimeError(
            "no NetCDF result found; downloaded: "
            + ", ".join(str(path) for path in files)
        )

    verify_dataset(result, config)
    return result


def verify_dataset(result: Path, config: dict) -> None:
    """Check a downloaded NetCDF result for the expected shape and values."""
    expected = set(config.get("dimensions") or [])
    with xr.open_dataset(result) as dataset:
        sizes = dict(dataset.sizes)
        bands, source = band_count(dataset)
        print(f"  result: {result} dims={sizes}")
        print(
            f"  bands: {bands} ({source}); "
            f"data variables: {len(dataset.data_vars)} "
            f"{list(dataset.data_vars)[:5]}"
        )

        if expected and bands not in expected:
            raise RuntimeError(
                f"{bands} embedding bands ({source}), expected one of "
                f"{sorted(expected)}; dims={sizes}, "
                f"data variables={list(dataset.data_vars)[:10]}"
            )

        spatial_dims = [name for name in SPATIAL_DIMENSIONS if name in sizes]
        if len(spatial_dims) < 2:
            raise RuntimeError(
                f"result has no spatial dimensions ({', '.join(SPATIAL_DIMENSIONS)}): "
                f"{sizes}"
            )
        if any(sizes[name] == 0 for name in spatial_dims):
            raise RuntimeError(f"result has an empty spatial dimension: {sizes}")

        for variable in dataset.data_vars.values():
            # Metadata variables such as ``crs`` or ``spatial_ref`` may be
            # strings; only numeric arrays can carry embedding values.
            if variable.dtype.kind not in "fiu":
                continue
            subset = variable.isel(
                {
                    name: slice(0, 16)
                    for name in variable.dims
                    if name in ("t", "band", "bands", *SPATIAL_DIMENSIONS)
                }
            )
            if np.isfinite(np.asarray(subset.values)).any():
                break
        else:
            raise RuntimeError("result contains no finite values")

    print(f"  verified {result}")


def test_model(
    connection,
    process_dir: Path,
    *,
    base_url: str,
    version: str,
    timeout_seconds: float,
    poll_interval: float,
    download_dir: Path,
    keep_jobs: bool,
    download_attempts: int = 4,
) -> Path:
    """Render, submit and verify one model's documented example."""
    config = json.loads((process_dir / "config.json").read_text(encoding="utf-8"))
    values = parameter_values(config)
    _text, graph, graph_url, archive_url = render(process_dir, base_url, version=version)

    process_graph = substitute_parameters(graph["process_graph"], values)
    if '"from_parameter"' in json.dumps(process_graph):
        # Callback parameters such as run_udf's ``data`` are expected here, but
        # a leftover process parameter would mean the example never supplied it.
        remaining = json.dumps(process_graph).count('"from_parameter"')
        print(f"  {remaining} callback parameter reference(s) left in the graph")

    print(f"  graph: {graph_url}")
    print(f"  runtime archive: {archive_url or '(none)'}")

    job = connection.create_job(
        {"process_graph": process_graph},
        title=f"ci_test_{process_dir.name}",
        description=f"Smoke test of the documented {process_dir.name} example",
        job_options=graph.get("default_job_options") or None,
    )
    print(f"  created job {job.job_id}")

    succeeded = False
    try:
        wait_for_job(job, timeout_seconds, poll_interval)
        result = download_and_verify(
            job, download_dir, config, download_attempts=download_attempts
        )
        succeeded = True
        return result
    finally:
        # Keep failed jobs so their logs and results can be inspected with the
        # service-account credentials after the CI run.
        if keep_jobs or not succeeded:
            reason = "requested" if keep_jobs else "failure"
            print(f"  keeping job {job.job_id} ({reason})")
        else:
            try:
                job.delete()
            except Exception as error:  # noqa: BLE001 - cleanup must not mask the result
                print(f"  could not delete job {job.job_id}: {error}")


def usage_examples(process_dir: Path, base_url: str) -> dict[str, str]:
    """The documented example snippet for every client, from ``model.md.j2``.

    The model page is rendered exactly as the documentation publishes it, and
    the code blocks are read from its ``## Usage`` tabs. Testing the rendered
    page rather than rebuilding the snippets here keeps the test tied to what
    users copy, so a template change that breaks an example fails this test.
    """
    page, _ = render_readme(process_dir, base_url, SITE_BASE_URL)
    if "## Usage" not in page:
        raise RuntimeError(
            f"{process_dir.name}: the rendered model page has no Usage section"
        )
    usage = page.split("## Usage", 1)[1].split("\n## ", 1)[0]
    fences = dict(re.findall(r"```(\w+)\n(.*?)```", usage, re.DOTALL))

    examples = {}
    for client, language in CLIENTS.items():
        if language not in fences:
            raise RuntimeError(
                f"{process_dir.name}: the {client} example has no "
                f"'{language}' code block in the Usage section"
            )
        examples[client] = fences[language]
    return examples


R_CLIENT_CREDENTIALS_LOGIN = """\
login(config = list(
  grant_type = "client_credentials",
  client_id = Sys.getenv("OPENEO_AUTH_CLIENT_ID"),
  secret = Sys.getenv("OPENEO_AUTH_CLIENT_SECRET")
))"""


def authenticate_r_snippet(code: str) -> str:
    """Replace the R example's interactive login with client credentials.

    The documented example calls ``login()``, which opens a browser or prints
    a device code and therefore cannot finish in CI. The R client has no
    environment-driven client-credentials mode, so this substitution is the
    only change made to the snippet; every other line runs as documented. If
    the template ever stops containing exactly one bare ``login()``, fail
    instead of silently testing something else.
    """
    adapted, count = re.subn(
        r"(?m)^[ \t]*login\(\)[ \t]*$", R_CLIENT_CREDENTIALS_LOGIN, code
    )
    if count != 1:
        raise RuntimeError(
            "the R example no longer contains exactly one bare login() line; "
            "update authenticate_r_snippet() in scripts/test-models.py"
        )
    return adapted


def run_snippet(
    client: str,
    code: str,
    *,
    download_dir: Path,
    timeout_seconds: float,
) -> Path:
    """Run a documented Python or R example and return its NetCDF result."""
    if client == "python":
        interpreter = [sys.executable]
        suffix = ".py"
        # authenticate_oidc() switches to the client-credentials flow when this
        # is set, so the documented Python snippet runs unchanged.
        environment = {**os.environ, "OPENEO_AUTH_METHOD": "client_credentials"}
    elif client == "r":
        rscript = shutil.which("Rscript")
        if rscript is None:
            raise RuntimeError(
                "Rscript not found; install R with the openeo and jsonlite packages"
            )
        interpreter = [rscript, "--vanilla"]
        suffix = ".R"
        environment = os.environ.copy()
        code = authenticate_r_snippet(code)
    else:
        raise ValueError(f"{client!r} is not an interpreter-based client")

    download_dir.mkdir(parents=True, exist_ok=True)
    script = download_dir / f"example{suffix}"
    script.write_text(code, encoding="utf-8")
    print(f"  running {script}")
    try:
        completed = subprocess.run(
            [*interpreter, str(script)],
            cwd=download_dir,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"the {client} example did not finish within "
            f"{timeout_seconds / 60:.0f} minutes"
        ) from None
    if completed.stdout:
        print(completed.stdout)
    if completed.returncode != 0:
        raise RuntimeError(
            f"the {client} example exited with {completed.returncode}:\n"
            f"{completed.stderr}"
        )

    results = sorted(
        path
        for path in download_dir.iterdir()
        if path.suffix.lower() in {".nc", ".nc4", ".netcdf"}
    )
    if not results:
        raise RuntimeError(
            f"the {client} example produced no NetCDF file in {download_dir}"
        )
    if len(results) > 1:
        raise RuntimeError(
            "the example produced more than one NetCDF file: "
            + ", ".join(path.name for path in results)
        )
    return results[0]


def test_client(
    connection,
    process_dir: Path,
    client: str,
    *,
    base_url: str,
    timeout_seconds: float,
    poll_interval: float,
    download_dir: Path,
    keep_jobs: bool,
    download_attempts: int = 4,
) -> Path:
    """Run one client's documented example and verify its result."""
    config = json.loads((process_dir / "config.json").read_text(encoding="utf-8"))
    code = usage_examples(process_dir, base_url)[client]

    if client != "web":
        result = run_snippet(
            client,
            code,
            download_dir=download_dir,
            timeout_seconds=timeout_seconds,
        )
        verify_dataset(result, config)
        return result

    # The Web editor POSTs its process-graph JSON as a job. The node's
    # ``namespace`` URL is resolved by the back-end through the
    # remote-process-definition extension.
    graph = json.loads(code)
    job = connection.create_job(
        graph,
        title=f"ci_client_web_{process_dir.name}",
        description=(
            f"Test of the documented Web editor example for {process_dir.name}"
        ),
    )
    print(f"  created job {job.job_id}")
    succeeded = False
    try:
        wait_for_job(job, timeout_seconds, poll_interval)
        result = download_and_verify(
            job, download_dir, config, download_attempts=download_attempts
        )
        succeeded = True
        return result
    finally:
        if keep_jobs or not succeeded:
            reason = "requested" if keep_jobs else "failure"
            print(f"  keeping job {job.job_id} ({reason})")
        else:
            try:
                job.delete()
            except Exception as error:  # noqa: BLE001 - cleanup must not mask the result
                print(f"  could not delete job {job.job_id}: {error}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--backend",
        default=os.environ.get("OPENEO_BACKEND", DEFAULT_BACKEND),
        help=f"openEO back-end URL (default: {DEFAULT_BACKEND})",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=(
            "public URL of the rendered runtime archive and usage examples; "
            "must be reachable by the back-end and client (default: "
            f"{DEFAULT_BASE_URL})"
        ),
    )
    parser.add_argument(
        "--version",
        default=os.environ.get("GITHUB_REF_NAME", "main"),
        help="version label rendered into the graph (default: GITHUB_REF_NAME or main)",
    )
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        metavar="NAME",
        help="test only this model directory; may be repeated",
    )
    parser.add_argument(
        "--client",
        action="append",
        dest="clients",
        choices=sorted(CLIENTS),
        metavar="CLIENT",
        help=(
            "test the documented example of this client instead of the inline "
            "graph; may be repeated ("
            + ", ".join(sorted(CLIENTS))
            + ")"
        ),
    )
    parser.add_argument(
        "--timeout-minutes",
        type=float,
        default=120,
        help="time to wait for one batch job (default: 120)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=30,
        help="seconds between job status polls (default: 30)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="keep downloaded results here instead of a temporary directory",
    )
    parser.add_argument(
        "--download-retries",
        type=int,
        default=4,
        help="attempts to download a job result before giving up (default: 4)",
    )
    parser.add_argument(
        "--keep-jobs",
        action="store_true",
        help="do not delete the CDSE jobs after the test",
    )
    args = parser.parse_args()

    models = discover_models(REPO_ROOT, args.models)
    connection = connect(args.backend)
    timeout_seconds = args.timeout_minutes * 60

    download_root = args.output_dir or Path(tempfile.mkdtemp(prefix="fair-geo-fm-test-"))
    download_root.mkdir(parents=True, exist_ok=True)

    failures = []
    tested = 0

    def run(label, function):
        nonlocal tested
        print(f"[{label}]")
        started = time.monotonic()
        tested += 1
        try:
            result = function()
            elapsed = time.monotonic() - started
            print(f"[{label}] ok after {elapsed:.1f}s: {result}")
        except Exception as error:  # noqa: BLE001 - run all, report at the end
            elapsed = time.monotonic() - started
            print(
                f"[{label}] FAILED after {elapsed:.1f}s: {error}",
                file=sys.stderr,
            )
            traceback.print_exc()
            failures.append(label)

    for process_dir in models:
        if args.clients:
            for client in args.clients:
                run(
                    f"{process_dir.name}/{client}",
                    lambda process_dir=process_dir, client=client: test_client(
                        connection,
                        process_dir,
                        client,
                        base_url=args.base_url,
                        timeout_seconds=timeout_seconds,
                        poll_interval=args.poll_interval,
                        download_dir=(
                            download_root / process_dir.name / client
                        ),
                        keep_jobs=args.keep_jobs,
                        download_attempts=args.download_retries,
                    ),
                )
        else:
            run(
                process_dir.name,
                lambda process_dir=process_dir: test_model(
                    connection,
                    process_dir,
                    base_url=args.base_url,
                    version=args.version,
                    timeout_seconds=timeout_seconds,
                    poll_interval=args.poll_interval,
                    download_dir=download_root / process_dir.name,
                    keep_jobs=args.keep_jobs,
                    download_attempts=args.download_retries,
                ),
            )

    if failures:
        raise SystemExit(f"model smoke test failed for: {', '.join(failures)}")
    print(f"all {tested} documented example(s) passed")


if __name__ == "__main__":
    main()
