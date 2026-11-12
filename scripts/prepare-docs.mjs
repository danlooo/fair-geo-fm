#!/usr/bin/env node
// Materialize the VitePress source tree for @viteplus/versions.
//
// The canonical pages stay where they belong in the repository (index.md,
// guide/, processes/<name>/README.md, CONTRIBUTING.md). The versioning plugin
// expects one folder per version, so this script copies those pages into
//
//   docs/src/                 the current version (DOCS_CURRENT_VERSION)
//   docs/archive/<ref>/       every other branch and tag
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
  rmSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";

const repoRoot = resolve(process.cwd());
const docsRoot = join(repoRoot, "docs");
const sourcesDir = join(docsRoot, "src");
const archiveDir = join(docsRoot, "archive");
const currentVersion = process.env.DOCS_CURRENT_VERSION || "main";
const withArchives = process.argv.includes("--archives");

function git(args) {
  return execFileSync("git", args, { cwd: repoRoot, encoding: "utf8" }).trim();
}

function warn(message) {
  console.warn(`prepare-docs: ${message}`);
}

// All markdown files below dir, as paths relative to dir.
function markdownFiles(dir) {
  if (!existsSync(dir)) return [];
  const files = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      for (const file of markdownFiles(full)) files.push(join(entry.name, file));
    } else if (entry.isFile() && entry.name.endsWith(".md")) {
      files.push(entry.name);
    }
  }
  return files;
}

// Map canonical repository paths to site paths for one version.
// Process READMEs are the source of truth for their pages, so they become
// index.md; VitePress serves them as directory indexes.
function sitePages(root) {
  const pages = new Map();
  const add = (from, to) => {
    const file = join(root, from);
    if (existsSync(file)) pages.set(to, file);
  };

  add("index.md", "index.md");
  for (const file of markdownFiles(join(root, "guide"))) {
    add(join("guide", file), join("guide", file));
  }
  add("processes/index.md", "processes/index.md");

  const processes = join(root, "processes");
  if (existsSync(processes)) {
    for (const entry of readdirSync(processes, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      add(join("processes", entry.name, "README.md"), join("processes", entry.name, "index.md"));
    }
  }

  add("CONTRIBUTING.md", "contributing.md");
  return pages;
}

function writePages(pages, destination) {
  rmSync(destination, { recursive: true, force: true });
  mkdirSync(destination, { recursive: true });
  for (const [to, from] of pages) {
    const target = join(destination, to);
    mkdirSync(dirname(target), { recursive: true });
    cpSync(from, target);
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
    // A branch and a tag with the same name are one version; the branch wins.
    if (name !== currentVersion && !refs.has(name)) refs.set(name, name);
  }

  return [...refs.entries()].map(([name, ref]) => ({ name, ref }));
}

writePages(sitePages(repoRoot), sourcesDir);
console.log(`prepare-docs: wrote ${currentVersion} to docs/src`);

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

  const tempRoot = mkdtempSync(join(tmpdir(), "fair-geo-fm-docs-"));
  const worktree = join(tempRoot, "repo");
  try {
    git(["worktree", "add", "--detach", "--force", worktree, ref]);
    const pages = sitePages(worktree);
    // A ref without the site's home page predates the documentation site (or
    // is not a site at all); publishing it would only yield dead links.
    if (!pages.has("index.md")) {
      warn(`skipping ${name}: no VitePress site at this ref`);
      continue;
    }
    writePages(pages, join(archiveDir, version));
    used.add(version);
    archived += 1;
    console.log(`prepare-docs: archived ${name} as docs/archive/${version}`);
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

mkdirSync(archiveDir, { recursive: true });
console.log(`prepare-docs: archived ${archived} ref(s)`);
