#!/usr/bin/env node
// Materialize the VitePress source tree for @viteplus/versions.
//
// The canonical pages stay where they belong in the repository (index.md,
// guide/, models/<name>/README.md, CONTRIBUTING.md). The versioning plugin
// expects one folder per version, so this script copies those pages into
//
//   docs/src/                 the current version (the newest v* tag)
//   docs/archive/<ref>/       every other branch and v* tag
//   docs/public/<ref>/        non-markdown process files served next to pages,
//                             plus the built runtime archives for the current
//                             version (runtimes/<name>.zip)
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
const currentVersion = process.env.DOCS_CURRENT_VERSION || newestTag() || "main";
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

// Foundation models live in models/. Archived refs that predate the rename keep
// them in processes/; both are published as models/ so their links match the
// current navigation.
function modelSourceDir(root) {
  return existsSync(join(root, "models")) ? "models" : "processes";
}

// Non-markdown files in each model directory (process_graph.json, udf.py,
// example.py, runtime.txt). They are served next to the model page, e.g.
// /<version>/models/tessera_v2/process_graph.json.
function siteAssets(root) {
  const assets = new Map();
  const source = modelSourceDir(root);
  const models = join(root, source);
  if (!existsSync(models)) return assets;
  for (const entry of readdirSync(models, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const dir = join("models", entry.name);
    for (const file of filesBelow(join(root, source, entry.name))) {
      if (file.endsWith(".md")) continue;
      assets.set(join(dir, file), join(root, source, entry.name, file));
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
// this script; read them from there. They are served as <name>.zip under the
// current version's pages, e.g. /v0.1/runtimes/py311-torch231.zip.
//
// Only the current version gets them: copying an archive into every archived
// version would multiply the site size (the torch archive alone is ~160 MB and
// a Pages site is capped at 1 GB) and would misattribute a rebuild as that old
// version's archive.
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

// Map canonical repository paths to site paths for one version.
// Model READMEs are the source of truth for their pages, so they become
// index.md; VitePress serves them as directory indexes.
function sitePages(root) {
  const pages = new Map();
  const source = modelSourceDir(root);
  const add = (from, to) => {
    const file = join(root, from);
    if (existsSync(file)) pages.set(to, file);
  };

  add("index.md", "index.md");
  for (const file of markdownFiles(join(root, "guide"))) {
    add(join("guide", file), join("guide", file));
  }
  add(join(source, "index.md"), "models/index.md");

  const models = join(root, source);
  if (existsSync(models)) {
    for (const entry of readdirSync(models, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      add(join(source, entry.name, "README.md"), join("models", entry.name, "index.md"));
    }
  }

  add("CONTRIBUTING.md", "contributing.md");
  return pages;
}

function writeTree(pages, destination) {
  rmSync(destination, { recursive: true, force: true });
  mkdirSync(destination, { recursive: true });
  for (const [to, from] of pages) {
    const target = join(destination, to);
    mkdirSync(dirname(target), { recursive: true });
    if (from.endsWith(".md")) {
      // Archived refs link to their layout (/processes/); publish it under the
      // current one so links keep resolving.
      const text = readFileSync(from, "utf8").replace(/\]\(\/processes\//g, "](/models/");
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
    // Only release tags are documentation versions; content-addressed archive
    // releases such as py311-torch231-<sha> are not.
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
  writeTree(sitePages(root), sourcesDir);
  writeTree(
    new Map([...siteAssets(root), ...runtimeAssets()]),
    join(publicDir, currentVersion),
  );
}

// Returns false when ref has no documentation site, so the caller falls back to
// the working tree instead of publishing an empty version.
function writeCurrentVersionFrom(ref) {
  return withWorktree(ref, (worktree) => {
    if (!sitePages(worktree).has("index.md")) {
      warn(`${ref} has no documentation site`);
      return false;
    }
    writeCurrentVersion(worktree);
    return true;
  });
}

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
  console.log(`prepare-docs: wrote ${currentVersion} to docs/src and docs/public`);
}

if (!withArchives) {
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
    warn(`skipping ${name}: version name ${version || "(empty)"} is already used`);
    continue;
  }

  const wasArchived = withWorktree(ref, (worktree) => {
    const pages = sitePages(worktree);
    // A ref without the site's home page predates the documentation site (or
    // is not a site at all); publishing it would only yield dead links.
    if (!pages.has("index.md")) {
      warn(`skipping ${name}: no VitePress site at this ref`);
      return false;
    }
    writeTree(pages, join(archiveDir, version));
    writeTree(siteAssets(worktree), join(publicDir, version));
    return true;
  });
  if (!wasArchived) continue;
  used.add(version);
  archived += 1;
  console.log(`prepare-docs: archived ${name} as docs/archive/${version}`);
}

mkdirSync(archiveDir, { recursive: true });
console.log(`prepare-docs: archived ${archived} ref(s)`);
