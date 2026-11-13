# Getting started

This site collects geospatial foundation models and their embedding workflows in
a form that is ready to use from [openEO](https://openeo.org) at the
[Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu) (CDSE).

Models are shipped either as **weights** or as **pre-calculated frozen
embeddings**. Weights are cheaper to produce; frozen embeddings are cheaper to
use. The catalogue covers both approaches.

## Models and approaches

| Example     | Model type             | Host               | Format    | Approach                                                    |
| ----------- | ---------------------- | ------------------ | --------- | ----------------------------------------------------------- |
| AlphaEarth  | Frozen embeddings      | Cloud Ferro        | COG       | Load from a revised STAC catalogue                          |
| TESSERA v1  | Frozen embeddings      | Source Cooperative | Zarr      | Read Zarr in CDSE or convert to COG on the fly              |
| TESSERA v2  | Weights                | Hugging Face       | PyTorch   | Run a PyTorch UDF, or a CWL-to-STAC process graph           |
| Embed2Scale | Weights for finetuning | Hugging Face       | TerraTorch | Run a CWL-to-STAC process graph or UDF                      |

## Connect to CDSE

```python
import openeo

connection = openeo.connect("https://openeo.dataspace.copernicus.eu")
connection.authenticate_oidc()
```

## Call a model

Models are published as user-defined processes (UDPs) through the
[openEO remote process definition extension](https://api.openeo.org/extensions/remote-process-definition/0.1.0).
Reference a model's process graph by URL as its `namespace`:

```python
embeddings = connection.datacube_from_process(
    "tessera_v2_embeddings",
    namespace=(
        "https://zenodo.org/records/<record-id>/files/models/tessera_v2/process_graph.json"
    ),
    spatial_extent={"west": 11.55, "south": 50.88, "east": 11.65, "north": 50.96},
    temporal_extent=["2023-08-01", "2023-09-30"],
)
```

Replace `<record-id>` with the version record id linked from the model's page.
Released versions are archived on Zenodo under their own version record (not
the concept/root DOI). For unreleased staging builds use
<https://danlooo.github.io/fair-geo-fm/main/models/tessera_v2/process_graph.json>.
See [TESSERA v2 embeddings](/models/tessera_v2/) for the complete example and
parameters.

## Reproducibility

- Pin `namespace` to a release DOI, never to a branch, so stored jobs keep
  resolving to the same process graph. The staging site is overwritten on every
  deployment.
- One embedding per process graph: do not pack several models into one UDP.
- A process id encodes its major model version (`tessera_v2_embeddings`), so
  versions can be used side by side.

> [!NOTE]
> The openEO Web editor does not fetch metadata from the remote `namespace`
> URL. The editor only shows the graph you paste, so any user-facing
> information belongs in node descriptions or in the documentation pages here.

## Learn more

- [Browse models](/models/) for the available models and their parameters.
- [Contributing](/contributing) for process, runtime, and release conventions.
- [NFDI4Earth](https://nfdi4earth.de) for the project context.
