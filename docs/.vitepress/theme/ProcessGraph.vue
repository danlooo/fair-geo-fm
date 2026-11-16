<template>
  <div class="process-graph">
    <openeo-model-builder
      ref="builder"
      :id="id"
      :height="height"
      explicit-zoom
    />
    <p v-if="error" class="process-graph-error">
      The process graph could not be loaded: {{ error }}
    </p>
  </div>
</template>

<script setup>
import { onMounted, ref } from "vue";

// Renders an openEO process graph with the ModelBuilder web component from
// @openeo/vue-components. That package is Vue 2 and cannot be imported as an
// SFC into this Vue 3 site, so the prebuilt web component bundle is used. Its
// assets are copied to docs/public/vendor/openeo by scripts/prepare-docs.mjs.
const props = defineProps({
  // Document-wide unique id, required by the web component.
  id: { type: String, required: true },
  // Graph file relative to the page; prepare-docs serves process_graph.json
  // next to each model page.
  src: { type: String, default: "process_graph.json" },
  height: { type: String, default: "600px" },
});

const builder = ref(null);
const error = ref("");

const fontAwesomeStylesheet = `${import.meta.env.BASE_URL}vendor/fontawesome/css/all.min.css`;

// ModelBuilder appends the Font Awesome CDN stylesheet to its shadow root. Use
// the copy served by this site instead, both to avoid a third-party request and
// because the icons do not render when that CDN is unreachable.
function useLocalFontAwesome(element) {
  const root = element.shadowRoot;
  if (!root) return;
  let found = false;
  for (const link of root.querySelectorAll('link[rel="stylesheet"]')) {
    if (link.href.includes("use.fontawesome.com")) {
      link.remove();
    } else if (link.href.includes("fontawesome")) {
      found = true;
    }
  }
  if (!found) {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = fontAwesomeStylesheet;
    root.appendChild(link);
  }
}

let bundlePromise = null;

function loadBundle() {
  if (bundlePromise) return bundlePromise;
  bundlePromise = new Promise((resolve, reject) => {
    if (window.customElements.get("openeo-model-builder")) return resolve();
    const script = document.createElement("script");
    script.src = `${import.meta.env.BASE_URL}vendor/openeo/openeo.min.js`;
    script.addEventListener("load", resolve, { once: true });
    script.addEventListener(
      "error",
      () => reject(new Error(`could not load ${script.src}`)),
      { once: true },
    );
    document.head.appendChild(script);
  });
  return bundlePromise;
}

// Resolve the graph next to the page rather than against the document base:
// the site is served under a base path and several version folders.
function graphUrl(src) {
  const page = new URL(window.location.href);
  if (!page.pathname.endsWith("/")) {
    page.pathname = page.pathname.replace(/[^/]*$/, "");
  }
  return new URL(src, page).href;
}

function waitFor(check, timeout = 15000) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      if (check()) return resolve();
      if (Date.now() - start > timeout) {
        return reject(new Error("timed out waiting for the model builder"));
      }
      requestAnimationFrame(tick);
    };
    tick();
  });
}

onMounted(async () => {
  try {
    const response = await fetch(graphUrl(props.src));
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}`);
    }
    const graph = await response.json();

    await loadBundle();
    await customElements.whenDefined("openeo-model-builder");

    const element = builder.value;
    // Before mounting, so ModelBuilder finds a Font Awesome stylesheet already
    // in the shadow root; again afterwards, in case it added the CDN one first.
    useLocalFontAwesome(element);
    // ModelBuilder ignores value changes until its first mount is complete,
    // and it needs the process graph as its `value` property (the attribute
    // form only accepts scalars).
    await waitFor(() => element.vueComponent?.allMounted);
    useLocalFontAwesome(element);
    element.value = graph;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
    console.error("process graph:", e);
  }
});
</script>

<style scoped>
.process-graph {
  /* The graph is drawn for a light background; keep it readable in dark mode. */
  background-color: #fff;
  border: 1px solid var(--vp-c-divider);
  border-radius: 8px;
  margin: 1.5rem 0;
  overflow: hidden;
}

.process-graph :deep(openeo-model-builder) {
  display: block;
  width: 100%;
}

.process-graph-error {
  margin: 0;
  padding: 0.75rem 1rem;
  color: var(--vp-c-danger-1);
}
</style>
