---
layout: home

hero:
  name: fair-geo-fm
  text: FAIR geospatial foundation models
  tagline: Ready to use in openEO
  image:
    src: /logo.svg
    alt: fair-geo-fm logo
  actions:
    - theme: brand
      text: Get started
      link: /guide/
    - theme: alt
      text: Browse models
      link: /models/

features:
  - title: Frozen embeddings
    details: Use pre-calculated embeddings without running a model.
  - title: Model weights
    details: Run foundation models on demand inside an openEO batch job.
  - title: openEO native
    details: Every model is a single openEO user-defined process, optimized to run at the Copernicus Data Space Ecosystem
  - title: FAIR principles
    details: We ensure that all models are findable, accessible, interoperable, and reusable.
---

<div class="feature-card">
  <img src="/alphaearth-false-color.png" style="height:20rem;" alt="AlphaEarth false-color satellite composite" />
  <div>
    <h2>Geospatial foundation models</h2>
    <p>
      Foundation models are large neural networks pertained on broad data. They learn from satellite imagery, radar, elevation and climate data. Their main output is an embedding: a short list of numbers per pixel or area (e.g. 64 or 128 values) summarizing vegetation, water, built-up land or seasonality — far smaller than the source imagery. Training is already done, so embeddings can be reused for land cover, crops, habitats, change detection or disaster response, and are most valuable where labelled data are scarce.
    </p>
  </div>
</div>

<div class="feature-card">
  <div>
    <h2>Ready to use in openEO</h2>
    <p>
      Each foundation model is shipped as a single openEO process node that can be integrated in your workflows. The underlying process graph and required runtime environments are uploaded to zenodo for reproducible execution. Additionally, models can be also explored using the provided STAC catalog.
    </p>
  </div>
  <img src="/openeo-graph.png" alt="openEO process graph for tessera embeddings" />
</div>
