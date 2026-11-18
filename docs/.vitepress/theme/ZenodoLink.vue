<script setup>
// Inline link to the Zenodo record of the version being viewed, for use in
// page text. Released tags open their own record; main and unresolved refs open
// the concept DOI, which resolves to the latest release, mirroring the navbar
// link in ZenodoNavLink.
import { computed } from "vue";
import { useData } from "vitepress";

defineProps({
  // Text of the link.
  label: { type: String, default: "Zenodo" },
});

const { page, theme } = useData();

const version = computed(() => page.value.relativePath.split("/")[0]);
const href = computed(() => {
  const records = theme.value.zenodoRecords || {};
  const fallback = "https://doi.org/10.5281/zenodo.24275707";
  return records[version.value] || records.main || fallback;
});
</script>

<template>
  <a :href="href">{{ label }}</a>
</template>
