<script setup>
// Navbar link to the Zenodo record of the version being viewed. A released tag
// opens its own record; main and unresolved refs open the concept DOI, which
// resolves to the latest release. zenodo-records.json is written by
// prepare-docs.mjs; a missing entry falls back to the concept DOI.
import { computed, inject } from "vue";
import { useData } from "vitepress";

defineProps({
  // Set by VitePress when the item is rendered in the mobile screen menu.
  screenMenu: { type: Boolean, default: false },
});

const { page, theme } = useData();
const closeScreen = inject("close-screen", () => {});

const version = computed(() => page.value.relativePath.split("/")[0]);
const href = computed(() => {
  const records = theme.value.zenodoRecords || {};
  const fallback = "https://doi.org/10.5281/zenodo.24275707";
  return records[version.value] || records.main || fallback;
});
</script>

<template>
  <a
    class="zenodo-nav-link"
    :class="{ screen: screenMenu }"
    :href="href"
    tabindex="0"
    @click="closeScreen"
  >
    Zenodo
  </a>
</template>

<style scoped>
.zenodo-nav-link {
  display: flex;
  align-items: center;
  padding: 0 12px;
  line-height: var(--vp-nav-height);
  font-size: 14px;
  font-weight: 500;
  color: var(--vp-c-text-1);
  transition: color 0.25s;
}

.zenodo-nav-link:hover {
  color: var(--vp-c-brand-1);
}

.zenodo-nav-link.screen {
  display: block;
  border-bottom: 1px solid var(--vp-c-divider);
  padding: 12px 0 11px;
  line-height: 24px;
}
</style>
