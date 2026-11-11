import { defineConfig } from "vitepress";

export default defineConfig({
  base: "/fair-geo-fm/",
  cleanUrls: true,
  title: "FAIR Geospatial Foundation Models",
  description: "Geospatial foundation models ready to use in openEO.",
  sitemap: { hostname: "https://danlooo.github.io" },
  // README.md stays the GitHub landing page and is not part of the site;
  // index.md is the website home.
  srcExclude: ["README.md", "runtimes/**", "scripts/**", ".github/**"],

  // Process READMEs remain the single source of truth for their pages.
  rewrites: (id) => {
    if (id.startsWith("processes/") && id.endsWith("/README.md")) {
      return id.replace(/README\.md$/, "index.md");
    }
    if (id === "CONTRIBUTING.md") {
      return "contributing.md";
    }
    return id;
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
    editLink: {
      pattern: "https://github.com/danlooo/fair-geo-fm/edit/main/:path",
      text: "Edit this page on GitHub",
    },
    socialLinks: [
      { icon: "github", link: "https://github.com/danlooo/fair-geo-fm" },
    ],
    footer: {
      message: "Part of NFDI4Earth, funded by DFG project 460036893.",
    },
  },

});
