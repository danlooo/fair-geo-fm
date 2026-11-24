"""openEO UDF: TerraMind 1.0 Tiny patch embeddings via ONNX Runtime.

The TerraMind encoder is exported to ONNX once, offline, with
``scripts/export-terramind-onnx.py``. The process graph ships that archive
through ``udf-dependency-archives`` with the ``#terramind_onnx`` alias, so the
UDF only needs ``onnxruntime`` from the ``py311-onnx`` runtime archive. No
PyTorch or TerraTorch is available (or needed) in the sandbox.

TerraMind was pretrained on single 224x224 tiles of S2 L1C raw digital numbers,
Sentinel-1 GRD in decibels, and Copernicus DEM in metres. Its per-modality
mean/std normalization is baked into the ONNX graph, so this UDF hands raw
values straight to the model. Sentinel-1 is converted from the linear power
returned by ``sar_backscatter`` to decibels first.

Input cube bands (resolved by name, order irrelevant):
    S2 L1C: B01, B02, B03, B04, B05, B06, B07, B08, B8A, B09, B10, B11, B12
    S1 GRD: VV, VH
    DEM:    DEM (context["dem_band"])

The encoder returns one 192-dimensional token per 16x16-pixel patch. The UDF
replicates the 14x14 token grid back to the 224x224 input grid so
``apply_neighborhood``'s pixel-index overlap trimming stays aligned. Use
``resample_spatial`` downstream to shrink the cube to the native ~160 m grid.

Keep the openEO annotations as runtime objects: do not add
``from __future__ import annotations``. The UDF runner locates its entry point
by comparing the evaluated annotations with ``openeo.udf.XarrayDataCube``; a
postponed annotation is the string ``"XarrayDataCube"`` and never matches.
"""
import functools
import sys
from pathlib import Path

# onnxruntime comes from the model's runtime archive, extracted to feature_deps.
if Path("feature_deps").is_dir():
    sys.path.insert(0, "feature_deps")

import numpy as np
import onnxruntime as ort
import xarray as xr
from openeo.metadata import CubeMetadata
from openeo.udf import XarrayDataCube

S2_BAND_ORDER = [
    "B01",
    "B02",
    "B03",
    "B04",
    "B05",
    "B06",
    "B07",
    "B08",
    "B8A",
    "B09",
    "B10",
    "B11",
    "B12",
]
S1_BAND_ORDER = ["VV", "VH"]
DEFAULT_DEM_BAND = "DEM"

PATCH = 224
PATCH_TOKEN = 16
TOKENS_PER_SIDE = PATCH // PATCH_TOKEN  # 14
EMBED_DIM = 192

DEFAULT_ONNX_DIR = "terramind_onnx"
DEFAULT_ONNX_FILENAME = "terramind_v1_tiny_encoder.onnx"


@functools.lru_cache(maxsize=2)
def _load_session(onnx_path: str) -> ort.InferenceSession:
    """Return a cached InferenceSession for the exported encoder."""
    if not Path(onnx_path).is_file():
        raise FileNotFoundError(
            f"ONNX encoder not found at {onnx_path}. Make sure the model's "
            "weights archive is listed in 'udf-dependency-archives' with the "
            "'#terramind_onnx' alias, so it extracts to ./terramind_onnx."
        )
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 2
    return ort.InferenceSession(
        onnx_path,
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )


def _select_bands(array: xr.DataArray, names: list[str]) -> np.ndarray:
    available = list(array.coords["bands"].values.tolist())
    missing = [name for name in names if name not in available]
    if missing:
        raise ValueError(
            f"Input cube is missing required bands: {missing}. "
            f"Expected {names}; got {available}."
        )
    selected = array.sel(bands=names)
    return np.nan_to_num(
        selected.transpose("bands", "y", "x").values.astype(np.float32),
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    )


def _to_decibel(power: np.ndarray) -> np.ndarray:
    """Convert linear SAR power to dB, mapping non-positive values to 0 dB."""
    return np.where(power > 0, 10.0 * np.log10(np.maximum(power, 1e-6)), 0.0)


def _pad_to_patch(array: np.ndarray) -> tuple[np.ndarray, int, int]:
    height, width = array.shape[-2:]
    pad_h = max(0, PATCH - height)
    pad_w = max(0, PATCH - width)
    if pad_h or pad_w:
        array = np.pad(array, ((0, 0), (0, pad_h), (0, pad_w)))
    return array, height, width


def apply_metadata(metadata: CubeMetadata, context: dict) -> CubeMetadata:
    del context
    return metadata.rename_labels(
        dimension="bands",
        target=[f"terramind_{index}" for index in range(EMBED_DIM)],
    )


def apply_datacube(cube: XarrayDataCube, context: dict) -> XarrayDataCube:
    context = context or {}
    onnx_dir = context.get("onnx_dir", DEFAULT_ONNX_DIR)
    onnx_filename = context.get("onnx_filename", DEFAULT_ONNX_FILENAME)
    dem_band = context.get("dem_band") or DEFAULT_DEM_BAND
    onnx_path = (Path(onnx_dir) / onnx_filename).as_posix()

    array = cube.get_array()
    if "t" in array.dims:
        array = array.median(dim="t", skipna=True)

    session = _load_session(onnx_path)

    s2 = _select_bands(array, S2_BAND_ORDER)
    s1 = _to_decibel(_select_bands(array, S1_BAND_ORDER))
    dem = _select_bands(array, [dem_band])

    s2, height, width = _pad_to_patch(s2)
    s1, _, _ = _pad_to_patch(s1)
    dem, _, _ = _pad_to_patch(dem)

    tokens = session.run(
        None,
        {
            "s2l1c": s2[np.newaxis, ...],
            "s1grd": s1[np.newaxis, ...],
            "dem": dem[np.newaxis, ...],
        },
    )[0]
    tokens = np.asarray(tokens, dtype=np.float32).reshape(
        TOKENS_PER_SIDE, TOKENS_PER_SIDE, EMBED_DIM
    )
    grid = tokens.transpose(2, 0, 1)
    embedding = np.repeat(
        np.repeat(grid, PATCH_TOKEN, axis=1), PATCH_TOKEN, axis=2
    )[:, :height, :width]

    result = xr.DataArray(
        embedding,
        dims=["bands", "y", "x"],
        coords={
            "bands": [f"terramind_{index}" for index in range(EMBED_DIM)],
            "y": array.coords["y"],
            "x": array.coords["x"],
        },
    )
    return XarrayDataCube(result)
