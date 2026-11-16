# Guide

This site collects geospatial foundation models and their embedding workflows in
a form that is ready to use from [openEO](https://openeo.org) at the
[Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu) (CDSE).

Models are shipped either as **weights** or as **pre-calculated frozen
embeddings**. Weights are cheaper to produce; frozen embeddings are cheaper to
use. The catalogue covers both approaches.

## Get Started

Install the [openEO Python client](https://openeo.org/documentation/1.0/python/) e.g. using pip:

```sh
pip install openeo
```

Calculate TESSERA v2 embeddings of the Region of Jena, Germany for 2025:

```python
import openeo

connection = openeo.connect("https://openeo.dataspace.copernicus.eu")
connection.authenticate_oidc()

embeddings = connection.datacube_from_process(
    "tessera_v2_embeddings",
    namespace="https://zenodo.org/records/24276408/files/models/tessera_v2/process_graph.json",
    spatial_extent={"west": 11.55, "south": 50.88, "east": 11.65, "north": 50.96},
    temporal_extent=["2025-01-01", "2026-01-01"],
)

job = embeddings.create_job(out_format="NETCDF")
job.start_and_wait()
job.download_result("tessera_v2_embeddings.nc")
```

Install Python libraries to analyze the output with this shell command:

```sh
pip install xarray matplotlib
```

Plot the first three bands of the result in Python:

```python
import xarray as xr
import matplotlib.pyplot as plt

ds = xr.open_dataset("tessera_v2_embeddings.nc")
a = ds[["tessera_0", "tessera_1", "tessera_2"]].to_array("bands").squeeze()
a.plot.imshow(rgb="bands", robust=True)
plt.show()
```

## Learn more

- [Browse models](/models/) for the available models and their parameters.
- [Contributing](/contributing) for model, runtime, and release conventions.
- [NFDI4Earth](https://nfdi4earth.de) for the project context.
