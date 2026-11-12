import { defineVersionedConfig } from "@viteplus/versions";

// The site's current version is the `stable` branch, which CI keeps pointing
// at the newest v* tag (see .github/workflows/build.yml). Every other branch
// and tag that contains a site is copied to docs/archive/<ref>/ by
// scripts/prepare-docs.mjs, so one build serves all versions and the version
// switcher links between them. `latest` defaults to the current version, so
// the site root opens the stable docs.
const currentVersion = process.env.DOCS_CURRENT_VERSION || "stable";

// Map a versioned page back to the canonical file in the repository.
// `relativePath` carries the version prefix, `filePath` the generated folder
// (docs/src or docs/archive/<ref>). The function is serialized into the client
// bundle, so it must not reference anything outside its own scope.
function editLinkFor(page) {
  const version = page.relativePath.split("/")[0];
  const parts = page.filePath.split("/");
  const rest = (parts[0] === "src" ? parts.slice(1) : parts.slice(2)).join("/");
  const source =
    rest === "contributing.md"
      ? "CONTRIBUTING.md"
      : rest.replace(/^models\/([^/]+)\/index\.md$/, "models/$1/README.md");
  // docs/src is the stable release branch, which mirrors a v* tag, so edits are
  // proposed against the default branch. Archived pages have no edit link.
  const action = version === "stable" ? "edit/main" : `blob/${version}`;
  return `https://github.com/danlooo/fair-geo-fm/${action}/${source}`;
}

export default defineVersionedConfig({
  base: "/fair-geo-fm/",
  // Direct .html URLs keep the version switcher working on a full page load;
  // clean URLs would make it link to /<version>/guide/index.
  cleanUrls: false,
  title: "fair-geo-fm",
  description: "Geospatial foundation models ready to use in openEO.",
  sitemap: { hostname: "https://danlooo.github.io" },

  // The plugin rewrites links in markdown, but not URLs in frontmatter such as
  // the home page hero buttons.
  transformPageData(pageData) {
    const version = pageData.relativePath.split("/")[0];
    const actions = pageData.frontmatter?.hero?.actions;
    const frontmatter = { ...pageData.frontmatter };
    // Archived pages are snapshots whose paths may predate renames, so an edit
    // link would point at a file that does not exist at that ref.
    if (version !== currentVersion) frontmatter.editLink = false;
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
    versionSwitcher: { text: "Version", includeCurrentVersion: true },
  },

  themeConfig: {
    nav: [
      { text: "Home", link: "/" },
      { text: "Guide", link: "/guide/" },
      { text: "Models", link: "/models/" },
      { text: "Contributing", link: "/contributing" },
    ],

    sidebar: {
      "/guide/": [
        {
          text: "Guide",
          items: [{ text: "Getting started", link: "/guide/" }],
        },
      ],
      "/models/": [
        {
          text: "Models",
          items: [
            { text: "Overview", link: "/models/" },
            {
              text: "TESSERA v2 embeddings",
              link: "/models/tessera_v2/",
            },
          ],
        },
      ],
    },

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
