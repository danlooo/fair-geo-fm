#!/usr/bin/env node
// Materialize the VitePress source tree for @viteplus/versions.
//
// The canonical pages stay where they belong in the repository (index.md,
// guide/, models/model.md.j2, CONTRIBUTING.md). The versioning plugin
// expects one folder per version, so this script copies those pages into
//
//   docs/src/                 the current version (the newest v* tag)
//   docs/archive/<ref>/       every other branch and v* tag
//   docs/public/<ref>/        non-markdown process files served next to pages
//   docs/public/main/runtimes/  built runtime archives for the default branch,
//                             overwritten on every deployment
//   docs/public/main/models/  rendered process graphs for the default branch
//
// Model pages of released tags link to that tag's Zenodo version record (its
// files, not the concept DOI); main and other branches link to the staging
// /main/ process graphs, which this script serves.
//
// The current version is the newest v* tag, so the site root redirects to that
// tag's URL (e.g. /v0.1/). Set DOCS_CURRENT_VERSION to override the name; the
// chosen name is also written to docs/.vitepress/current-version.json for the
// VitePress config. Set DOCS_CURRENT_REF to read the pages from a ref instead
// of the working tree (CI sets both); without it, local builds use the working
// tree so previewing edits works.
//
// Run `node scripts/prepare-docs.mjs` for the current version only, and
// `node scripts/prepare-docs.mjs --archives` to also archive refs. Archives
// are read from git, so the checkout must contain all branches and tags
// (`actions/checkout` with fetch-depth: 0).

import { execFileSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";

const repoRoot = resolve(process.cwd());
const docsRoot = join(repoRoot, "docs");
const sourcesDir = join(docsRoot, "src");
const archiveDir = join(docsRoot, "archive");
// VitePress copies docs/public as-is to the site root, so per-version process
// files end up next to their pages.
const publicDir = join(docsRoot, "public");
const withArchives = process.argv.includes("--archives");

function git(args) {
  return execFileSync("git", args, { cwd: repoRoot, encoding: "utf8" }).trim();
}

// The newest release tag, or "" when the repository has none yet.
function newestTag() {
  try {
    const tags = git(["tag", "--list", "v*", "--sort=version:refname"])
      .split("\n")
      .filter(Boolean);
    return tags.at(-1) ?? "";
  } catch {
    return "";
  }
}

// Before the first release the default branch is the current version.
const currentVersion =
  process.env.DOCS_CURRENT_VERSION || newestTag() || "main";
const currentRef = process.env.DOCS_CURRENT_REF || "";

// The VitePress config is a separate process, so hand it the chosen name.
mkdirSync(join(docsRoot, ".vitepress"), { recursive: true });
writeFileSync(
  join(docsRoot, ".vitepress", "current-version.json"),
  `${JSON.stringify({ version: currentVersion })}\n`,
);

function warn(message) {
  console.warn(`prepare-docs: ${message}`);
}

function hasRef(ref) {
  try {
    git(["rev-parse", "--verify", "--quiet", `${ref}^{commit}`]);
    return true;
  } catch {
    return false;
  }
}

// Run fn against a temporary worktree of ref, then remove it. Cloning into a
// worktree keeps the checkout (and its git index) untouched, so refs can be
// read without switching the working tree.
function withWorktree(ref, fn) {
  const tempRoot = mkdtempSync(join(tmpdir(), "fair-geo-fm-docs-"));
  const worktree = join(tempRoot, "repo");
  try {
    git(["worktree", "add", "--detach", "--force", worktree, ref]);
    return fn(worktree);
  } finally {
    try {
      git(["worktree", "remove", "--force", worktree]);
    } catch {
      rmSync(worktree, { recursive: true, force: true });
    }
    rmSync(tempRoot, { recursive: true, force: true });
    try {
      git(["worktree", "prune"]);
    } catch {
      // Nothing to prune.
    }
  }
}

// All files below dir, as paths relative to dir.
function filesBelow(dir, base = dir) {
  if (!existsSync(dir)) return [];
  const files = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...filesBelow(full, base));
    } else if (entry.isFile()) {
      files.push(relative(base, full));
    }
  }
  return files;
}

function markdownFiles(dir) {
  return filesBelow(dir).filter((file) => file.endsWith(".md"));
}

// Model pages are rendered from the shared models/model.md.j2 with the release
// values of the version being published. The models index template iterates a
// `models` list built from each model directory's config.json, the single
// source of truth for the model's display name, runtime and badge tags.
function modelConfig(dir) {
  const file = join(dir, "config.json");
  if (!existsSync(file)) return {};
  try {
    const config = JSON.parse(readFileSync(file, "utf8"));
    if (
      config === null ||
      typeof config !== "object" ||
      Array.isArray(config)
    ) {
      throw new Error("expected a JSON object");
    }
    return config;
  } catch (error) {
    throw new Error(`prepare-docs: ${file}: ${error.message}`);
  }
}

// The model's STAC metadata is optional for the docs build: a directory
// without stac.json simply gets no STAC links. Its `id` names the generated
// Item, so the docs link to the same file build-stac.py writes.
function stacConfig(dir) {
  const file = join(dir, "stac.json");
  if (!existsSync(file)) return {};
  try {
    const stac = JSON.parse(readFileSync(file, "utf8"));
    if (stac === null || typeof stac !== "object" || Array.isArray(stac)) {
      throw new Error("expected a JSON object");
    }
    return stac;
  } catch (error) {
    throw new Error(`prepare-docs: ${file}: ${error.message}`);
  }
}

const HTML_ESCAPES = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    (character) => HTML_ESCAPES[character],
  );
}

// The `models` list models/index.md.j2 iterates over. A directory is a model
// when it has a config.json; other directories (e.g. shared runtime files) are
// skipped. Values are HTML-escaped because the template interpolates them into
// markup. Cards deliberately omit the DOI: it identifies the release, not the
// individual model, so it belongs on the model page header rather than repeated
// on every card.
function modelCards(root) {
  const modelsDir = join(root, "models");
  if (!existsSync(modelsDir)) return [];

  const models = [];

  for (const entry of readdirSync(modelsDir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const dir = join(modelsDir, entry.name);
    if (!existsSync(join(dir, "config.json"))) continue;

    const config = modelConfig(dir);
    const name = config.name.trim();
    const tags = Array.isArray(config.tags)
      ? config.tags
          .filter((tag) => typeof tag === "string" && tag.trim())
          .map((tag) => escapeHtml(tag.trim()))
      : [];
    const description =
      typeof config.description === "string" ? config.description.trim() : "";

    models.push({
      slug: entry.name,
      name: escapeHtml(name),
      description: escapeHtml(description),
      tags,
    });
  }

  return models;
}

// Public base of the generated STAC tree for a documentation version: the
// version's Zenodo files for a released tag, the staging /main tree otherwise.
// Mirrors the process-graph base in modelTemplateValues.
function stacBaseUrl(version) {
  const base = zenodoReleases.get(version)?.filesBase ?? `${siteBaseUrl}/main`;
  return base.replace(/\/+$/, "");
}

function renderModelsIndex(text, source, version) {
  // <root>/models/index.md.j2 -> <root>
  const root = dirname(dirname(source));
  const base = stacBaseUrl(version);
  return renderTemplate(
    text,
    {
      models: modelCards(root),
      stac_catalog_url: `${base}/stac/catalog.json`,
      stac_collection_url: `${base}/stac/collections/fair-geo-fm-models/collection.json`,
    },
    source,
  );
}

// Non-markdown, non-template files in each model directory
// (process_graph.json, udf.py, config.json, stac.json). They are served next
// to the model page, e.g. /<version>/models/tessera_v2/process_graph.json.
// config.json is the model's single source of truth (display name, runtime,
// badge tags) and doubles as the published model record. The .j2 source
// templates are build inputs, not public assets.
function siteAssets(root) {
  const assets = new Map();
  const models = join(root, "models");
  if (!existsSync(models)) return assets;
  for (const entry of readdirSync(models, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const dir = join("models", entry.name);
    for (const file of filesBelow(join(models, entry.name))) {
      if (file.endsWith(".md") || file.endsWith(".j2")) continue;
      assets.set(join(dir, file), join(models, entry.name, file));
    }
  }
  return assets;
}

// Built runtime archives, as site path -> source file.
//
// They are build outputs (runtimes/<name>/requirements.txt zipped into
// <name>/dependencies.zip by scripts/create-python-dependency-archive.py) and
// are git-ignored, so they are never present in a ref worktree. CI downloads
// the build job's dependency-archives artifact into the checkout before running
// this script; read them from there.
function runtimeAssets() {
  const assets = new Map();
  const runtimes = join(repoRoot, "runtimes");
  if (!existsSync(runtimes)) return assets;
  for (const entry of readdirSync(runtimes, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const archive = join(runtimes, entry.name, "dependencies.zip");
    if (existsSync(archive)) {
      assets.set(join("runtimes", `${entry.name}.zip`), archive);
    }
  }
  return assets;
}

// Serve the archives at a fixed /main/ path, independent of the documentation
// version, replacing them on every deployment: released versions are archived
// elsewhere (Zenodo), so Pages only ever carries the latest default-branch
// build. e.g. /fair-geo-fm/main/runtimes/py311-torch231.zip.
function writeRuntimeAssets() {
  const assets = runtimeAssets();
  if (assets.size === 0) return;
  // Copy rather than writeTree: docs/public/main may already hold the archived
  // main branch's pages and model files, which writeTree would remove.
  for (const [to, from] of assets) {
    const target = join(publicDir, "main", to);
    mkdirSync(dirname(target), { recursive: true });
    cpSync(from, target);
  }
  console.log(
    `prepare-docs: wrote ${assets.size} runtime archive(s) under docs/public/main`,
  );
}

// Rendered process graphs are git-ignored build outputs, so ref worktrees
// never contain them. The publish workflow renders the default branch's graphs
// in the checkout; serve them at the fixed /main/ path that model pages link
// to. Copy rather than writeTree, for the same reason as writeRuntimeAssets.
function writeStagingProcessGraphs() {
  const models = join(repoRoot, "models");
  if (!existsSync(models)) return;
  let count = 0;
  for (const entry of readdirSync(models, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const graph = join(models, entry.name, "process_graph.json");
    if (!existsSync(graph)) continue;
    const target = join(
      publicDir,
      "main",
      "models",
      entry.name,
      "process_graph.json",
    );
    mkdirSync(dirname(target), { recursive: true });
    cpSync(graph, target);
    count += 1;
  }
  if (count > 0) {
    console.log(
      `prepare-docs: wrote ${count} staging process graph(s) under docs/public/main`,
    );
  }
}

// The STAC catalog is generated by scripts/build-stac.py into ``stac/`` (a
// git-ignored build output), so ref worktrees never contain one. Like the
// process graphs, the staging catalog is served at the fixed /main/ path and
// replaced on every deployment; released catalogs live in the Zenodo record.
function writeStagingStac() {
  const source = join(repoRoot, "stac");
  if (!existsSync(source)) return;
  const target = join(publicDir, "main", "stac");
  mkdirSync(dirname(target), { recursive: true });
  rmSync(target, { recursive: true, force: true });
  cpSync(source, target, { recursive: true });
  console.log("prepare-docs: wrote the staging STAC catalog under docs/public/main/stac");
}

// Public origin of the documentation site. VitePress serves every version
// under /fair-geo-fm/<version>/, while process graphs are only rebuilt under
// /main/ on every deployment.
const siteBaseUrl = (
  process.env.DOCS_SITE_BASE_URL || "https://danlooo.github.io/fair-geo-fm"
).replace(/\/+$/, "");

// Unversioned URLs open the newest release, just like the site root: the
// versioning plugin redirects only "/" and locale roots, so write a tiny page
// for every route the current version publishes, e.g.
// /fair-geo-fm/models/tessera_v2/ -> /fair-geo-fm/<currentVersion>/models/tessera_v2/.
// VitePress copies docs/public to the site root, so these work on the deployed
// site and in local preview.
function redirectPage(target) {
  const href = target.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
  return `<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Redirecting to the latest release</title>
    <link rel="canonical" href="${href}">
    <script>location.replace(${JSON.stringify(target)} + location.search + location.hash)</script>
    <meta http-equiv="refresh" content="0; url=${href}">
  </head>
  <body>
    <p>Redirecting to the <a href="${href}">latest release</a>.</p>
  </body>
</html>
`;
}

function writeUnversionedRedirects() {
  const basePath = new URL(siteBaseUrl).pathname.replace(/\/+$/, "");
  let count = 0;
  for (const page of markdownFiles(sourcesDir)) {
    if (page === "index.md") continue;
    const route = page.endsWith("/index.md")
      ? page.slice(0, -"index.md".length)
      : page.replace(/\.md$/, ".html");
    const target = `${basePath}/${currentVersion}/${route}`;
    const file = route.endsWith("/")
      ? join(publicDir, route, "index.html")
      : join(publicDir, route);
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(file, redirectPage(target));
    count += 1;
  }
  console.log(`prepare-docs: wrote ${count} unversioned redirect(s)`);
}

// Released tags are archived on Zenodo as versions of one concept record.
// Kept in sync with CONCEPT_DOI in scripts/process_graphs.py and
// DEFAULT_CONCEPT_DOI in scripts/publish-release.py.
const zenodoConceptId = process.env.DOCS_ZENODO_CONCEPT_ID || "24275707";
const zenodoConceptDoi =
  process.env.DOCS_ZENODO_CONCEPT_DOI || `10.5281/zenodo.${zenodoConceptId}`;
const zenodoApiUrl = (
  process.env.DOCS_ZENODO_API_URL || "https://zenodo.org/api"
).replace(/\/+$/, "");
const zenodoSiteUrl = zenodoApiUrl.replace(/\/api$/, "");

// Tag -> public file base of its Zenodo version, e.g.
// v0.1.0 -> https://zenodo.org/records/24275708/files, plus that version's DOI
// for the model page header. One unauthenticated request; a failure degrades to
// the staging /main base and the concept DOI instead of breaking the docs
// build. ``all_versions=true`` is required: without it the search endpoint only
// returns the latest version of the concept record.
async function fetchZenodoReleases() {
  const releases = new Map();
  const query = `parent.id:${zenodoConceptId}`;
  try {
    const response = await fetch(
      `${zenodoApiUrl}/records?q=${encodeURIComponent(query)}` +
        "&size=25&all_versions=true",
      {
        headers: {
          Accept: "application/json",
          // Zenodo rejects requests without a descriptive User-Agent.
          "User-Agent":
            "fair-geo-fm-docs/1.0 (+https://danlooo.github.io/fair-geo-fm/)",
        },
        signal: AbortSignal.timeout(10000),
      },
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const body = await response.json();
    for (const hit of body?.hits?.hits ?? []) {
      const version = hit?.metadata?.version;
      if (typeof version === "string" && version) {
        releases.set(version, {
          filesBase: `${zenodoSiteUrl}/records/${hit.id}/files`,
          doi: typeof hit.doi === "string" ? hit.doi : "",
        });
      }
    }
    console.log(`prepare-docs: resolved ${releases.size} Zenodo release(s)`);
  } catch (error) {
    warn(
      `could not resolve Zenodo releases (${error.message}); ` +
        "tagged versions link to the staging base and the concept DOI",
    );
  }
  return releases;
}

// The variables a model README template can use, mirroring the ones in
// scripts/process_graphs.py. Released tags link to their immutable Zenodo
// files; main and branches link to the staging graphs rebuilt under /main.
// The documentation page itself is always versioned. ``release`` is true for
// v* tags and false for branches, so archived templates that gate development
// notices on it still render; the current template relies on the site-wide
// banner in docs/.vitepress/theme/VersionBanner.vue instead.
// ``release_version`` / ``release_url`` / ``doi`` describe the release the page
// advertises: a tag page links itself, a branch page (main) links the newest
// release, the same one the site root redirects to. Only a release has a DOI,
// so ``doi`` stays empty on a branch page and the template omits that link.
// External STAC browser. Its /external/ route takes the target URL without the
// scheme, e.g. /external/danlooo.github.io/fair-geo-fm/main/stac/...; wrapping
// the Item lets readers browse it instead of downloading raw JSON.
const stacBrowserBase = "https://browser.moregeo.it/external/";

function stacBrowserUrl(url) {
  return `${stacBrowserBase}${url.replace(/^https?:\/\//, "")}`;
}

function modelTemplateValues(version, model, stacItemId = model) {
  const isRelease = /^v\d+(?:\.\d+)*$/.test(version);
  const releaseVersion = isRelease ? version : newestTag() || version;
  const release = zenodoReleases.get(releaseVersion);
  const filesBaseUrl =
    zenodoReleases.get(version)?.filesBase ?? `${siteBaseUrl}/main`;
  const docsBaseUrl = `${siteBaseUrl}/${version}`;
  const stacBase = `${filesBaseUrl}/stac`;
  return {
    base_url: filesBaseUrl,
    model,
    release: isRelease,
    process_graph_url: `${filesBaseUrl}/models/${model}/process_graph.json`,
    docs_url: `${docsBaseUrl}/models/${model}/`,
    stac_catalog_url: `${stacBase}/catalog.json`,
    stac_collection_url: `${stacBase}/collections/fair-geo-fm-models/collection.json`,
    stac_item_url: stacBrowserUrl(
      `${stacBase}/collections/fair-geo-fm-models/items/${stacItemId}.json`,
    ),
    release_version: releaseVersion,
    release_url: `${siteBaseUrl}/${releaseVersion}/models/${model}/`,
    doi: isRelease ? release?.doi || zenodoConceptDoi : "",
  };
}

// Minimal Jinja2 subset: ``{{ path.to.value }}`` variables,
// ``{% if [not] path %} ... {% endif %}`` blocks and
// ``{% for item in list.path %} ... {% endfor %}`` loops, inline or spanning
// whole lines. An unknown variable is an error rather than a published page
// that keeps a literal ``{{ ... }}``.
function templateValue(values, path, source) {
  let current = values;
  for (const part of path.split(".")) {
    if (
      current === null ||
      current === undefined ||
      (typeof current !== "object" && typeof current !== "function") ||
      !(part in current)
    ) {
      throw new Error(
        `prepare-docs: ${source}: unknown template variable ${path}`,
      );
    }
    current = current[part];
  }
  return current;
}

function renderModelTemplate(text, version, model, source, config, stac) {
  // config.json is the model's single source of truth; the page reads e.g.
  // config.name, config.description, config.parameters and config.links from
  // it instead of duplicating them. stac.json names the generated Item so the
  // page links the same file build-stac.py writes.
  const itemId = typeof stac?.id === "string" && stac.id ? stac.id : model;
  const values = { ...modelTemplateValues(version, model, itemId), config };
  return renderTemplate(text, values, source);
}

function renderTemplate(text, values, source) {
  const render = (template, scope) => {
    // Loops that occupy their own lines own those lines. The body is rendered
    // recursively, so nested blocks and the loop variable resolve in scope.
    const expandLoop = (item, path, body) => {
      const items = templateValue(scope, path, source);
      if (!Array.isArray(items)) {
        throw new Error(
          `prepare-docs: ${source}: ${path} must be a list to iterate over`,
        );
      }
      return items
        .map((entry) => render(body, { ...scope, [item]: entry }))
        .join("");
    };
    template = template.replace(
      /^[ \t]*\{%\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*%\}[ \t]*\n([\s\S]*?)^[ \t]*\{%\s*endfor\s*%\}[ \t]*\n/gm,
      (match, item, path, body) => expandLoop(item, path, body),
    );
    // Inline tags are part of a line, so keep the rest of it, newline included.
    template = template.replace(
      /\{%\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*%\}([\s\S]*?)\{%\s*endfor\s*%\}/g,
      (match, item, path, body) => expandLoop(item, path, body),
    );

    const shown = (negated, path) => {
      const current = templateValue(scope, path, source);
      // Jinja treats an empty list as falsy; JavaScript arrays are always truthy.
      const truthy = Array.isArray(current)
        ? current.length > 0
        : Boolean(current);
      return negated ? !truthy : truthy;
    };
    // Tags that occupy their own lines own those lines, so a hidden block
    // leaves no blank line behind.
    let rendered = template.replace(
      /^[ \t]*\{%\s*if\s+(not\s+)?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*%\}[ \t]*\n([\s\S]*?)^[ \t]*\{%\s*endif\s*%\}[ \t]*\n/gm,
      (match, negated, path, body) => (shown(negated, path) ? body : ""),
    );
    rendered = rendered.replace(
      /\{%\s*if\s+(not\s+)?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*%\}([\s\S]*?)\{%\s*endif\s*%\}/g,
      (match, negated, path, body) => (shown(negated, path) ? body : ""),
    );
    return rendered.replace(
      /\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\}\}/g,
      (match, path) => templateValue(scope, path, source),
    );
  };

  return render(text, values);
}

// Map canonical repository paths to site paths for one version.
// The shared model template is the source of truth for every model page, so it
// becomes each model's index.md; VitePress serves them as directory indexes.
function sitePages(root, version) {
  const pages = new Map();
  const add = (from, to) => {
    const file = join(root, from);
    if (existsSync(file)) pages.set(to, file);
  };

  add("index.md", "index.md");
  for (const file of markdownFiles(join(root, "guide"))) {
    add(join("guide", file), join("guide", file));
  }
  add("models/index.md.j2", "models/index.md");

  const models = join(root, "models");
  const template = join(models, "model.md.j2");
  if (existsSync(models) && existsSync(template)) {
    for (const entry of readdirSync(models, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      if (!existsSync(join(models, entry.name, "config.json"))) continue;
      pages.set(join("models", entry.name, "index.md"), template);
    }
  }

  add("CONTRIBUTING.md", "contributing.md");
  return pages;
}

function writeTree(root, pages, destination, version) {
  rmSync(destination, { recursive: true, force: true });
  mkdirSync(destination, { recursive: true });
  for (const [to, from] of pages) {
    const target = join(destination, to);
    mkdirSync(dirname(target), { recursive: true });
    if (from.endsWith(".md") || from.endsWith(".md.j2")) {
      let text = readFileSync(from, "utf8");
      if (from.endsWith(".md.j2")) {
        if (to === "models/index.md") {
          text = renderModelsIndex(text, from, version);
        } else {
          const model = to.split("/")[1];
          const dir = join(root, "models", model);
          const config = modelConfig(dir);
          const stac = stacConfig(dir);
          text = renderModelTemplate(text, version, model, from, config, stac);
        }
      }
      writeFileSync(target, text);
    } else {
      cpSync(from, target);
    }
  }
}

// Branch and tag names are URL segments, so flatten them to a single segment.
function versionName(ref) {
  return ref.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
}

function refsToArchive() {
  const refs = new Map();

  const branches = git([
    "for-each-ref",
    "--format=%(refname:lstrip=3)",
    "refs/remotes/origin",
  ])
    .split("\n")
    .filter((name) => name && name !== "HEAD");
  for (const name of branches) {
    if (name !== currentVersion) refs.set(name, `origin/${name}`);
  }

  for (const name of git([
    "for-each-ref",
    "--format=%(refname:short)",
    "refs/tags",
  ])
    .split("\n")
    .filter(Boolean)) {
    // Only release tags are documentation versions; other tags (if any) are
    // not published as site versions.
    if (!name.startsWith("v")) continue;
    // A branch and a tag with the same name are one version; the branch wins.
    if (name !== currentVersion && !refs.has(name)) refs.set(name, name);
  }

  return [...refs.entries()].map(([name, ref]) => ({ name, ref }));
}

// A full archive run replaces the whole public tree, dropping versions of
// deleted refs as well as the current version's files.
if (withArchives) rmSync(publicDir, { recursive: true, force: true });

function writeCurrentVersion(root) {
  writeTree(root, sitePages(root, currentVersion), sourcesDir, currentVersion);
  writeTree(root, siteAssets(root), join(publicDir, currentVersion), currentVersion);
}

// Local dev runs without `--archives`, so docs/archive can be stale (or absent).
// Write the working tree to the staging /main/ path as well, so the URL
// advertised as "docs dev" previews uncommitted changes instead of an old
// origin/main snapshot. A full `--archives` build uses the real ref instead.
function writeWorkingTreePreview() {
  if (currentVersion === "main") return;
  writeTree(repoRoot, sitePages(repoRoot, "main"), join(archiveDir, "main"), "main");
  writeTree(repoRoot, siteAssets(repoRoot), join(publicDir, "main"), "main");
  console.log(
    "prepare-docs: wrote the working tree to /main for local preview",
  );
}

// Returns false when ref has no documentation site, so the caller falls back to
// the working tree instead of publishing an empty version.
function writeCurrentVersionFrom(ref) {
  return withWorktree(ref, (worktree) => {
    if (!sitePages(worktree, currentVersion).has("index.md")) {
      warn(`${ref} has no documentation site`);
      return false;
    }
    writeCurrentVersion(worktree);
    return true;
  });
}

// Resolve the version records before rendering pages, so a tag's README links
// to that tag's record and DOI rather than the concept/root DOI.
const zenodoReleases = await fetchZenodoReleases();

// The navbar STAC link follows the version being viewed: a released tag opens
// the catalog in its Zenodo record, while main and unresolved/archived refs use
// the staging tree. VitePress reads this file from config.mjs; a missing file
// makes the component fall back to main.
const stacBases = { main: `${siteBaseUrl}/main` };
for (const [version, release] of zenodoReleases) {
  stacBases[version] = release.filesBase;
}
writeFileSync(
  join(docsRoot, ".vitepress", "stac-bases.json"),
  `${JSON.stringify(stacBases, null, 2)}\n`,
);

let fromRef = false;
if (currentRef) {
  if (hasRef(currentRef)) {
    fromRef = writeCurrentVersionFrom(currentRef);
  } else {
    warn(`${currentRef} does not exist`);
  }
}
if (fromRef) {
  console.log(
    `prepare-docs: wrote ${currentVersion} (from ${currentRef}) to docs/src and docs/public`,
  );
} else {
  writeCurrentVersion(repoRoot);
  console.log(
    `prepare-docs: wrote ${currentVersion} to docs/src and docs/public`,
  );
}

writeUnversionedRedirects();

if (!withArchives) {
  writeWorkingTreePreview();
  writeStagingProcessGraphs();
  writeRuntimeAssets();
  writeStagingStac();
  process.exit(0);
}

const used = new Set([currentVersion]);
let archived = 0;

// Drop archives left behind by earlier runs or deleted refs.
rmSync(archiveDir, { recursive: true, force: true });
mkdirSync(archiveDir, { recursive: true });

for (const { name, ref } of refsToArchive()) {
  const version = versionName(name);
  if (!version || used.has(version)) {
    warn(
      `skipping ${name}: version name ${version || "(empty)"} is already used`,
    );
    continue;
  }

  const wasArchived = withWorktree(ref, (worktree) => {
    const pages = sitePages(worktree, version);
    // A ref without the site's home page predates the documentation site (or
    // is not a site at all); publishing it would only yield dead links.
    if (!pages.has("index.md")) {
      warn(`skipping ${name}: no VitePress site at this ref`);
      return false;
    }
    writeTree(worktree, pages, join(archiveDir, version), version);
    writeTree(worktree, siteAssets(worktree), join(publicDir, version), version);
    return true;
  });
  if (!wasArchived) continue;
  used.add(version);
  archived += 1;
  console.log(`prepare-docs: archived ${name} as docs/archive/${version}`);
}

mkdirSync(archiveDir, { recursive: true });
console.log(`prepare-docs: archived ${archived} ref(s)`);

// Last, so an archived `main` version written above does not replace them.
writeStagingProcessGraphs();
writeRuntimeAssets();
writeStagingStac();
