# Contributing

Conventions for the models and runtimes in this repository.

## Processes

- Each process is a user-defined process (UDP) served through the
  [openEO remote process definition extension](https://api.openeo.org/extensions/remote-process-definition/0.1.0).
  Callers set `namespace` to the URL of the exported `process_graph.json`.
- One directory per major model version, so versions can be used side by side:
  `models/tessera_v2/`, `models/tessera_v1/`.
- Put the major version in the process id, not in a parameter:
  `tessera_v2_embeddings`. UDPs have no version property, and a
  `model_version` parameter cannot change the process contract.
- Process ids are model-first, end with `_embeddings`, and carry no `infer_` or
  `load_` prefix. The suffix states the output type; a verb would collide with
  the generic `load_embeddings` and `ml_predict` processes. Say whether a
  process computes on demand or reads archived tiles in `summary` and
  `description`.
- One embedding per process graph: do not pack several models into one UDP.
- Use openEO-friendly file names with underscores. Process ids must match
  `^\w+$`, so no hyphens or dots.
- Inside a version directory, use role names rather than repeating the id:
  - `process_graph.json.j2` — the Jinja2 template, maintained by hand; source of
    truth for the graph structure and its published URLs
  - `process_graph.json` — generated from the template, `udf.py` and
    `runtime.txt` by `scripts/build-process-graph.py`; never edit it by hand
    and do not commit it. CI renders it to validate the template, and the
    publish workflows render it for the served site and release.
  - `udf.py` — UDF source, when the UDF is specific to this model
  - `example.py` — sample call
  - `README.md.j2` — the model's documentation page as a Jinja2 template,
    maintained by hand; `scripts/prepare-docs.mjs` renders it to the page's
    `index.md` for the version being published
- Inline the UDF in `process_graph.json` so one URL resolves the whole process.
  The template receives it as `{{ udf_json }}`, which the build script fills
  from `udf.py`; do not paste the UDF into the template.
  If it grows too large to maintain inline, reference `udf.py` by URL from
  `run_udf` instead.
- Do not hardcode publication URLs. The process graph template is rendered for
  a base URL and exposes `{{ runtime_url }}` (the dependency archive, with
  `#feature_deps`), `{{ process_graph_url }}` and `{{ model }}` / `{{ runtime }}`.
  `README.md.j2` exposes `{{ process_graph_url }}` (the same published graph
  URL the process graph template uses), `{{ base_url }}`, `{{ model }}`,
  `{{ docs_url }}` (the version's documentation page, with a trailing `/`) and
  `release` (`true` on `v*` tags, `false` on branches). Wrap development
  notices in `{% if not release %} ... {% endif %}` so released pages show
  only the stable content.
  The same layout is used for the staging Pages site and for a Zenodo release:
  - staging: `https://danlooo.github.io/fair-geo-fm/main`
  - release: `https://zenodo.org/records/<record-id>/files`
  A released tag's page links to **that tag's** Zenodo version record, resolved
  from the concept record's versions (not the concept/root DOI); `main` and
  other branches link to the staging `/main` graphs.
- Reference a released model by its DOI URL in `namespace`, not by a branch, so
  stored jobs stay reproducible. The staging URL is overwritten on every
  deployment and is only for checking a change before a release.
- Do not end `namespace` or `links` URLs with a trailing `/`. Documentation
  page URLs do end with `/` because VitePress serves each process page as a
  directory index.
- Documentation is published at <https://danlooo.github.io/fair-geo-fm>. Put the
  link in the process `description` and in `links` (`rel: canonical`), and in
  the node `description` of copy-paste examples — that is what a user reads in
  the Web editor's Code tab.

> **Note:** the openEO Web editor does not retrieve metadata from the external
> `namespace` URL when a process graph is created. `summary`, `description`,
> `links` and the parameter descriptions are not shown there; the editor only
> displays the process graph that is pasted into it. Do not rely on them to
> communicate anything users must see — put it in the node `description` of
> examples, this README, or the documentation page instead.

## Runtimes

- Shared dependency archives live in `runtimes/`, never inside a process
  directory, and are reused across models.
- One directory per interpreter/stack combination, named after both, containing
  only `requirements.txt`: `runtimes/py311-torch231/`. The target Python version
  is encoded in the name (`py311` -> 3.11) and the file may pin an index via
  `--extra-index-url`.
- `uv run scripts/create-python-dependency-archive.py --requirements
  runtimes/py311-torch231/requirements.txt --output-file
  runtimes/py311-torch231/dependencies.zip` writes that file. It is git-ignored;
  CI rebuilds it from the pinned `requirements.txt` for each publication.
- Pin `runtime` and `version` in `run_udf` and reference the archive from the
  process's `default_job_options` via `udf-dependency-archives`, so interpreter
  and archive move together. Entries always carry the `#feature_deps` suffix,
  which tells openEO to extract the archive into the UDF's `feature_deps/`
  directory instead of replacing the sandbox's own dependencies.
- The URL comes from the template's `{{ runtime_url }}`, not from a literal:
  - staging: <https://danlooo.github.io/fair-geo-fm/main/runtimes/py311-torch231.zip>
  - release: `https://zenodo.org/records/<record-id>/files/runtimes/py311-torch231.zip`
- The staging archive is built from the default branch and **overwritten on
  every deployment**, so the URL always yields the current `main` archive rather
  than a snapshot. The Zenodo copy is immutable and carries the release DOI.
- Prefer object storage for production. A Pages site is capped at 1 GB and
  roughly 100 GB/month of bandwidth, so a handful of `py311-torch231` (~160 MB)
  downloads exhaust the monthly budget, and Pages does not content-address the
  archive. Do not serve archives from GitHub raw or LFS either: raw is capped at
  100 MiB and LFS is bandwidth-metered.

## Releases

Two publication targets share one layout, selected by a base URL:

| Target | Base URL | Contents |
| --- | --- | --- |
| Staging (`publish`) | `https://danlooo.github.io/fair-geo-fm/main` | docs site, process graphs, runtime archives |
| Release (`publish-release`) | `https://zenodo.org/records/<record-id>/files` | process graphs, model READMEs, runtime archives, full source archive |

The staging target is rebuilt automatically after every successful push build.
Nothing is published unless the `build` workflow succeeded for the commit, so a
broken process graph or archive is never deployed. Staging URLs are
**overwritten on every deployment** and are only for checking a change before a
release.

Releases are never automatic. A maintainer creates a GitHub release by hand,
which is the release decision, and the `publish-release` workflow archives the
tag on Zenodo. It refuses to guess URLs: it opens a new version of the
project's concept record (**10.5281/zenodo.24193626**) through Zenodo's
InvenioRDM API, reserves the version DOI first, renders every process graph
against the version's file URL, uploads the graphs, the runtime archives and a
source archive of the released commit, and publishes the version last. Every
release therefore adds a version to the same root record; the concept DOI
always resolves to the latest version. Each
model contributes its rendered `process_graph.json` and `README.md`; the UDF is
embedded in the graph, and `udf.py` / `example.py` / `runtime.txt` stay in the
source archive. The README's code samples link to the record's own graph while
its documentation link points at the versioned Pages site. The resulting record
has many files under custom paths —

    https://zenodo.org/records/<record-id>/files/models/tessera_v2/process_graph.json
    https://zenodo.org/records/<record-id>/files/models/tessera_v2/README.md
    https://zenodo.org/records/<record-id>/files/runtimes/py311-torch231.zip
    https://zenodo.org/records/<record-id>/files/danlooo/fair-geo-fm-v0.2.zip

The DOI (`https://doi.org/<doi>`) is the citable identifier of the record but
does not serve individual files: `doi.org` treats any suffix as part of the DOI
and returns 404. Link files through the record URL instead.

— so Zenodo's automatic GitHub integration cannot be used; it archives one
repository snapshot under its own paths and cannot add the rendered graphs.
The source archive is exported with `git archive` from the released tag and
uploaded under the path the GitHub integration would have used, so the record
is self-contained. The workflow talks to the Zenodo REST API
(<https://developers.zenodo.org/>) through `scripts/publish-release.py`.

### Zenodo credentials

Add the repository secret **`ZENODO_ACCESS_TOKEN`** (Settings → Secrets and
variables → Actions). Create it at <https://zenodo.org/account/settings/applications/>
with the `deposit:write` and `deposit:actions` scopes. The optional repository
variable **`ZENODO_API_URL`** overrides the API base; set it to
`https://sandbox.zenodo.org/api` to rehearse against the Zenodo sandbox. When
rehearsing against the sandbox, also set **`ZENODO_CONCEPT_DOI`** to the
sandbox concept record's DOI; it defaults to the production record
`10.5281/zenodo.24193626`.

### Publishing a release

```sh
# 1. tag and create the GitHub release (the release decision)
gh release create v0.2 --generate-notes

# 2. rehearse: reserve a DOI, render and upload, then delete the draft
gh workflow run "publish-release" -f tag=v0.2 -F dry_run=true

# 3. publish for real, or let the `release: published` event do it
gh workflow run "publish-release" -f tag=v0.2 -F dry_run=false
```

- Creating the release normally triggers the workflow automatically. The
  `workflow_dispatch` form is for retries and rehearsals.
- The job waits for approval by the protected `release` environment.
- `dry_run` defaults to `true` for `workflow_dispatch`. It reserves a DOI,
  renders and uploads every file, writes the planned URLs to the run summary,
  and deletes the draft. `ZENODO_API_URL` can point at the sandbox.
- Creating a new GitHub release adds a new version to the same Zenodo concept
  record and gives that version its own DOI. The concept DOI
  `10.5281/zenodo.24193626` always resolves to the latest version. Re-running a
  failed upload against the same draft should use `--deposition-id` (the draft
  id is printed and written to the run summary); re-running a release that was
  already published is refused, because its version is already the latest.
- Documentation is versioned by git ref. The site root redirects to the newest
  `v*` tag, which is the current version; `main` and every older tag are
  published under `/<ref>/` and listed in the version switcher.
- A released tag's model page links to that tag's Zenodo version record; the
  docs build resolves the version records from the concept record through the
  Zenodo API and falls back to the staging base if that lookup fails.
- `publish-release` dispatches `publish` after the Zenodo record is published,
  so the tag page is rebuilt against the new version record/DOI instead of the
  staging URLs from the deployment that ran when the tag was pushed. A
  rehearsal (`dry_run`) does not rebuild, because the draft is deleted.
- The staging process graph is served at the fixed `/main/models/<model>/`
  path on Pages. `prepare-docs.mjs` copies the rendered default-branch graph
  there, because a ref worktree never contains the git-ignored build output.
- After a successful release, update the model page and `example.py` to show the
  DOI URL, and quote the runtime sha256 in its README.
  `scripts/build-process-graph.py --check` enforces that the committed
  `process_graph.json` matches the template and that its archive URL ends with
  `runtimes/<runtime>.zip#feature_deps`.
