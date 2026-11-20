# AGENTS.md

Orientation for AI coding agents working in this repository. Read this first,
then [CONTRIBUTING.md](CONTRIBUTING.md) — that file is the source of truth for
model, naming, STAC, runtime and release conventions. This file only says what
lives where, which files are generated, and how to validate a change.

## What this repository is

openEO process graphs and UDF runtimes that expose geospatial foundation-model
embeddings (AlphaEarth, TESSERA, ...) as user-defined processes, so callers can
use them on an openEO back-end. Every model ships three things together: a
process graph, an optional UDF plus its dependency archive, and a STAC Item in
the published catalog. Publishes to two targets with the same layout:

| Target | Base URL |
| --- | --- |
| Staging (GitHub Pages) | `https://danlooo.github.io/fair-geo-fm/main` |
| Release (Zenodo) | `https://zenodo.org/records/<record-id>/files` |

## Layout

| Path | What it is |
| --- | --- |
| [models/](models/) | One directory per major model version (`tessera_v2/`, `alphaearth/`, ...). See below. |
| [runtimes/](runtimes/) | One directory per interpreter/stack (`py311-torch231/`) containing only `requirements.txt`. |
| [scripts/](scripts/) | Build, validate and publish tooling. PEP 723 inline dependencies; run with `uv run`. |
| [docs/](docs/) | VitePress site. `docs/.vitepress/` (config, theme) is hand-maintained; `docs/src/`, `docs/public/`, `docs/archive/` are generated. |
| [stac/](stac/) | Generated STAC catalog. Git-ignored, never committed. |
| [.github/workflows/](.github/workflows/) | `build` (validation), `test` (runs examples on CDSE), `publish-staging`, `publish-release`. |

A model directory with a `process_graph.json.j2` contains:

- `process_graph.json.j2` — hand-written Jinja2 template, source of truth for
  graph structure and published URLs.
- `udf.py` — optional UDF source, inlined into the graph at build time.
- `config.json` — single source of truth for name, description, runtime, type,
  architecture, input sources, extents, dimensions and parameters. The build
  validates it and both templates render from it.
- `stac.json` — the MLM/processing/scientific fields that cannot be derived.
- `process_graph.json` — **generated**, git-ignored. Never edit by hand.

## Commands

Python 3.12 and [uv](https://docs.astral.sh/uv/); Node 22 and npm for the docs.

```sh
# Render one model's graph against the staging base URL
uv run scripts/build-process-graph.py models/<model>

# Validate without writing (what CI runs); fails on a stale or invalid graph
uv run scripts/build-process-graph.py models/<model> --check

# Generate or validate the STAC catalog (writes to stac/, git-ignored)
uv run scripts/build-stac.py
uv run scripts/build-stac.py --check

# Build a UDF dependency archive (git-ignored output)
uv run scripts/create-python-dependency-archive.py \
    --requirements runtimes/<runtime>/requirements.txt \
    --output-file runtimes/<runtime>/dependencies.zip

# Run a model's documented example on CDSE (needs credentials, see below)
OPENEO_AUTH_CLIENT_ID=... OPENEO_AUTH_CLIENT_SECRET=... \
    uv run scripts/test-models.py --model <model> --keep-jobs

# Run the Web editor, Python and R examples documented on a model page
# (needs credentials; R also needs Rscript with the openeo and jsonlite packages)
OPENEO_AUTH_CLIENT_ID=... OPENEO_AUTH_CLIENT_SECRET=... \
    uv run scripts/test-models.py --model alphaearth \
    --client web --client python --client r

# Preview / build the documentation site
npm ci
npm run dev      # prepares versioned pages, then vitepress dev
npm run build
```

`test-models.py` needs the OIDC client credentials described in
[CONTRIBUTING.md](CONTRIBUTING.md#openeo-credentials-for-the-test-workflow);
without them, validate with the `--check` commands above.

## Generated vs. source

| Edit this | Never edit this |
| --- | --- |
| `models/*/process_graph.json.j2` | `models/*/process_graph.json` |
| `models/*/config.json` | anything under `stac/` |
| `models/*/stac.json` | `runtimes/*/dependencies.zip` |
| `models/model.md.j2`, `models/index.md.j2` | `models/*/index.md` (rendered) |
| `docs/.vitepress/**` | `docs/src/`, `docs/public/`, `docs/archive/`, `docs/.vitepress/{current-version,stac-bases,zenodo-records}.json` |
| `runtimes/*/requirements.txt` | `checksums.sha256` |

All the right-hand files are git-ignored build outputs. If one is wrong, fix
its source and regenerate rather than patching the output.

## Invariants

The build rejects most of these, so a violation fails CI rather than shipping:

- No hardcoded publication URLs. Templates receive `{{ runtime_url }}`,
  `{{ process_graph_url }}`, `{{ version }}`, `{{ model }}`, `{{ runtime }}`
  and, on a release, `{{ zenodo_url }}`. Never write a version tag or DOI into
  a template.
- Process ids must match `^\w+$`, are model-first and end in `_embeddings`.
- `config.json` has no `tags` key (badges are derived) and every declared
  parameter must be referenced from a process node via `from_parameter`.
- `spatial_extent` / `temporal_extent` examples must match `SHARED_EXAMPLES` in
  [scripts/process_graphs.py](scripts/process_graphs.py).
- `config.json` `architecture` must equal the Item's `mlm:architecture`.
- A model that names a `runtime` must reference an existing `runtimes/`
  directory; a graph built only from built-in processes names no runtime.
- UDFs are inlined from `udf.py`; do not paste UDF source into the template.
- Write American English (see CONTRIBUTING.md).
- Pin external URLs — including weights — to a commit or release tag, never a
  mutable branch.
- The docs page template is rendered by `scripts/prepare-docs.mjs`, which
  supports only `{{ dotted.path }}`, `{% if [not] path %}` and
  `{% for x in list.path %}`. `process_graph.json.j2`, by contrast, gets full
  Jinja2 from `scripts/process_graphs.py`.

## Working notes

- `uv run …` uses each script's inline dependencies; there is no repository
  `pyproject.toml`.
- Validate before committing: the two `--check` commands above cover graphs and
  STAC without network access or credentials.
- Published URLs are immutable once released; staging URLs are overwritten on
  every deployment and are for pre-release checks only.
- Details on naming, `config.json` fields, STAC mapping, templates, versioning
  and release mechanics are all in [CONTRIBUTING.md](CONTRIBUTING.md). Prefer it
  over guessing.
