import { h } from "vue";
import DefaultTheme from "vitepress/theme";
import { enhanceAppWithTabs } from "vitepress-plugin-tabs/client";
import "@fontsource-variable/open-sans";
import "@fontsource-variable/open-sans/wght-italic.css";
import "./custom.css";
import PreviewBanner from "./PreviewBanner.vue";

// Also installs the components for the `::: tabs` blocks enabled in config.mjs,
// so a page can show e.g. the web-editor and Python variants of one example.
export default {
  extends: DefaultTheme,
  // doc-before renders above the page content, immediately above the h1.
  Layout: () =>
    h(DefaultTheme.Layout, null, {
      "doc-before": () => h(PreviewBanner),
    }),
  enhanceApp({ app }) {
    app.use(enhanceAppWithTabs);
  },
};
