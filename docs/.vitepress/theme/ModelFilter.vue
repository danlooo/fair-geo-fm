<template>
  <div
    v-if="ready"
    class="model-filter"
    role="region"
    aria-label="Filter models"
  >
    <div class="model-filter-groups">
      <fieldset
        v-for="group in groups"
        :key="group.key"
        class="model-filter-group"
      >
        <legend>{{ group.label }}</legend>
        <button
          v-for="option in group.options"
          :key="option.value"
          type="button"
          class="model-filter-chip"
          :class="{ 'is-active': isActive(group.key, option.value) }"
          :aria-pressed="isActive(group.key, option.value)"
          @click="toggle(group.key, option.value)"
        >
          {{ option.label }}
          <span class="model-filter-chip-count">{{ option.count }}</span>
        </button>
      </fieldset>
    </div>
    <div class="model-filter-summary">
      <span aria-live="polite">
        Showing {{ visibleCount }} of {{ total }} models
      </span>
      <button
        v-if="hasSelection"
        type="button"
        class="model-filter-reset"
        @click="clear"
      >
        Clear filters
      </button>
    </div>
  </div>
</template>

<script setup>
// Client-side filter for the model catalogue. The cards are rendered as static
// HTML by models/index.md.j2; this component reads the data-* attributes that
// prepare-docs.mjs writes from each config.json, so the filter cannot drift
// from the values shown on the cards. Values within one group are combined
// with OR, groups with AND.
import { onMounted, ref } from "vue";

const props = defineProps({
  // Cards to filter. Keep in sync with the selector in models/index.md.j2.
  selector: { type: String, default: ".model-card-link[data-model]" },
});

// data-* attributes are read camelCase from element.dataset.
const FACETS = [
  { key: "spatialResolution", label: "Spatial resolution" },
  { key: "temporalResolution", label: "Temporal resolution" },
  { key: "type", label: "Output" },
  {
    key: "dimensions",
    label: "Embedding size",
    format: (value) => `${value}-dim`,
  },
  {
    key: "multimodal",
    label: "Input",
    format: (value) => (value === "true" ? "Multimodal" : "Single-source"),
  },
];

const groups = ref([]);
const selected = ref({});
const visibleCount = ref(0);
const total = ref(0);
const ready = ref(false);
const hasSelection = ref(false);

let cards = [];

// Numeric values sort numerically ("10 m" before "30 m", 128 before 768);
// everything else alphabetically.
function compareValues(a, b) {
  const numberA = Number.parseInt(a, 10);
  const numberB = Number.parseInt(b, 10);
  if (Number.isFinite(numberA) && Number.isFinite(numberB)) {
    return numberA - numberB;
  }
  return a.localeCompare(b);
}

function buildGroups() {
  groups.value = FACETS.map((facet) => {
    const counts = new Map();
    for (const card of cards) {
      const value = card.dataset[facet.key] || "";
      if (!value) continue;
      counts.set(value, (counts.get(value) || 0) + 1);
    }
    return {
      ...facet,
      options: [...counts.keys()].sort(compareValues).map((value) => ({
        value,
        label: facet.format ? facet.format(value) : value,
        count: counts.get(value),
      })),
    };
  })
    // A group with one value cannot narrow anything down.
    .filter((group) => group.options.length > 1);
}

function isActive(key, value) {
  return (selected.value[key] || []).includes(value);
}

function toggle(key, value) {
  const next = new Set(selected.value[key] || []);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  const updated = { ...selected.value };
  if (next.size) updated[key] = [...next];
  else delete updated[key];
  selected.value = updated;
  apply();
}

function clear() {
  selected.value = {};
  apply();
}

function apply() {
  let shown = 0;
  for (const card of cards) {
    const visible = FACETS.every(({ key }) => {
      const chosen = selected.value[key];
      if (!chosen || !chosen.length) return true;
      return chosen.includes(card.dataset[key] || "");
    });
    // .model-card-link sets display: block, which outranks the UA [hidden]
    // rule; custom.css restores display: none for [hidden] here.
    card.hidden = !visible;
    if (visible) shown += 1;
  }
  visibleCount.value = shown;
  hasSelection.value = Object.keys(selected.value).length > 0;
}

onMounted(() => {
  cards = [...document.querySelectorAll(props.selector)];
  total.value = cards.length;
  if (!total.value) return;
  buildGroups();
  apply();
  ready.value = true;
});
</script>
