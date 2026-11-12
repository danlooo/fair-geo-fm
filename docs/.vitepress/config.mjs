import { defineVersionedConfig } from "@viteplus/versions";

// The default branch is the current version on the site. Every other branch
// and tag that contains a site is copied to docs/archive/<ref>/ by
// scripts/prepare-docs.mjs, so one build serves all versions and the version
// switcher links between them.
const currentVersion = "main";

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
      : rest.replace(/^processes\/([^/]+)\/index\.md$/, "processes/$1/README.md");
  const action = version === "main" ? "edit/main" : `blob/${version}`;
  return `https://github.com/danlooo/fair-geo-fm/${action}/${source}`;
}

export default defineVersionedConfig({
  base: "/fair-geo-fm/",
  // Direct .html URLs keep the version switcher working on a full page load;
  // clean URLs would make it link to /<version>/guide/index.
  cleanUrls: false,
  title: "FAIR Geospatial Foundation Models",
  description: "Geospatial foundation models ready to use in openEO.",
  sitemap: { hostname: "https://danlooo.github.io" },

  // The plugin rewrites links in markdown, but not URLs in frontmatter such as
  // the home page hero buttons.
  transformPageData(pageData) {
    const actions = pageData.frontmatter?.hero?.actions;
    if (!Array.isArray(actions)) return;
    const version = pageData.relativePath.split("/")[0];
    return {
      frontmatter: {
        ...pageData.frontmatter,
        hero: {
          ...pageData.frontmatter.hero,
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
      { text: "Processes", link: "/processes/" },
      { text: "Contributing", link: "/contributing" },
    ],

    sidebar: {
      "/guide/": [
        {
          text: "Guide",
          items: [{ text: "Getting started", link: "/guide/" }],
        },
      ],
      "/processes/": [
        {
          text: "Processes",
          items: [
            { text: "Overview", link: "/processes/" },
            {
              text: "TESSERA v2 embeddings",
              link: "/processes/tessera_v2/",
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
      message: "Part of NFDI4Earth, funded by DFG project 460036893.",
    },
  },
});
