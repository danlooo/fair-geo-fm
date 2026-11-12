"""Run the TESSERA v2 embedding process on CDSE for the Jena area in 2025.

The process is not stored on the backend: CDSE resolves it from the public URL
below through the remote-process-definition extension, so the submitted process
graph is a single node. Batch-job options (torch archive, memory) come from the
JSON's ``default_job_options``.
"""

from pathlib import Path

import openeo

OUT_DIR = Path("downloads/tessera-jena-2025")

# Public raw URL of process_graph.json. It must be fetchable without
# credentials; CDSE workers do not forward your openEO token to external URLs.
PROCESS_URL = (
    "https://raw.githubusercontent.com/danlooo/fair-geo-fm/main/"
    "models/tessera_v2/process_graph.json"
)
PROCESS_ID = "tessera_v2_embeddings"

# Jena city centre, roughly 7 x 9 km.
JENA_BBOX = {"west": 11.55, "south": 50.88, "east": 11.65, "north": 50.96}
TEMPORAL_EXTENT = ["2025-01-01", "2025-12-31"]

connection = openeo.connect("openeo.dataspace.copernicus.eu").authenticate_oidc()

cube = connection.datacube_from_process(
    PROCESS_ID,
    namespace=PROCESS_URL,
    spatial_extent=JENA_BBOX,
    temporal_extent=TEMPORAL_EXTENT,
)

job = cube.save_result(format="NetCDF").create_job(
    title="TESSERA v2 embeddings Jena 2025",
)
print(f"Created job {job.job_id}")

status = job.start_and_wait()
if status != "finished":
    raise SystemExit(f"Job {job.job_id} ended with status {status!r}; check its logs.")

OUT_DIR.mkdir(parents=True, exist_ok=True)
paths = job.get_results().download_files(OUT_DIR)
print(f"Downloaded {len(paths)} file(s) to {OUT_DIR}")
