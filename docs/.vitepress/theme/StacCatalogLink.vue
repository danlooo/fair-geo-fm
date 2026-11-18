<script setup>
// Inline link to the STAC catalog of the version being viewed.
//
// Pages copied verbatim into every version (the home page has no template
// variables) cannot link a version-specific catalog URL, so this component
// resolves it from stacBases at render time, mirroring StacNavLink: released
// tags open their Zenodo record's catalog, main and unresolved refs the
// staging tree.
import { computed } from "vue";
import { useData } from "vitepress";

const props = defineProps({
  // Text of the link.
  label: { type: String, default: "catalog" },
  // Wrap the catalog URL in the STAC Browser viewer instead of linking JSON.
  browser: { type: Boolean, default: false },
});

const { page, theme } = useData();

const version = computed(() => page.value.relativePath.split("/")[0]);
const target = computed(() => {
  const bases = theme.value.stacBases || {};
  const fallback =
    "https://danlooo.github.io/fair-geo-fm/main/stac/catalog.json";
  return bases[version.value] || bases.main || fallback;
});
const href = computed(() =>
  props.browser
    ? `https://browser.moregeo.it/external/${target.value.replace(/^https?:\/\//, "")}`
    : target.value,
);
</script>

<template>
  <a :href="href">{{ label }}</a>
</template>
