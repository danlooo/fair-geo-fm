<script setup>
// Navbar link to the generated STAC catalog for the version being viewed.
// Released tags open their catalog in the Zenodo record; main and unresolved
// refs use the staging tree. stac-bases.json is written by prepare-docs.mjs;
// a missing entry falls back to main.
import { computed, inject } from "vue";
import { useData } from "vitepress";

defineProps({
  // Set by VitePress when the item is rendered in the mobile screen menu.
  screenMenu: { type: Boolean, default: false },
});

const { page, theme } = useData();
const closeScreen = inject("close-screen", () => {});

const version = computed(() => page.value.relativePath.split("/")[0]);
const base = computed(() => {
  const bases = theme.value.stacBases || {};
  const fallback = "https://danlooo.github.io/fair-geo-fm/main";
  return (bases[version.value] || bases.main || fallback).replace(/\/+$/, "");
});
const href = computed(() => {
  const target = `${base.value}/stac/catalog.json`;
  return `https://browser.moregeo.it/external/${target.replace(/^https?:\/\//, "")}`;
});
</script>

<template>
  <a
    class="stac-nav-link"
    :class="{ screen: screenMenu }"
    :href="href"
    tabindex="0"
    @click="closeScreen"
  >
    STAC
  </a>
</template>

<style scoped>
.stac-nav-link {
  display: flex;
  align-items: center;
  padding: 0 12px;
  line-height: var(--vp-nav-height);
  font-size: 14px;
  font-weight: 500;
  color: var(--vp-c-text-1);
  transition: color 0.25s;
}

.stac-nav-link:hover {
  color: var(--vp-c-brand-1);
}

.stac-nav-link.screen {
  display: block;
  border-bottom: 1px solid var(--vp-c-divider);
  padding: 12px 0 11px;
  line-height: 24px;
}
</style>
