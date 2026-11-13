# TESSERA v2

<div style="display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; margin-top: 1rem;">
  <Badge text="computed on demand" type="info" style="font-size: inherit; 
  line-height: inherit;" />
  <Badge text="Sentinel 1+2" type="info" style="font-size: inherit; 
  line-height: inherit;" />
</div>

On-demand pixel embeddings based on Sentinel-1 and Sentinel-2 using Barlow Twins.

## Usage

::: tabs

== Web editor

1. Go to a openEO Web Editor page e.g. https://openeofed.dataspace.copernicus.eu
1. Click on Log in at the bottom of the page if required.
1. Click on the `</> Code` button
1. Replace the content with this code
1. Modify the workflow and run the job

```json
{
  "process_graph": {
    "tessera_v2_embeddings1": {
      "process_id": "tessera_v2_embeddings",
      "namespace": "https://danlooo.github.io/fair-geo-fm/main/models/tessera_v2/process_graph.json",
      "description": "Documentation: https://danlooo.github.io/fair-geo-fm/models/tessera_v2/",
      "arguments": {
        "spatial_extent": {
          "west": 11.55,
          "south": 50.88,
          "east": 11.65,
          "north": 50.96
        },
        "temporal_extent": ["2025-01-01", "2026-01-01"]
      }
    },
    "save1": {
      "process_id": "save_result",
      "arguments": {
        "data": { "from_node": "tessera_v2_embeddings1" },
        "format": "NETCDF"
      },
      "result": true
    }
  }
}
```

== Python

```python
import openeo

connection = openeo.connect("https://openeo.dataspace.copernicus.eu")
connection.authenticate_oidc()

embeddings = connection.datacube_from_process(
    "tessera_v2_embeddings",
    namespace="https://danlooo.github.io/fair-geo-fm/main/models/tessera_v2/process_graph.json",
    spatial_extent={"west": 11.55, "south": 50.88, "east": 11.65, "north": 50.96},
    temporal_extent=["2025-01-01", "2026-01-01"],
)

job = embeddings.create_job(out_format="NETCDF")
job.start_and_wait()
job.download_result("tessera_v2_embeddings.nc")
```

== R

```r
library(openeo)

connect(host = "https://openeo.dataspace.copernicus.eu")
login()

# The R client cannot attach a process namespace URL to a node yet
# (Open-EO/openeo-r-client#143), so fetch the definition and bind its
# parameters locally instead.
definition <- jsonlite::fromJSON(
  "https://danlooo.github.io/fair-geo-fm/main/models/tessera_v2/process_graph.json",
  simplifyVector = FALSE
)
definition$parameters <- NULL

graph <- parse_graph(
  definition,
  parameters = list(
    spatial_extent = list(west = 11.55, south = 50.88, east = 11.65, north = 50.96),
    temporal_extent = list("2025-01-01", "2026-01-01"),
    s2_collection_id = "SENTINEL2_L2A",
    s1_collection_id = "SENTINEL1_GRD",
    s2_bands = list("B04", "B02", "B03", "B08", "B8A", "B05", "B06", "B07", "B11", "B12", "SCL"),
    s1_bands = list("VV", "VH")
  )
)

compute_result(
  graph = graph,
  format = "NETCDF",
  output_file = "tessera_v2_embeddings.nc"
)
```

:::

## Input parameters

| Name               | Required | Default                                                                     | Description                                                           |
| ------------------ | -------- | --------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `spatial_extent`   | yes      | —                                                                           | Area to compute embeddings for (WGS84 bounding box).                  |
| `temporal_extent`  | yes      | —                                                                           | Period to compute embeddings for, e.g. one calendar year.             |
| `s2_collection_id` | no       | `SENTINEL2_L2A`                                                             | Sentinel-2 L2A collection id.                                         |
| `s1_collection_id` | no       | `SENTINEL1_GRD`                                                             | Sentinel-1 GRD collection id.                                         |
| `s2_bands`         | no       | `B04`, `B02`, `B03`, `B08`, `B8A`, `B05`, `B06`, `B07`, `B11`, `B12`, `SCL` | Sentinel-2 bands in TESSERA order: ten reflectance bands, then `SCL`. |
| `s1_bands`         | no       | `VV`, `VH`                                                                  | Sentinel-1 bands, renamed per orbit (`VV_ASC`, `VH_DESC`, …).         |

## Publication

Feng, Z. et al. (2026) ‘TESSERA v2: Scaling pixel-wise Earth
foundation models’, _arXiv_ [Preprint]. Available at:
<https://arxiv.org/abs/2607.03949> (Accessed: 12 November 2026).

**Abstract**

Pixel-wise Earth-observation (EO) foundation models are now achieving state-of-the-art performance via generated spatial embeddings. However, how these models scale and how best to spend a pretraining budget remain poorly understood. We present the largest controlled scaling study for EO to date: 395 training runs within a fixed pixel-wise Barlow Twins family, each evaluated on 15 diverse downstream tasks. We find that pretraining loss barely predicts downstream performance (|Pearson r| < 0.2), so selecting models by loss wastes a large share of the compute. We also find that, as the training budget grows, the encoder and the data should grow together while the projector stays fixed, which gives a simple rule for allocating compute. Using this rule, we train a family of pixel-wise teachers (0.5B, 1B, and 2B) and distil the largest into compact students for embeddings-as-data deployment. In aggregate, our 44-million-parameter distilled student outperforms every open and proprietary embedding product we test, several of them an order of magnitude larger. These students produce Matryoshka representations that are inexpensive to serve: a 16-dimensional prefix keeps 92% of the full 128-dimensional performance at 1/8 of the storage. Together, these results give a concrete, empirically grounded recipe for scaling pixel-wise EO foundation models: train large encoders, select by downstream performance, and distil into flexible student models. We plan to release global 10 m annual embeddings covering 2017-2025 as version 2 of the TESSERA foundation-model embeddings product. All code is available at: this https URL

## Links

- [Original notebook as an openEO community example](https://github.com/Open-EO/openeo-community-examples/blob/main/python/60_geospatial-embeddings/tessera/tessera-embedding.ipynb)
- [CDSE release note on the original notebook](https://dataspace.copernicus.eu/news/2026-9-25-new-openeo-community-example-generate-tessera-pixel-embeddings-sentinel-1-and)
