import { readFileSync } from "node:fs";

import { defineVersionedConfig } from "@viteplus/versions";
import { tabsMarkdownPlugin } from "vitepress-plugin-tabs";

// The site's current version is the newest v* tag. Every other branch and v*
// tag that contains a site is copied to docs/archive/<ref>/ by
// scripts/prepare-docs.mjs, so one build serves all versions and the version
// switcher links between them. `latest` defaults to the current version, so
// the site root redirects to that tag's URL (e.g. /v0.1/).
//
// prepare-docs.mjs runs first and writes the resolved name to
// current-version.json; before the first release it is the default branch.
function resolveCurrentVersion() {
  if (process.env.DOCS_CURRENT_VERSION) return process.env.DOCS_CURRENT_VERSION;
  try {
    const file = readFileSync(
      new URL("./current-version.json", import.meta.url),
      "utf8",
    );
    return JSON.parse(file).version;
  } catch {
    return "main";
  }
}

const currentVersion = resolveCurrentVersion();

// STAC catalog URLs per version, written by prepare-docs.mjs: a released tag's
// CORS-enabled Zenodo API URL or the staging /main tree. The navbar component
// falls back to main when a version is absent.
function resolveStacBases() {
  try {
    return JSON.parse(
      readFileSync(new URL("./stac-bases.json", import.meta.url), "utf8"),
    );
  } catch {
    return {};
  }
}

const stacBases = resolveStacBases();

// Zenodo record URLs per version, written by prepare-docs.mjs: a released tag's
// own record or, for main and unresolved refs, the concept DOI, which resolves
// to the latest release. The navbar component falls back the same way.
function resolveZenodoRecords() {
  try {
    return JSON.parse(
      readFileSync(new URL("./zenodo-records.json", import.meta.url), "utf8"),
    );
  } catch {
    return {};
  }
}

const zenodoRecords = resolveZenodoRecords();

// Map a versioned page back to the canonical file in the repository.
// `filePath` is the generated folder (docs/src or docs/archive/<ref>), so the
// version prefix and archive segment are stripped here. The function is
// serialized into the client bundle, so it must not reference anything outside
// its own scope.
function editLinkFor(page) {
  const parts = page.filePath.split("/");
  const rest = (parts[0] === "src" ? parts.slice(1) : parts.slice(2)).join("/");
  const source =
    rest === "contributing.md"
      ? "CONTRIBUTING.md"
      : rest === "models/index.md"
        ? "models/index.md.j2"
        : rest.replace(/^models\/([^/]+)\/index\.md$/, "models/$1/README.md.j2");
  // docs/src is the newest release, a snapshot of a v* tag, so edits are
  // proposed against the default branch. Archived pages have no edit link, so
  // this only produces URLs for that snapshot.
  return `https://github.com/danlooo/fair-geo-fm/edit/main/${source}`;
}

const base = "/fair-geo-fm/";

// Rewrite origin-root icon probes to the same files under `base`. Hooks into
// both dev and preview servers; the static build does not need it.
function rewriteRootIcons(server) {
  server.middlewares.use((req, _res, next) => {
    const path = (req.url || "").split("?")[0];
    if (path === "/favicon.ico" || path === "/favicon.svg") {
      req.url = `${base}${path.slice(1)}`;
    }
    next();
  });
}

export default defineVersionedConfig({
  base,
  head: [
    // Icon set written from the theme sources by scripts/prepare-docs.mjs.
    // The SVG is preferred; the ICO is the fallback browsers request by
    // convention, and the PNG is used when iOS saves the site to the home
    // screen.
    ["link", { rel: "icon", type: "image/svg+xml", href: `${base}favicon.svg` }],
    ["link", { rel: "icon", sizes: "any", href: `${base}favicon.ico` }],
    ["link", { rel: "apple-touch-icon", sizes: "180x180", href: `${base}apple-touch-icon.png` }],
  ],
  // Direct .html URLs keep the version switcher working on a full page load;
  // clean URLs would make it link to /<version>/guide/index.
  cleanUrls: false,
  title: "fair-geo-fm",
  description: "Geospatial foundation models ready to use in openEO.",
  sitemap: { hostname: "https://danlooo.github.io" },

  // The dev server's initial HTML has no icon link (VitePress adds it on the
  // client), so browsers probe /favicon.ico at the origin root before the app
  // loads. The site is served under `base`, so map those root requests onto
  // the real files; otherwise dev logs a 404 for an icon the site does have.
  vite: {
    plugins: [
      {
        name: "fair-geo-fm-root-icon-fallback",
        configureServer: rewriteRootIcons,
        configurePreviewServer: rewriteRootIcons,
      },
    ],
  },

  // `::: tabs` / `== Tab` blocks, rendered by docs/.vitepress/theme/.
  markdown: {
    config(md) {
      md.use(tabsMarkdownPlugin);
    },
  },

  // ProcessGraph.vue renders <openeo-model-builder>, a custom element from the
  // prebuilt @openeo/vue-components web component bundle. Mark the whole
  // namespace as custom elements so Vue leaves them to the browser.
  vue: {
    template: {
      compilerOptions: {
        isCustomElement: (tag) => tag.startsWith("openeo-"),
      },
    },
  },

  // The plugin rewrites links in markdown, but not URLs in frontmatter such as
  // the home page hero buttons.
  transformPageData(pageData) {
    const version = pageData.relativePath.split("/")[0];
    const actions = pageData.frontmatter?.hero?.actions;
    const frontmatter = { ...pageData.frontmatter };
    // Archived pages are snapshots whose paths may predate renames, so an edit
    // link would point at a file that does not exist at that ref.
    if (version !== currentVersion) frontmatter.editLink = false;
    // Model pages embed the interactive process graph; custom.css uses this
    // page class to widen their content column.
    if (/(^|\/)models\/[^/]+\/index\.md$/.test(pageData.relativePath)) {
      frontmatter.pageClass = [frontmatter.pageClass, "model-page"]
        .filter(Boolean)
        .join(" ");
    }
    if (!Array.isArray(actions)) return { frontmatter };
    return {
      frontmatter: {
        ...frontmatter,
        hero: {
          ...frontmatter.hero,
          actions: actions.map((action) => ({
            ...action,
            link:
              typeof action.link === "string" &&
              action.link.startsWith("/") &&
              !action.link.startsWith("//")
                ? `/${version}${action.link}`
                : action.link,
          })),
        },
      },
    };
  },

  versionsConfig: {
    current: currentVersion,
    sources: "src",
    archive: "archive",
    // The generated nav dropdown links to each version's home page, losing the
    // current page. The VersionSwitcher component keeps the page and only swaps
    // the version segment (see docs/.vitepress/theme/index.mjs).
    versionSwitcher: false,
  },

  themeConfig: {
    // The mark is an isometric cube: a thick foundation-model slab with a thin
    // custom-model layer on top. The same file works on both backgrounds.
    logo: { light: "/logo.svg", dark: "/logo.svg", alt: "fair-geo-fm" },
    // Read by StacNavLink; version -> public base of the generated STAC tree.
    stacBases,
    // Read by ZenodoNavLink; version -> its Zenodo record.
    zenodoRecords,
    nav: [
      { text: "Home", link: "/" },
      { text: "Guide", link: "/guide/" },
      { text: "Models", link: "/models/" },
      { component: "StacNavLink" },
      { component: "ZenodoNavLink" },
      { text: "Contributing", link: "/contributing" },
      // Switches version while keeping the current page; falls back to the
      // version home only when that page does not exist there.
      { component: "VersionSwitcher" },
    ],
    outline: [2, 3],
    search: { provider: "local" },
    editLink: { pattern: editLinkFor, text: "Edit this page on GitHub" },
    socialLinks: [
      { icon: "github", link: "https://github.com/danlooo/fair-geo-fm" },
    ],
    footer: {
      message:
        'Part of <a href="https://nfdi4earth.de" target="_blank" rel="noreferrer">NFDI4Earth</a>, funded by the <a href="https://gepris.dfg.de/gepris/projekt/460036893" target="_blank" rel="noreferrer">German Research Foundation</a>.',
    },
  },
});
