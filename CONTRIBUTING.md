# Contributing

Conventions for the models and runtimes in this repository.

## Models

- Each model is a user-defined process (UDP) served through the
  [openEO remote process definition extension](https://api.openeo.org/extensions/remote-process-definition/0.1.0).
  Callers set `namespace` to the URL of the exported `process_graph.json`.
- One directory per major model version, so versions can be used side by side:
  `models/tessera_v2/`, `models/tessera_v1/`.
- Put the major version in the process id, not in a parameter:
  `tessera_v2_embeddings`. UDPs have no version property, and a
  `model_version` parameter cannot change the process contract.
- Process ids are model-first, end with `_embeddings`, and carry no `infer_` or
  `load_` prefix. The suffix states the output type; a verb would collide with
  the generic `load_embeddings` and `ml_predict` processes. State whether a
  model reads published embedding tiles or loads weights and computes on demand
  in the `type` of its `config.json` (`"frozen embeddings"` or `"weights"`).
- One embedding per process graph: do not pack several models into one UDP.
- Use openEO-friendly file names with underscores. Process ids must match
  `^\w+$`, so no hyphens or dots.
- Inside a version directory, use role names rather than repeating the id:
  - `process_graph.json.j2` — the Jinja2 template, maintained by hand; source of
    truth for the graph structure and its published URLs
  - `process_graph.json` — generated from the template, `udf.py` and
    `config.json` by `scripts/build-process-graph.py`; never edit it by hand
    and do not commit it. CI renders it to validate the template, and the
    publish workflows render it for the served site and release.
  - `udf.py` — UDF source, when the UDF is specific to this model. A graph
    built only from built-in openEO processes (for example one that loads
    precomputed embeddings) has no `udf.py` and needs no `runtime` in
    `config.json`.
  - `config.json` — the model's single source of truth: the display `name`,
    `description`, optional `runtime`, the model `type` (`"frozen embeddings"`
    or `"weights"`), its `architecture` (the same value as the Item's
    `mlm:architecture`), `input_sources`, the `spatial_extent`
    (`[west, south, east, north]`, WGS 84) and `temporal_extent`
    (`[start, end]`, RFC 3339 timecodes) of the product it serves,
    `spatial_resolution` (a string such as `"10 m"`), `temporal_resolution`
    (such as `"annual"`), and the embedding
    `dimensions` (ascending, one entry per usable output width, e.g. `[128]` or
    the Matryoshka prefixes `[16, 32, 64, 128]`). `dimensions_label` optionally
    annotates the values, e.g. `"Matryoshka"`. Then the `parameters` table,
    `publication` (title, URL, citation and abstract) and `links` list of its
    page. The model page renders these as a properties table. Badge tags are
    **generated** from those fields rather than stored: `type`,
    `dimensions_label`, `<max dimension>-dim`, `spatial_resolution` and
    `temporal_resolution`. Do not add a `tags` key; the build rejects it. The
    build reads the runtime from it,
    both templates receive it as `config`, and
    the docs build renders the model cards and page sections from it. Each
    parameter also carries an openEO `schema`; required parameters carry an
    `example` value (plain JSON), optional ones a `default_value`. The usage
    examples render one argument per required parameter, formatting that
    value as a JSON, Python and R literal, and ignore optional parameters.
    The `spatial_extent` and `temporal_extent` examples must match
    `SHARED_EXAMPLES` in `scripts/process_graphs.py`, so every model shows the
    same region and year; the build rejects a mismatch.
    `process_graph.json` declares all parameters in a top-level openEO
    `parameters` array, with each example copied into its `schema.examples`
    and each `default_value` published as the parameter default. Every
    declared parameter must be referenced from the process nodes with
    `from_parameter`; the build rejects unused ones.
  - `stac.json` — the model's STAC Item metadata, read by
    `scripts/build-stac.py`: the MLM fields that cannot be derived from
    `config.json` or the process graph. See [STAC metadata](#stac-metadata).
- All models share one documentation page template,
  `models/model.md.j2`, rendered by `scripts/prepare-docs.mjs` to each model's
  `index.md` for the version being published.
- The catalogue page `models/index.md.j2` is a Jinja2 template.
  `scripts/prepare-docs.mjs` exposes a `models` list at docs-build time, one
  entry per model directory with a `config.json`, with `slug`, `name` and
  `description` taken from that file and `tags` derived from its properties.
  It also exposes
  <code v-pre>{{ stac_catalog_url }}</code> and
  <code v-pre>{{ stac_collection_url }}</code> for the version's generated STAC
  tree, both opened in the external STAC browser. The page renders one card per
  entry with
  <code v-pre>{% for model in models %} ... {% endfor %}</code>. Add a model by
  adding its version directory with a `config.json`, a `process_graph.json.j2`
  and a `udf.py`; do not list it by hand.
- Inline the UDF in `process_graph.json` so one URL resolves the whole process.
  The template receives it as <code v-pre>{{ udf_json }}</code>, which the build script fills
  from `udf.py`; do not paste the UDF into the template.
  If it grows too large to maintain inline, reference `udf.py` by URL from
  `run_udf` instead. `udf.py` is required when the graph runs a UDF: the build
  reads it for <code v-pre>{{ udf_json }}</code> and the STAC Item links it. A graph with
  only built-in processes omits it, receives an empty `udf_json`, and CI still
  validates it because the template identifies the model directory.
- Do not hardcode publication URLs. The process graph template is rendered for
  a base URL and exposes <code v-pre>{{ runtime_url }}</code> (the dependency archive, with
  `#feature_deps`), <code v-pre>{{ process_graph_url }}</code>, <code v-pre>{{ version }}</code>
  (the release tag, or `main` for a branch render) and
  <code v-pre>{{ model }}</code> / <code v-pre>{{ runtime }}</code>. A release render
  also sets <code v-pre>{{ zenodo_url }}</code> (the Zenodo record page serving
  that release); it is empty for the staging render, which omits the field.
  It also receives the parsed `config.json` as <code v-pre>{{ config }}</code> (and as the JSON
  literal <code v-pre>{{ config_json }}</code>) and copies it into the graph's top-level `model`
  entry, so the published `process_graph.json` is self-contained.
  The documentation template receives the same <code v-pre>{{ config }}</code> mapping: the title is
  <code v-pre>{{ config.name }}</code> and the badges are rendered with
  <code v-pre>{% for tag in config.tags %}</code>. It also exposes
  <code v-pre>{{ process_graph_url }}</code> (the same published graph
  URL the process graph template uses), <code v-pre>{{ base_url }}</code>, <code v-pre>{{ model }}</code>,
  <code v-pre>{{ docs_url }}</code> (the version's documentation page, with a trailing `/`), and
  the release its header advertises: <code v-pre>{{ release_version }}</code>, <code v-pre>{{ release_url }}</code>
  (that release's documentation page, with a trailing `/`) and <code v-pre>{{ doi }}</code> (that
  release's version DOI). It also exposes
  <code v-pre>{{ stac_catalog_url }}</code> and
  <code v-pre>{{ stac_collection_url }}</code> as raw JSON and
  <code v-pre>{{ stac_item_url }}</code> as the Item opened in the external STAC
  browser. Their base is the same publication target as the process graph (the
  version's Zenodo record for a tag, the staging `/main` tree otherwise), but
  the Item URL used by the browser points at Zenodo's CORS-enabled API route
  (`/api/records/<record-id>/files/stac/.../content`) for a tag, because the
  plain file route sends no `Access-Control-Allow-Origin` header. The Item id
  comes from `stac.json`. A tag
  page advertises its own release, DOI and Item; a branch page (`main`) has no
  DOI, so the <code v-pre>{% if doi %}</code> gate omits the release header and the
  <code v-pre>{% if not doi %}</code> block shows the STAC item link instead.
  <code v-pre>{{ release }}</code> is `true` for a `v*` tag and `false` for a branch,
  for templates that gate development notices on it.
  The two templates are not rendered by the same engine. `process_graph.json.j2`
  gets full Jinja2 from `scripts/process_graphs.py`; the documentation template
  is rendered by `scripts/prepare-docs.mjs`, which implements only
  <code v-pre>{{ dotted.path }}</code> variables, <code v-pre>{% if [not] path %}</code> blocks and
  <code v-pre>{% for item in list.path %}</code> loops, inline or spanning whole lines. An unknown
  variable is an error rather than a published page that keeps a literal
  <code v-pre>{{ ... }}</code>, so keep the page template within that subset.
  The release README uploaded to Zenodo advertises the record it
  belongs to; never write `v0.1.2` or a DOI into the template.
  The same layout is used for the staging Pages site and for a Zenodo release:
  - staging: `https://danlooo.github.io/fair-geo-fm/main`
  - release: `https://zenodo.org/records/<record-id>/files`
  A released tag's page links to **that tag's** Zenodo version record, resolved
  from the concept record's versions (not the concept/root DOI); `main` and
  other branches link to the staging `/main` graphs.
- Reference a released model by the file URL of its Zenodo version record in
  `namespace`, not by a branch, so stored jobs stay reproducible. Do not use the
  bare DOI: `doi.org` treats any suffix as part of the DOI and returns 404, so
  it cannot locate a process graph. The staging URL is overwritten on every
  deployment and is only for checking a change before a release.
- Do not end `namespace` URLs with a trailing `/`. Documentation page URLs do
  end with `/` because VitePress serves each process page as a directory index.
- Documentation is published at <https://danlooo.github.io/fair-geo-fm>. Put the
  link in the node `description` of the copy-paste examples in the model template
  (<code v-pre>{{ docs_url }}</code>) — that is what a user reads in the Web editor's Code
  tab — and on the model page itself.

> **Note:** the openEO Web editor does not retrieve metadata from the external
> `namespace` URL when a process graph is created. `summary`, `description`,
> `links` and the parameter descriptions are not shown there; the editor only
> displays the process graph that is pasted into it. Do not rely on them to
> communicate anything users must see — put it in the node `description` of
> examples, this README, or the documentation page instead. The published graph
> in this repository carries `id`, `version`, `model`, `default_job_options`
> and `process_graph`, plus `zenodo_url` on a released graph, and nothing else.

## Publication metadata

Each `config.json` carries a `publication` object:

- `title` — the paper title exactly as published, including its capitalisation.
- `url` — the paper page, for example its arXiv abstract page or DOI resolver
  URL. The model page renders `title` as a link to it.
- `citation` — the bibliographic details without the title, such as authors,
  year, venue and access date.
- `abstract` — the **exact** abstract from the paper, copied verbatim from its
  publisher or arXiv page. Do not paraphrase, shorten, reformat, or tidy the
  wording, punctuation or capitalisation, and do not leave placeholder text
  such as `this https URL` in place of a URL. The model page renders the
  abstract verbatim, so any deviation misrepresents the paper.

## STAC metadata

Every model directory with a `process_graph.json.j2` also needs a `stac.json`.
`scripts/build-stac.py` renders the model into a STAC Item (MLM, processing,
scientific and version extensions) inside the `fair-geo-fm-models` Collection;
`stac.json` supplies what cannot be derived:

- `id`, `title`, `datetime`, `bbox`, `keywords` — Item identity and extent
- `properties` — MLM fields: `mlm:architecture`, `mlm:tasks`, `mlm:framework`,
  `mlm:framework_version`, `mlm:input`, `mlm:output`
- `processing` — `lineage` and `software` for the processing extension
- `publications` — `sci:publications` entries (citation and DOI)
- `weights` — optional override of the model asset; otherwise the weights URL
  is read from the rendered process graph's `weights_url`. Pin external weight
  URLs to a commit or release tag rather than a mutable branch such as
  `resolve/main`, so the Item and the graph keep resolving to the same file.
  Omit it for a model that loads precomputed embeddings; the Item then has no
  `model` asset.

The display name, description, runtime and publication come from `config.json`,
and so does the coverage: `temporal_extent` becomes the Item's
`start_datetime`/`end_datetime` plus a `cube:dimensions.t` entry (with an ISO
8601 `step` from `temporal_resolution`), `spatial_resolution` becomes `gsd`,
and `architecture` must match `mlm:architecture`. The remaining model
properties have no consulted extension field, so they are published with the
project prefix: `fairgeofm:type`, `fairgeofm:input_sources`,
`fairgeofm:dimensions`, `fairgeofm:spatial_resolution`,
`fairgeofm:temporal_resolution` and the derived `fairgeofm:tags`. Prefixed fields are not covered by a schema yet; move them to
the `emb:` extension if one takes them over.

The conventional asset keys are `model` (weights), `process_graph`, `udf`,
`runtime`, `documentation` and, on a release, `source_code`; a Collection
`item_assets` template documents them.

The catalog is generated, never committed:

    uv run scripts/build-stac.py --check     # validate only (CI)
    uv run scripts/build-stac.py             # write stac/ for the staging base URL
    uv run scripts/build-stac.py --base-url <record-files-url> --version v0.2 --doi <doi>

`publish-staging.yml` serves the staging catalog at `/main/stac/`. `publish-release.py`
writes the same tree into the Zenodo record under `stac/`, with asset URLs
pointing at the record's own files. Its catalog links are absolute URLs on
Zenodo's API route (`/api/records/<record-id>/files/stac/.../content`): the
plain `/records/<record-id>/files/...` route sends no
`Access-Control-Allow-Origin` header, so STAC Browser cannot fetch it from the
documentation site. The staging catalog keeps relative links, since GitHub Pages
serves them with CORS. The documentation site's navbar STAC link is
version-aware: `prepare-docs.mjs` writes
`docs/.vitepress/stac-bases.json`, mapping each version to the catalog URL a
browser can fetch (the Zenodo API route for a released tag, the staging
`/main/stac/catalog.json` otherwise), and `config.mjs` exposes it to
`StacNavLink.vue`. Because the Item is generated per release, do not edit a
generated Item to correct metadata; change `stac.json` instead.
Items describe models, not embedding products: a frozen-embedding Collection
belongs in a separate embedding catalog and links back to the model Item with
`emb:encoder_model`.

## Runtimes

- Shared dependency archives live in `runtimes/`, never inside a process
  directory, and are reused across models.
- A model selects its runtime with the `runtime` field of its `config.json`,
  next to the display `name`. One file per model keeps the catalogue, the
  documentation and the process graph in sync.
- The runtime is optional: a graph built only from built-in openEO processes
  has no UDF and no dependency archive, so it omits `runtime` and
  `default_job_options.udf-dependency-archives`. The build rejects a graph that
  declares archives without naming a runtime.
- One directory per interpreter/stack combination, named after both, containing
  only `requirements.txt`: `runtimes/py311-torch231/`. The target Python version
  is encoded in the name (`py311` -> 3.11) and the file may pin an index via
  `--extra-index-url`.
- `uv run scripts/create-python-dependency-archive.py --requirements
  runtimes/py311-torch231/requirements.txt --output-file
  runtimes/py311-torch231/dependencies.zip` writes that file. It is git-ignored;
  CI rebuilds it from the pinned `requirements.txt` for each publication.
- The template sets `runtime` in `run_udf` to the UDF language (`Python`) and
  references the archive from the process's `default_job_options` via
  `udf-dependency-archives`. The interpreter and stack version are not in the
  graph: they are the runtime directory's name and its pinned `requirements.txt`,
  which is what the archive is built from, so interpreter and archive move
  together. Entries always carry the `#feature_deps` suffix,
  which tells openEO to extract the archive into the UDF's `feature_deps/`
  directory instead of replacing the sandbox's own dependencies.
- The URL comes from the template's <code v-pre>{{ runtime_url }}</code>, not from a literal:
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

## Documentation site

The documentation is a VitePress site under `docs/`.
`scripts/prepare-docs.mjs` generates the versioned pages; the theme —
`docs/.vitepress/config.mjs`, `theme/` and `theme/custom.css` — is maintained by
hand. Components in `docs/.vitepress/theme/` are registered globally in
`theme/index.mjs`, so a page uses them as plain tags without an import.

- `ProcessGraph.vue` renders an openEO process graph with the
  `<openeo-model-builder>` web component and is used by `models/model.md.j2` as
  <code v-pre>&lt;ProcessGraph id="process-graph-{{ model }}" /&gt;</code>.
  Props are `id` (required, unique in the document), `src` (graph file relative
  to the page, default `process_graph.json`) and `height` (default `600px`).
  `@openeo/vue-components` is Vue 2 and cannot be imported as an SFC into this
  Vue 3 site, so the component lazily loads the prebuilt web-component bundle
  from `docs/public/vendor/openeo/openeo.min.js`, which `prepare-docs.mjs`
  copies out of `node_modules`. `config.mjs` marks every `openeo-*` tag as a
  custom element so Vue leaves it to the browser. The graph is fetched next to
  the page, not against the document base, because the site is served under a
  base path and one folder per version. A graph is a git-ignored build output,
  so `prepare-docs.mjs` materializes `process_graph.json` next to every version:
  the release's own copy from its Zenodo record for a tag, the staging `/main`
  graph for branches and unresolved releases. The toolbar opens the fetched
  `process_graph.json` in a new tab and enlarges the same builder instance in a
  dialog instead of mounting a second one. ModelBuilder appends a Font Awesome
  CDN stylesheet to its shadow root; the component replaces it with the
  vendored copy under `docs/public/vendor/fontawesome/`, so the page makes no
  third-party request.
- `StacNavLink.vue` is the navbar `STAC` entry. It opens the generated catalog
  of the version being viewed in the external STAC browser, reading
  `theme.stacBases` from `docs/.vitepress/stac-bases.json` (written by
  `prepare-docs.mjs`) and falling back to the staging `/main/stac/catalog.json`
  for a version without an entry. The values are complete catalog URLs,
  because a Zenodo release must use the API's CORS-enabled `/content` route.
- `VersionBanner.vue` renders the notice above the `h1` of every page that is
  not the current release: an outdated-version banner for an older `v*` tag and
  a preview banner for a branch. It links to the same page in the latest
  release, or to that release's home page when the page does not exist there.
  `index.mjs` mounts it in VitePress's `doc-before` slot, which the home layout
  does not render, so the banner never appears on the home page.
- `VersionSwitcher` from `@viteplus/versions` is registered with them. The
  plugin's own nav dropdown is disabled (`versionSwitcher: false`) because it
  links each version's home page; this component keeps the current page and
  swaps only the version segment. `installVersionFallback` handles refs that
  have no site.
- `::: tabs` / `== Tab` blocks come from `vitepress-plugin-tabs`, enabled in
  both `config.mjs` (Markdown plugin) and `index.mjs`
  (`enhanceAppWithTabs`). Use them when a page shows alternative ways to do one
  thing, as `models/model.md.j2` does for the Web editor and Python.
- `custom.css` styles these components. `config.mjs` adds the `model-page`
  class to model pages, which widens the content column for the graph viewer.

## Releases

Two publication targets share one layout, selected by a base URL:

| Target | Base URL | Contents |
| --- | --- | --- |
| Staging (`publish-staging`) | `https://danlooo.github.io/fair-geo-fm/main` | docs site, process graphs, runtime archives, STAC catalog |
| Release (`publish-release`) | `https://zenodo.org/records/<record-id>/files` | process graphs, UDFs, model READMEs, runtime archives, STAC catalog, full source archive |

The staging target is rebuilt automatically after every successful push build.
Nothing is published unless the `build` workflow succeeded for the commit, so a
broken process graph or archive is never deployed. Staging URLs are
**overwritten on every deployment** and are only for checking a change before a
release.

### Build and publish

Four workflows carry a commit from validation to publication.

- `build` runs on every push, pull request and `workflow_dispatch`. It renders
  each `models/*/process_graph.json.j2` with its `udf.py` and `config.json` and
  fails on an invalid template, an invalid `config.json` or a runtime reference
  that does not match `runtimes/`; it renders each model page from the shared
  `models/model.md.j2` as well, so a broken template fails before the docs or
  release pipeline sees it. It also builds the STAC catalog into a temporary
  directory, so an invalid `stac.json` or missing weights URL fails the build.
  It also
  builds each `runtimes/*/requirements.txt` into a deterministic
  `dependencies.zip`, smoke-tests the imports on the pinned Python version,
  writes `checksums.sha256`, and uploads all of it as the `dependency-archives`
  artifact (30 days). Unchanged archives are restored from a cache keyed on the
  requirements files instead of rebuilt.
- `publish-staging` deploys the documentation site to GitHub Pages. It is triggered by a
  completed `build` run, refuses to publish unless that run was a successful
  push build for the commit, and takes the runtime archives from the build's
  artifact.
- `test` runs the documented openEO example of every model against CDSE
  (`scripts/test-models.py`). `publish-release` calls it for the released tag
  before its Zenodo job, so a model whose example does not run blocks the
  upload. It can also be started by hand with `workflow_dispatch`; see
  [openEO credentials](#openeo-credentials-for-the-test-workflow).
- `publish-release` uploads a release to Zenodo (below). It is triggered by a
  published GitHub release.

`scripts/build-process-graph.py` and `scripts/publish-release.py` share
`scripts/process_graphs.py`, which owns template rendering and the `config.json`
and runtime validation, so the build and the release render graphs the same way.
After adding a model, check it locally with
`uv run scripts/build-process-graph.py models/<model>` (staging base URL) and
preview the site with `npm run dev`; both render from the working tree.

Releases are never automatic. A maintainer creates a GitHub release by hand,
which is the release decision, and the `publish-release` workflow archives the
tag on Zenodo. It refuses to guess URLs: it opens a new version of the
project's concept record (**10.5281/zenodo.24275707**) through Zenodo's
InvenioRDM API, reserves the version DOI first, renders every process graph
against the version's file URL, uploads the graphs, the runtime archives and a
source archive of the released commit, and publishes the version last. Every
release therefore adds a version to the same root record; the concept DOI
always resolves to the latest version. If Zenodo reports that concept record as
deleted (HTTP 410), the script starts a new root record and reports its concept
DOI; update `CONCEPT_DOI` in `scripts/process_graphs.py`, `DOCS_ZENODO_CONCEPT_ID`
in `scripts/prepare-docs.mjs`, the **`ZENODO_CONCEPT_DOI`** repository variable
and the DOI badge in `README.md` to it. Each
model contributes its rendered `process_graph.json`, `udf.py` and `README.md`;
the UDF is also embedded in the graph, and `config.json` / `stac.json` stay in
the source archive. The README's code samples link to the
record's own graph while its documentation link points at the versioned Pages
site. The record also carries the generated STAC catalog under `stac/`, whose
asset URLs point at the record's own files. The resulting record has many files
under custom paths —

    https://zenodo.org/records/<record-id>/files/models/tessera_v2/process_graph.json
    https://zenodo.org/records/<record-id>/files/models/tessera_v2/udf.py
    https://zenodo.org/records/<record-id>/files/models/tessera_v2/README.md
    https://zenodo.org/records/<record-id>/files/runtimes/py311-torch231.zip
    https://zenodo.org/records/<record-id>/files/stac/catalog.json
    https://zenodo.org/records/<record-id>/files/danlooo/fair-geo-fm-v0.2.zip

Inside the uploaded catalog, STAC document links (`self`, `root`, `child`,
`item`, `collection`) are absolute URLs on the API's `/content` route instead,

    https://zenodo.org/api/records/<record-id>/files/stac/catalog.json/content

because Zenodo's plain file route sends no `Access-Control-Allow-Origin`
header and browser-based clients such as STAC Browser cannot read it. Asset
URLs continue to use the plain record file route.

The DOI (`https://doi.org/<doi>`) is the citable identifier of the record but
does not serve individual files: `doi.org` treats any suffix as part of the DOI
and returns 404. Link files through the record URL instead.

— so Zenodo's automatic GitHub integration cannot be used; it archives one
repository snapshot under its own paths and cannot add the rendered graphs.
The source archive is exported with `git archive` from the released tag and
uploaded under the path the GitHub integration would have used, so the record
is self-contained. The workflow talks to the Zenodo REST API
(<https://developers.zenodo.org/>) through `scripts/publish-release.py`.

### openEO credentials for the test workflow

The `test` workflow runs each model's documented example on the Copernicus
Data Space Ecosystem (CDSE) before `publish-release` may upload it. It uses the
OIDC client credentials flow, the only non-interactive option on CDSE (HTTP
Basic is not offered, and a refresh token would tie releases to a person).

1. Create an OAuth client in the Sentinel Hub dashboard under
   <https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings>. Its
   client id and secret also work for openEO. For a project-owned identity,
   request a service account through the
   [CDSE help center](https://helpcenter.dataspace.copernicus.eu/hc/en-gb/requests/new)
   instead; its jobs and processing credits are separate from a personal
   account.
2. Add the repository secrets **`OPENEO_AUTH_CLIENT_ID`** and
   **`OPENEO_AUTH_CLIENT_SECRET`** (Settings → Secrets and variables → Actions).
   Keep them repository-level: the `test` job does not use the protected
   `release` environment.
3. Optionally set the repository variable **`OPENEO_AUTH_PROVIDER_ID`**; CDSE
   is the only provider, so it can stay unset.

To reproduce a CI run locally:

```sh
OPENEO_AUTH_CLIENT_ID=... OPENEO_AUTH_CLIENT_SECRET=... \
    uv run scripts/test-models.py --model tessera_v1 --keep-jobs
```

The script submits each graph inline, because a draft Zenodo record is private
and its files cannot be fetched by the CDSE back-end. `--base-url` therefore
only selects the public runtime archive (`/main` on Pages by default); the
build workflow already checks that the released tag's archive imports.

### Zenodo credentials

Add the repository secret **`ZENODO_ACCESS_TOKEN`** (Settings → Secrets and
variables → Actions). Create it at <https://zenodo.org/account/settings/applications/>
with the `deposit:write` and `deposit:actions` scopes. The optional repository
variable **`ZENODO_API_URL`** overrides the API base; set it to
`https://sandbox.zenodo.org/api` to rehearse against the Zenodo sandbox. When
rehearsing against the sandbox, also set **`ZENODO_CONCEPT_DOI`** to the
sandbox concept record's DOI; it defaults to the production record
`10.5281/zenodo.24275707`.

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
- Before the upload, `test` runs every model's documented example for the tag.
  If one fails, the Zenodo job is skipped and no draft is created.
- The Zenodo job waits for approval by the protected `release` environment.
- `dry_run` defaults to `true` for `workflow_dispatch`. It reserves a DOI,
  renders and uploads every file, writes the planned URLs to the run summary,
  and deletes the draft. `ZENODO_API_URL` can point at the sandbox.
- Creating a new GitHub release adds a new version to the same Zenodo concept
  record and gives that version its own DOI. The concept DOI
  `10.5281/zenodo.24275707` always resolves to the latest version. Re-running a
  failed upload against the same draft should use `--deposition-id` (the draft
  id is printed and written to the run summary); re-running a release that was
  already published is refused, because its version is already the latest.
- Documentation is versioned by git ref. The site root redirects to the newest
  `v*` tag, which is the current version; `main` and every older tag are
  published under `/<ref>/` and listed in the version switcher. Every page that
  is not the current release shows a notice above its heading: older tags an
  outdated-version banner and branches an unreleased-preview banner. Both link
  to the same page in the current version, or to its home page when the page
  does not exist there (`docs/.vitepress/theme/VersionBanner.vue`).
- A released tag's model page links to that tag's Zenodo version record and shows
  that version's DOI; a branch page (`main`) has no DOI, so it carries no release
  header at all. The docs build resolves the version records and DOIs from the
  concept record through the Zenodo API and falls back to the staging base and
  the concept DOI if that lookup fails.
- `publish-release` dispatches `publish-staging` after the Zenodo record is published,
  so the tag page is rebuilt against the new version record/DOI instead of the
  staging URLs from the deployment that ran when the tag was pushed. A
  rehearsal (`dry_run`) does not rebuild, because the draft is deleted.
- The staging process graph is served at the fixed `/main/models/<model>/`
  path on Pages. `prepare-docs.mjs` copies the rendered default-branch graph
  there, because a ref worktree never contains the git-ignored build output.
- The model page header follows the release automatically. Quoting the
  runtime sha256 in the model README is a manual step: `checksums.sha256` is a
  CI artifact, not part of the site or the published record.
  `scripts/build-process-graph.py --check` enforces that a present
  `process_graph.json` matches the template and that its archive URL ends with
  `runtimes/<runtime>.zip#feature_deps`. It cannot compare against a committed
  file, because the generated graph is git-ignored: in a fresh checkout it only
  validates the render.
