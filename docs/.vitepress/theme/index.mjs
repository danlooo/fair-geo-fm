import { h } from "vue";
import DefaultTheme from "vitepress/theme";
import VersionSwitcher from "@viteplus/versions/components/version-switcher.component.vue";
import { installVersionFallback } from "@viteplus/versions/fallback";
import { enhanceAppWithTabs } from "vitepress-plugin-tabs/client";
import "@fontsource-variable/open-sans";
import "@fontsource-variable/open-sans/wght-italic.css";
import "@fortawesome/fontawesome-free/css/all.min.css";
import "./custom.css";
import StacCatalogLink from "./StacCatalogLink.vue";
import StacNavLink from "./StacNavLink.vue";
import VersionBanner from "./VersionBanner.vue";
import ZenodoLink from "./ZenodoLink.vue";
import ZenodoNavLink from "./ZenodoNavLink.vue";
import ProcessGraph from "./ProcessGraph.vue";

// Also installs the components for the `::: tabs` blocks enabled in config.mjs,
// so a page can show e.g. the web-editor and Python variants of one example.
export default {
  extends: DefaultTheme,
  // doc-before renders above the page content, immediately above the h1.
  Layout: () =>
    h(DefaultTheme.Layout, null, {
      "doc-before": () => h(VersionBanner),
    }),
  enhanceApp({ app, router, siteData }) {
    app.use(enhanceAppWithTabs);
    app.component("StacCatalogLink", StacCatalogLink);
    app.component("StacNavLink", StacNavLink);
    app.component("ZenodoLink", ZenodoLink);
    app.component("ZenodoNavLink", ZenodoNavLink);
    app.component("VersionSwitcher", VersionSwitcher);
    app.component("ProcessGraph", ProcessGraph);
    installVersionFallback(router, siteData);
  },
};
