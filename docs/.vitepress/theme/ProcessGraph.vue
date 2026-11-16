<template>
  <div class="process-graph">
    <div class="process-graph-toolbar">
      <VPButton
        tag="button"
        theme="alt"
        text="process_graph.json"
        :disabled="!graphHref"
        @click="openJson"
      />
      <VPButton
        ref="expandButton"
        tag="button"
        text="Open larger view"
        :disabled="!ready"
        @click="openPopup"
      />
    </div>

    <div
      v-if="expanded"
      class="process-graph-backdrop"
      @click="closePopup"
    />
    <div
      class="process-graph-canvas"
      :class="{ 'process-graph-canvas-expanded': expanded }"
      :role="expanded ? 'dialog' : undefined"
      :aria-modal="expanded ? 'true' : undefined"
      :aria-label="expanded ? 'Process graph, enlarged' : undefined"
    >
      <button
        v-if="expanded"
        ref="closeButton"
        type="button"
        class="process-graph-close"
        aria-label="Close enlarged process graph"
        @click.stop="closePopup"
      >
        ×
      </button>
      <openeo-model-builder
        ref="builder"
        :id="id"
        :height="expanded ? expandedHeight : height"
        explicit-zoom
      />
      <p v-if="error" class="process-graph-error">
        The process graph could not be loaded: {{ error }}
      </p>
    </div>
  </div>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { VPButton } from "vitepress/theme";

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
const expandButton = ref(null);
const closeButton = ref(null);
const error = ref("");
const ready = ref(false);
const expanded = ref(false);
const graphHref = ref("");

// The enlarged canvas is inset by 2vh top and bottom; the extra pixels account
// for its border, so the inner ModelBuilder does not overflow the rounded box.
const expandedHeight = "calc(96vh - 6px)";

let graphData = null;
let previousOverflow = "";

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

async function mountGraph(element) {
  if (!element || !graphData) return;
  // Before mounting, so ModelBuilder finds a Font Awesome stylesheet already
  // in the shadow root; again afterwards, in case it added the CDN one first.
  useLocalFontAwesome(element);
  // ModelBuilder ignores value changes until its first mount is complete,
  // and it needs the process graph as its `value` property (the attribute
  // form only accepts scalars).
  await waitFor(() => element.vueComponent?.allMounted);
  // The page can be left while the component is still loading.
  if (!element.isConnected) return;
  useLocalFontAwesome(element);
  element.value = graphData;
}

function onKeydown(event) {
  if (event.key === "Escape") closePopup();
}

// Refit after a resize. Wait for the next frame: the height change goes through
// the Vue 2 instance inside the web component, which has its own scheduler.
function refitGraph() {
  requestAnimationFrame(() => {
    builder.value?.vueComponent?.perfectScale?.();
  });
}

function openJson() {
  if (graphHref.value) {
    window.open(graphHref.value, "_blank", "noopener");
  }
}

// Enlarge the existing ModelBuilder instead of mounting a second instance: it
// already holds the graph and its view state, and re-initializing a web
// component inside a popup is fragile.
async function openPopup() {
  if (!ready.value || expanded.value) return;
  expanded.value = true;
  previousOverflow = document.documentElement.style.overflow;
  document.documentElement.style.overflow = "hidden";
  document.addEventListener("keydown", onKeydown);
  await nextTick();
  closeButton.value?.focus();
  refitGraph();
}

async function closePopup() {
  if (!expanded.value) return;
  expanded.value = false;
  document.documentElement.style.overflow = previousOverflow;
  document.removeEventListener("keydown", onKeydown);
  await nextTick();
  refitGraph();
  nextTick(() => expandButton.value?.$el?.focus());
}

onMounted(async () => {
  try {
    graphHref.value = graphUrl(props.src);
    const response = await fetch(graphHref.value);
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}`);
    }
    graphData = await response.json();

    await loadBundle();
    await customElements.whenDefined("openeo-model-builder");
    await mountGraph(builder.value);
    ready.value = true;
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e);
    console.error("process graph:", e);
  }
});

onBeforeUnmount(() => {
  document.removeEventListener("keydown", onKeydown);
  if (expanded.value) {
    document.documentElement.style.overflow = previousOverflow;
  }
});
</script>

<style scoped>
.process-graph {
  margin: 1.5rem 0;
}

.process-graph-toolbar {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-bottom: 0.5rem;
}

.process-graph-toolbar :deep(.VPButton:disabled) {
  opacity: 0.5;
  cursor: default;
}

.process-graph-canvas {
  /* The graph is drawn for a light background; keep it readable in dark mode. */
  background-color: #fff;
  border: 1px solid var(--vp-c-divider);
  border-radius: 8px;
  overflow: hidden;
}

.process-graph-canvas :deep(openeo-model-builder) {
  display: block;
  width: 100%;
}

.process-graph-error {
  margin: 0;
  padding: 0.75rem 1rem;
  color: var(--vp-c-danger-1);
}

.process-graph-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background-color: rgba(0, 0, 0, 0.55);
}

.process-graph-canvas-expanded {
  position: fixed;
  inset: 2vh 2vw;
  z-index: 1001;
  border-radius: 10px;
  box-shadow: var(--vp-shadow-4);
}

.process-graph-close {
  position: absolute;
  top: 0.5rem;
  left: 0.5rem;
  /* Above the ModelBuilder's absolutely positioned canvas and blocks layers
     (z-index 1 and 3, which otherwise swallow the click). */
  z-index: 10;
  width: 2rem;
  height: 2rem;
  padding: 0;
  border: 1px solid var(--vp-c-divider);
  border-radius: 50%;
  background-color: var(--vp-c-bg-soft);
  color: var(--vp-c-text-1);
  font-size: 1.4rem;
  line-height: 1;
  cursor: pointer;
}

.process-graph-close:hover {
  border-color: var(--vp-c-brand-1);
  color: var(--vp-c-brand-1);
}
</style>
