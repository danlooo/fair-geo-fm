"""openEO UDF: Prithvi-EO-2.0 (100M-TL) embeddings via ONNX Runtime.

Prithvi-EO-2.0 was pretrained on NASA's HLS product at 30 m with six bands in
the order Blue, Green, Red, Narrow NIR, SWIR 1, SWIR 2. HLS is not a CDSE
collection, so this UDF deliberately uses CDSE Sentinel-2 L2A as a proxy:

    Prithvi band   Sentinel-2 band
    BLUE           B02
    GREEN          B03
    RED            B04
    NIR_NARROW     B8A
    SWIR_1         B11
    SWIR_2         B12

This substitution changes gridding and harmonization, so embeddings can differ
from HLS-based inference. It is documented on the model page.

The encoder is exported to ONNX once, offline, with
``scripts/export-prithvi-onnx.py``. The process graph ships that archive
through ``udf-dependency-archives`` with the ``#prithvi_onnx`` alias, so the
UDF only needs ``onnxruntime`` from the ``py311-onnx`` runtime archive. No
PyTorch is available (or needed) in the sandbox.

The checkpoint's per-band mean/std and the nodata replacement are baked into
the graph, so this UDF hands raw reflectance (NaN for masked pixels) straight
to ONNX Runtime. The graph is static in time because Prithvi's sinusoidal
position embedding depends on the frame count; the UDF therefore always bins
the time series into four frames, matching the four-timestep pretraining.
Empty date bins fall back to the whole window, so short requests still produce
four frames.

The UDF runs inside ``apply_dimension(dimension="t", target_dimension="bands")``:
it receives ``[t, bands, y, x]``, masks clouds from SCL, bins the time series
into four frames, runs the frozen encoder in 224x224 windows, and returns
``[768, y, x]``. Prithvi is a patch model: the 14x14 token grid is upsampled
back to the 224x224 window, so neighboring pixels share context.

Keep the openEO annotations as runtime objects: do not add
``from __future__ import annotations``. The openEO UDF runner locates its entry
point by comparing the evaluated parameter and return annotations with
``openeo.udf.XarrayDataCube``; postponed annotations become the plain string
``"XarrayDataCube"``, which never matches, so every executor fails the job with
``openeo.udf.OpenEoUdfException: No UDF found.`` even though ``apply_datacube``
is defined.
"""
import functools
import math
import sys
import warnings
from pathlib import Path

# onnxruntime comes from the model's runtime archive, extracted to feature_deps.
if Path("feature_deps").is_dir():
    sys.path.insert(0, "feature_deps")

import numpy as np
import pandas as pd
import onnxruntime as ort
import xarray as xr
from openeo.metadata import CubeMetadata
from openeo.udf import XarrayDataCube

# Model input order: Blue, Green, Red, Narrow NIR, SWIR 1, SWIR 2.
BANDS = ("B02", "B03", "B04", "B8A", "B11", "B12")
SCL_BAND = "SCL"

# S2 L2A SCL classes treated as unusable: no data, saturated/defective, dark
# area, cloud shadow, cloud medium, cloud high. Thin cirrus (10) and
# unclassified (7) are kept, as in the TESSERA adapter.
INVALID_SCL = (0, 1, 2, 3, 8, 9)

NUM_FRAMES = 4
TOKENS_PER_SIDE = 14
EMBED_DIM = 768
# The official inference runs the encoder over 224x224 windows. Capping the
# UDF at the same window size bounds attention cost per forward pass on CDSE
# CPUs; the embedding is stitched back onto the chunk grid.
WINDOW = 224

DEFAULT_ONNX_DIR = "prithvi_onnx"
DEFAULT_ONNX_FILENAME = "prithvi_eo_v2_encoder.onnx"


@functools.lru_cache(maxsize=1)
def _load_session(onnx_path: str) -> ort.InferenceSession:
    """Return a cached InferenceSession for the exported encoder."""
    if not Path(onnx_path).is_file():
        raise FileNotFoundError(
            f"ONNX encoder not found at {onnx_path}. Make sure the model's "
            "weights archive is listed in 'udf-dependency-archives' with the "
            "'#prithvi_onnx' alias, so it extracts to ./prithvi_onnx."
        )
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 2
    return ort.InferenceSession(
        onnx_path,
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )


# =============================================================================
# Temporal binning and preprocessing
# =============================================================================
def _bin_selections(
    times: pd.DatetimeIndex,
) -> list[tuple[np.ndarray, pd.Timestamp]]:
    """Equal-width date bins, with their representative (midpoint) date.

    Empty bins fall back to the whole window so the model still receives
    ``NUM_FRAMES`` frames; the midpoint date is kept explicit.
    """
    start, end = times.min(), times.max()
    span = end - start
    if span == pd.Timedelta(0):
        selection = np.ones(len(times), dtype=bool)
        return [(selection, start)] * NUM_FRAMES

    selections: list[tuple[np.ndarray, pd.Timestamp]] = []
    for index in range(NUM_FRAMES):
        low = start + span * (index / NUM_FRAMES)
        high = start + span * ((index + 1) / NUM_FRAMES)
        if index < NUM_FRAMES - 1:
            selection = (times >= low) & (times < high)
        else:
            selection = (times >= low) & (times <= high)
        if not selection.any():
            selection = np.ones(len(times), dtype=bool)
        midpoint = low + (high - low) / 2
        selections.append((np.asarray(selection), midpoint))
    return selections


def _temporal_coords(dates: list[pd.Timestamp]) -> list[list[float]]:
    return [[float(date.year), float(date.dayofyear)] for date in dates]


def _extent_center(context: dict) -> tuple[float, float]:
    """Tile center (latitude, longitude) from the requested WGS84 extent."""
    extent = context.get("spatial_extent")
    if not isinstance(extent, dict):
        raise ValueError(
            "context['spatial_extent'] (a WGS84 bounding box) is required for "
            "Prithvi's location encoding."
        )
    try:
        west = float(extent["west"])
        south = float(extent["south"])
        east = float(extent["east"])
        north = float(extent["north"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "context['spatial_extent'] must contain west, south, east and north"
        ) from error
    return (south + north) / 2.0, (west + east) / 2.0


def _prepare_frames(
    da: xr.DataArray, scl_band: str
) -> tuple[np.ndarray, list[list[float]], int, int]:
    """Return raw ``[T,6,H,W]`` with NaN for masked pixels, coords and H/W."""
    available = set(da.coords["bands"].values.tolist())
    required = set(BANDS) | {scl_band}
    missing = required - available
    if missing:
        raise ValueError(
            f"Input cube is missing required bands: {sorted(missing)}. "
            f"Expected {list(BANDS)} and {scl_band!r}; got {sorted(available)}."
        )

    times = pd.DatetimeIndex(da.coords["t"].values)
    if len(times) == 0:
        raise ValueError("Input cube has an empty temporal dimension.")

    reflectance = (
        da.sel(bands=list(BANDS))
        .transpose("t", "y", "x", "bands")
        .values.astype(np.float32)
    )
    scl = (
        da.sel(bands=[scl_band])
        .isel(bands=0)
        .transpose("t", "y", "x")
        .values
    )
    scl_values = np.nan_to_num(scl, nan=-1).astype(np.int16)
    cloudy = np.isin(scl_values, INVALID_SCL) | ~np.isfinite(scl)
    reflectance = np.where(cloudy[..., None], np.nan, reflectance)

    height, width = reflectance.shape[1], reflectance.shape[2]
    frames = np.full(
        (NUM_FRAMES, height, width, len(BANDS)), np.nan, np.float32
    )
    dates: list[pd.Timestamp] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        for index, (selection, midpoint) in enumerate(_bin_selections(times)):
            frames[index] = np.nanmedian(reflectance[selection], axis=0)
            dates.append(midpoint)

    # Raw values: the graph applies the checkpoint's per-band mean/std and the
    # nodata replacement, so NaN travels through as NaN.
    return (
        frames.transpose(0, 3, 1, 2).astype(np.float32),
        _temporal_coords(dates),
        height,
        width,
    )


# =============================================================================
# Forward pass
# =============================================================================
def _resize_bilinear(
    grid: np.ndarray, out_height: int, out_width: int
) -> np.ndarray:
    """Bilinear resize of ``[C, H, W]``, matching torch ``align_corners=False``.

    Source coordinates are clamped at zero exactly like torch's
    ``area_pixel_compute_source_index`` for bilinear sampling.
    """
    channels, in_height, in_width = grid.shape

    def axis(out_size: int, in_size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        source = (np.arange(out_size, dtype=np.float64) + 0.5) * (
            in_size / out_size
        ) - 0.5
        source = np.maximum(source, 0.0)
        low = np.floor(source).astype(np.int64)
        high = np.minimum(low + 1, in_size - 1)
        weight = (source - low).astype(np.float32)
        return low, high, weight

    y0, y1, wy = axis(out_height, in_height)
    x0, x1, wx = axis(out_width, in_width)
    rows = grid[:, y0, :] * (1.0 - wy)[None, :, None] + grid[
        :, y1, :
    ] * wy[None, :, None]
    return (
        rows[:, :, x0] * (1.0 - wx)[None, None, :]
        + rows[:, :, x1] * wx[None, None, :]
    )


def _encode_window(
    session: ort.InferenceSession,
    window_tchw: np.ndarray,
    temporal_coords: list[list[float]],
    lon: float,
    lat: float,
) -> np.ndarray:
    """Run the encoder on one ``[T,6,224,224]`` window, returning ``[768,224,224]``."""
    height, width = window_tchw.shape[-2:]
    tokens = session.run(
        None,
        {
            "frames": window_tchw[np.newaxis].astype(np.float32),
            "temporal": np.asarray([temporal_coords], dtype=np.float32),
            "location": np.asarray([[lat, lon]], dtype=np.float32),
        },
    )[0]
    # The graph already averages the four frames' tokens.
    grid = tokens[0].reshape(
        TOKENS_PER_SIDE, TOKENS_PER_SIDE, EMBED_DIM
    ).transpose(2, 0, 1)
    return _resize_bilinear(grid, height, width)


def _encode(
    session: ort.InferenceSession,
    x_tchw: np.ndarray,
    temporal_coords: list[list[float]],
    lon: float,
    lat: float,
    height: int,
    width: int,
) -> np.ndarray:
    """Return a ``[768, H, W]`` embedding, encoded in 224x224 windows."""
    canvas_height = int(math.ceil(height / WINDOW) * WINDOW)
    canvas_width = int(math.ceil(width / WINDOW) * WINDOW)
    # NaN is Prithvi's nodata signal: the graph maps non-finite values to the
    # checkpoint's normalized nodata constant.
    canvas = np.full(
        (x_tchw.shape[0], x_tchw.shape[1], canvas_height, canvas_width),
        np.nan,
        np.float32,
    )
    canvas[:, :, :height, :width] = x_tchw
    embedding = np.empty((EMBED_DIM, canvas_height, canvas_width), np.float32)
    for y0 in range(0, canvas_height, WINDOW):
        for x0 in range(0, canvas_width, WINDOW):
            window = canvas[:, :, y0:y0 + WINDOW, x0:x0 + WINDOW]
            embedding[:, y0:y0 + WINDOW, x0:x0 + WINDOW] = _encode_window(
                session, window, temporal_coords, lon, lat
            )
    return embedding[:, :height, :width]


# =============================================================================
# openEO entry points
# =============================================================================
# These annotations must stay evaluated types; see the module docstring for why
# `from __future__ import annotations` would break `run_udf` with "No UDF found".
def apply_metadata(metadata: CubeMetadata, context: dict) -> CubeMetadata:
    del context  # the output width is fixed for the 100M-TL checkpoint
    return metadata.rename_labels(
        dimension="bands",
        target=[f"prithvi_{index}" for index in range(EMBED_DIM)],
    )


def apply_datacube(cube: XarrayDataCube, context: dict) -> XarrayDataCube:
    context = context or {}
    da = cube.get_array()
    if "t" not in da.dims:
        raise ValueError("Prithvi embeddings require a temporal dimension 't'.")

    scl_band = context.get("scl_band") or SCL_BAND
    onnx_dir = context.get("onnx_dir", DEFAULT_ONNX_DIR)
    onnx_filename = context.get("onnx_filename", DEFAULT_ONNX_FILENAME)
    onnx_path = (Path(onnx_dir) / onnx_filename).as_posix()
    lat, lon = _extent_center(context)

    x_tchw, temporal_coords, height, width = _prepare_frames(da, scl_band)
    session = _load_session(onnx_path)
    embedding = _encode(
        session, x_tchw, temporal_coords, lon, lat, height, width
    )

    result = xr.DataArray(
        embedding,
        dims=["bands", "y", "x"],
        coords={
            "bands": [f"prithvi_{index}" for index in range(EMBED_DIM)],
            "y": da.coords["y"],
            "x": da.coords["x"],
        },
    )
    return XarrayDataCube(result)
