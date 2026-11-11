# TESSERA v2 embeddings

On-demand TESSERA v2 pixel embeddings for any AOI and period, computed inside
openEO from Sentinel-1 GRD (ascending and descending) and Sentinel-2 L2A.

Process id: `tessera_v2_embeddings`

This is **not** the published geotessera tiles: it runs the TESSERA v2 student
encoder inside a PyTorch UDF, so coverage is any AOI/year, but results depend on
available scenes and cloud cover, and it is a heavy batch job. Use the generic
`load_embeddings` process for archived tiles.

## Usage

`process_graph.json` is a user-defined process served remotely through the
[remote process definition extension](https://api.openeo.org/extensions/remote-process-definition/0.1.0).
Reference it by URL as the `namespace`; the node id follows the web editor's
`<process_id>1` convention:

```json
{
  "process_graph": {
    "tessera_v2_embeddings1": {
      "process_id": "tessera_v2_embeddings",
      "namespace": "https://raw.githubusercontent.com/danlooo/fair-geo-fm/main/processes/tessera_v2/process_graph.json",
      "description": "Documentation: https://danlooo.github.io/fair-geo-fm/processes/tessera_v2",
      "arguments": {
        "spatial_extent": {
          "west": 11.55,
          "south": 50.88,
          "east": 11.65,
          "north": 50.96
        },
        "temporal_extent": ["2023-08-01", "2023-09-30"]
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

Paste this into the `</> Code` tab of the openEO Web editor, then add downstream
processes (e.g. classification) as needed. `example.py` submits the same call
from Python.

## Parameters

| Name               | Required | Description                                                                                                                  |
| ------------------ | -------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `spatial_extent`   | yes      | AOI as a WGS84 bounding box (`west`, `south`, `east`, `north`).                                                              |
| `temporal_extent`  | yes      | Period to compute embeddings for, e.g. one calendar year.                                                                    |
| `s2_collection_id` | no       | Sentinel-2 L2A collection id. Defaults to `SENTINEL2_L2A`.                                                                   |
| `s1_collection_id` | no       | Sentinel-1 GRD collection id. Defaults to `SENTINEL1_GRD`.                                                                   |
| `s2_bands`         | no       | Sentinel-2 bands in TESSERA order: 10 reflectance bands followed by SCL. Renamed positionally to TESSERA's canonical labels. |
| `s1_bands`         | no       | Sentinel-1 bands in TESSERA order (`VV`, `VH`). Renamed positionally to `VV_ASC`/`VH_ASC` and `VV_DESC`/`VH_DESC` per orbit. |

Returns a raster cube with one 128-band embedding vector per pixel.

The inputs are consumed raw: do not composite, resample or rescale them before
this process. The UDF does SCL-based cloud masking and source-date pruning
internally.

## Runtime

`run_udf` is pinned to Python 3.11. PyTorch comes from the
`udf-dependency-archives` job option in `default_job_options`, which points at
the archive built from [`runtimes/py311-torch231`](../../runtimes/py311-torch231)
and published by the `publish runtime` workflow. The full URL lives in
`runtime.txt` and must appear verbatim in `default_job_options`:

```
https://github.com/danlooo/fair-geo-fm/releases/download/staging/py311-torch231.zip#feature_deps
```

This is the **mutable `staging` prerelease** (sha256
`84c33b6a11c407ad61f6cb48e937dd843daf17df105eb74b3b7e626658009932`, replaced on
every publish), used while the backend fetch is being verified. Once it works,
switch to the content-addressed release so the URL is immutable. The checkpoint
is downloaded at runtime from the `weights_url` in the UDF context.

## Files

| File                 | Purpose                                                                                     |
| -------------------- | ------------------------------------------------------------------------------------------- |
| `process_graph.json` | Process definition served as the remote namespace; graph structure is maintained by hand.   |
| `udf.py`             | UDF source of truth, inlined into `process_graph.json` (see below).                         |
| `runtime.txt`        | Dependency archive URL the process needs; must appear in `default_job_options` (CI-checked). |
| `example.py`         | Sample call on CDSE.                                                                        |

The `udf` argument cannot reference `udf.py`: relative paths resolve against the
user's server-side workspace, not this repository, and URL UDFs are
backend-specific. Regenerate the inline copy after editing the UDF:

```sh
uv run scripts/build-process-graph.py processes/tessera_v2
```

`--check` verifies it is in sync without writing.

## Notes

- The id ends with `_embeddings` to state the output type and mark this as a
  foundation-model embedding process; no `infer_`/`load_` verb prefix.
- One directory per major model version, so v1 and v2 can coexist.
- The UDF is vendored from the
  [openEO community example](https://github.com/Open-EO/openeo-community-examples/tree/main/python/60_geospatial-embeddings/tessera)
  (Apache-2.0), pinned at submodule commit `938d997`.
