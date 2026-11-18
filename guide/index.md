# Guide

This site collects geospatial foundation models and their embedding workflows in
a form that is ready to use from [openEO](https://openeo.org) at the
[Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu) (CDSE).

## Get started

Get precomputed, frozen [TESSERA v1 embeddings](/models/tessera_v1/) for the
region around Jena, Germany in 2025, sampled onto a 10 m UTM grid:

::: tabs

== Web editor

1. Go to an openEO Web Editor page e.g. https://openeofed.dataspace.copernicus.eu
1. Click on Log in at the bottom of the page if required.
1. Click on the `</> Code` button
1. Replace the content with this code
1. Modify the workflow and run the job

```json
{
  "process_graph": {
    "tessera_v1_embeddings1": {
      "process_id": "tessera_v1_embeddings",
      "namespace": "https://danlooo.github.io/fair-geo-fm/main/models/tessera_v1/process_graph.json",
      "description": "Documentation: https://danlooo.github.io/fair-geo-fm/main/models/tessera_v1/",
      "arguments": {
        "spatial_extent": {
          "west": 11.55,
          "south": 50.88,
          "east": 11.65,
          "north": 50.96
        },
        "temporal_extent": ["2025-01-01", "2026-01-01"],
        "projection": 32632
      }
    },
    "save1": {
      "process_id": "save_result",
      "arguments": {
        "data": { "from_node": "tessera_v1_embeddings1" },
        "format": "NETCDF"
      },
      "result": true
    }
  }
}
```

== Python

Install the [openEO Python client](https://openeo.org/documentation/1.0/python/) first:

```sh
pip install openeo
```

Calculate the embeddings:

```python
import openeo

connection = openeo.connect("https://openeo.dataspace.copernicus.eu")
connection.authenticate_oidc()

embeddings = connection.datacube_from_process(
    "tessera_v1_embeddings",
    namespace=(
        "https://danlooo.github.io/fair-geo-fm/main/"
        "models/tessera_v1/process_graph.json"
    ),
    spatial_extent={
        "west": 11.55, "south": 50.88, "east": 11.65, "north": 50.96
    },
    temporal_extent=["2025-01-01", "2026-01-01"],
    # UTM zone 32N; output pixels are 10 m in this CRS.
    projection=32632,
)

job = embeddings.create_job(out_format="NETCDF")
job.start_and_wait()
job.download_result("tessera_v1_embeddings.nc")
```

== R

Install the [openEO R client](https://github.com/Open-EO/openeo-r-client) first:

```r
install.packages("openeo")
```

Calculate the embeddings:

```r
library(openeo)

connect(host = "https://openeo.dataspace.copernicus.eu")
login()

# The R client cannot attach a process namespace URL to a node yet
# (Open-EO/openeo-r-client#143), so fetch the definition and bind its
# parameters locally instead. Start from the defaults the process declares,
# then override the required parameters.
definition <- jsonlite::fromJSON(
  paste0(
    "https://danlooo.github.io/fair-geo-fm/main/",
    "models/tessera_v1/process_graph.json"
  ),
  simplifyVector = FALSE
)
parameters <- lapply(definition$parameters, `[[`, "default")
names(parameters) <- vapply(definition$parameters, `[[`, "", "name")
parameters$spatial_extent <- list(
  west = 11.55, south = 50.88, east = 11.65, north = 50.96
)
parameters$temporal_extent <- list(
  "2025-01-01", "2026-01-01"
)
# UTM zone 32N; output pixels are 10 m in this CRS.
parameters$projection <- 32632L
definition$parameters <- NULL

graph <- parse_graph(definition, parameters = parameters)

compute_result(
  graph = graph,
  format = "NETCDF",
  output_file = "tessera_v1_embeddings.nc"
)
```

:::

## What are geospatial foundation models?

Foundation models are large neural networks pretrained on broad data. Geospatial
ones learn from satellite imagery, radar, elevation and climate data.
Their main output is an embedding: a short list of numbers per pixel or area
(e.g. 64 or 128 values) summarising vegetation, water, built-up land or
seasonality — far smaller than the source imagery.
Training is already done, so embeddings can be reused for land cover, crops,
habitats, change detection or disaster response, and are most valuable where
labelled data are scarce.
Models here are either frozen embeddings to download or model weights run
on demand; weights are cheaper to produce, frozen embeddings cheaper to use.
Both are usable as openEO processes from the Web Editor, Python or R.
Please note that embeddings are learned representations, not ground truth; validate results
before decisions.

## FAIR principles

Models are described by [STAC](https://stacspec.org) Items so they can be
discovered and processed without reading this site.
Each release is archived on [Zenodo](https://doi.org/10.5281/zenodo.24275707)
under a DOI, which keeps it citable and independent of this website.

## Learn more

- [Browse models](/models/) for the available models and their parameters.
- <StacCatalogLink label="STAC catalog" /> · <StacCatalogLink label="open in STAC Browser" browser /> for machine-readable metadata.
- <ZenodoLink label="Zenodo release" /> for the archived version and its DOI.
- [Contributing](/contributing) for model, runtime, and release conventions.
- [NFDI4Earth](https://nfdi4earth.de) for the project context.
