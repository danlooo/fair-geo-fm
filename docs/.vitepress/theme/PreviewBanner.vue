<script setup>
// Shown above the h1 on every page of an unreleased version (the default
// branch and other branches), linking to the same page in the newest release.
// The home page uses VitePress's home layout, which does not render the doc
// slots, so the banner never appears there.
import { computed } from "vue";
import { useData } from "vitepress";

const { page, site, theme } = useData();

const versioning = computed(() => theme.value.versioning || {});
// A version folder is a release when it is a v* tag, e.g. v0.1.2.
const currentVersion = computed(() => page.value.relativePath.split("/")[0]);
const isRelease = computed(() => /^v\d+(?:\.\d+)*$/.test(currentVersion.value));
const latestVersion = computed(() => versioning.value.latestVersion || "");

// Directory pages keep their trailing slash; other pages are .html files
// because the site does not use clean URLs.
function releaseRoute(rest) {
  if (rest === "index.md") return "";
  if (rest.endsWith("/index.md")) return rest.slice(0, -"index.md".length);
  return rest.replace(/\.md$/, ".html");
}

const target = computed(() => {
  if (!latestVersion.value || isRelease.value) return "";
  const rest = page.value.relativePath.slice(currentVersion.value.length + 1);
  // A page that only exists on the branch has no released counterpart; send
  // the reader to the release's home page instead of a 404.
  const routes = versioning.value.routes?.[latestVersion.value] || [];
  const released = routes.includes(`${latestVersion.value}/${rest}`);
  return `${site.value.base}${latestVersion.value}/${releaseRoute(
    released ? rest : "index.md",
  )}`;
});
</script>

<template>
  <div v-if="target" class="preview-banner">
    This is a preview version. See the
    <a :href="target">latest stable release</a> for production workflows.
  </div>
</template>
