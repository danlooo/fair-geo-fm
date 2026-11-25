"""openEO UDF: TESSERA v2 "student" pixel embeddings via ONNX Runtime.

The TESSERA v2 pixel student is exported to ONNX once, offline, with
``scripts/export-tessera-onnx.py``. The process graph ships that archive
through ``udf-dependency-archives`` with the ``#tessera_onnx`` alias, so the
UDF only needs ``onnxruntime`` from the ``py311-onnx`` runtime archive. No
PyTorch is available (or needed) in the sandbox.

The model definition and the binning/standardization helpers are vendored from
`tessera_infer_v2/student/{model.py,infer.py}` in
https://github.com/ucam-eo/tessera. The exported graph takes each pixel's
already standardized S2 and S1 sequences and returns the 128-dimensional
Matryoshka embedding; only the transformer forward pass runs in ONNX. The
optional ``embedding_length`` context entry selects a Matryoshka prefix (one of
the checkpoint's trained cuts, 16/32/64/128); it defaults to the full width.

Expects the merged input cube to contain (at least) these bands, produced by
merge_cubes() of three separately-loaded collections (see TesseraEmbedding.ipynb):
    S2:        B04, B02, B03, B08, B8A, B05, B06, B07, B11, B12, SCL
    S1 asc:    VV_ASC, VH_ASC
    S1 desc:   VV_DESC, VH_DESC
Because merge_cubes() unions the "t" dimension, a given band's source can be
all-NaN on dates that belong to another source. This UDF first prunes those
source-mismatch dates per source, then applies SCL-based cloud masking on S2
(SCL classes 0-3, 8, 9) so cloud NaNs don't interfere with source-date pruning.

Keep the openEO annotations as runtime objects: do not add
``from __future__ import annotations``. The UDF runner locates its entry point
by comparing the evaluated annotations with ``openeo.udf.XarrayDataCube``.
"""
import functools
import sys
from pathlib import Path
from typing import Optional

# onnxruntime comes from the model's runtime archive, extracted to feature_deps.
if Path("feature_deps").is_dir():
    sys.path.insert(0, "feature_deps")

import numpy as np
import pandas as pd
import onnxruntime as ort
import xarray as xr
from openeo.metadata import CubeMetadata
from openeo.udf import XarrayDataCube

# =============================================================================
# Model constants (vendored from tessera_infer_v2/student/model.py)
# =============================================================================
S2_BAND_MEAN = np.array(
    [1633.0042, 1341.1090, 1539.5536, 3054.8269, 3117.4658,
     2004.1648, 2694.7275, 2945.1504, 2266.6079, 1657.3094],
    dtype=np.float32,
)
S2_BAND_STD = np.array(
    [1999.4603, 2014.7549, 1929.2201, 1754.2493, 1649.9807,
     1936.8988, 1748.6041, 1708.6991, 1207.5250, 1108.6046],
    dtype=np.float32,
)
S1A_BAND_MEAN = np.array([5909.3921, 3405.0322], dtype=np.float32)
S1A_BAND_STD = np.array([1507.1750, 1531.2615], dtype=np.float32)
S1D_BAND_MEAN = np.array([5816.1382, 3277.7576], dtype=np.float32)
S1D_BAND_STD = np.array([1554.6475, 1546.4733], dtype=np.float32)

S2_BAND_ORDER = ["B04", "B02", "B03", "B08", "B8A", "B05", "B06", "B07", "B11", "B12"]
S2_SCL_BAND = "SCL"
# Matches TESSERA v2 training pipeline (tessera_preprocessing/s2_fast_processor.py::SCL_INVALID):
# 0=NoData, 1=Saturated/Defective, 2=Dark area, 3=Cloud shadow, 8=Cloud medium, 9=Cloud high.
# Classes 7 (Unclassified) and 10 (Thin cirrus) are kept as VALID at training time.
S2_CLOUD_SCL_CLASSES = np.array([0, 1, 2, 3, 8, 9], dtype=np.int16)
S1_BAND_ORDER = ["VV", "VH"]

EMBED_DIM = 128
MATRYOSHKA_DIMS = (16, 32, 64, 128)

DEFAULT_ONNX_DIR = "tessera_onnx"
DEFAULT_ONNX_FILENAME = "tessera_v2_encoder.onnx"

# =============================================================================
# Binning helpers (vendored from tessera_infer_v2/student/infer.py)
# =============================================================================
BIN_EDGES = np.array(list(range(8, 257, 8)), dtype=np.int64)  # [8, 16, 24, ..., 256]


@functools.lru_cache(maxsize=1)
def _load_session(onnx_path: str) -> ort.InferenceSession:
    """Return a cached InferenceSession for the exported encoder."""
    if not Path(onnx_path).is_file():
        raise FileNotFoundError(
            f"ONNX encoder not found at {onnx_path}. Make sure the model's "
            "weights archive is listed in 'udf-dependency-archives' with the "
            "'#tessera_onnx' alias, so it extracts to ./tessera_onnx."
        )
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 2
    return ort.InferenceSession(
        onnx_path,
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )


def _vec_get_bin_size(n_obs: np.ndarray) -> np.ndarray:
    """Vectorized: smallest bin edge >= n_obs, clamped to [0, max_edge]."""
    idx = np.searchsorted(BIN_EDGES, n_obs, side="left")
    idx = np.clip(idx, 0, len(BIN_EDGES) - 1)
    return np.where(n_obs <= 0, 0, BIN_EDGES[idx])


def _pad_pattern(n: int, B: int) -> np.ndarray:
    """(B,) int64 indices into [0, n) reproducing the training pad_to_bin."""
    if n == 0:
        return np.zeros(B, dtype=np.int64)
    if n >= B:
        return np.linspace(0, n - 1, B, dtype=np.int64)
    remain = B - n
    if remain <= n:
        groups = np.array_split(np.arange(n), remain)
        fill = np.array([gp[len(gp) // 2] for gp in groups], dtype=np.int64)
    else:
        fill = (np.arange(remain) % n).astype(np.int64)
    return np.concatenate([np.arange(n, dtype=np.int64), fill])


def _build_source_indices(valid_per_pix: np.ndarray, B: int) -> np.ndarray:
    """valid_per_pix: (G, T) bool. Returns (G, B) int64."""
    G, T = valid_per_pix.shape
    src = np.zeros((G, B), dtype=np.int64)
    if G == 0 or B == 0:
        return src
    n_per = valid_per_pix.sum(axis=1).astype(np.int64)
    sorted_pos = np.argsort(~valid_per_pix, axis=1, kind="stable").astype(np.int64)
    unique_n, inverse = np.unique(n_per, return_inverse=True)
    for ki, n_val in enumerate(unique_n):
        n = int(n_val)
        if n == 0:
            continue
        pix = np.where(inverse == ki)[0]
        pattern = _pad_pattern(n, B)
        src[pix] = sorted_pos[pix][:, pattern]
    return src


def encode_pixels(
    session: ort.InferenceSession,
    s2_bands: np.ndarray,
    s2_doys: np.ndarray,
    s1_asc_bands: Optional[np.ndarray] = None,
    s1_asc_doys: Optional[np.ndarray] = None,
    s1_desc_bands: Optional[np.ndarray] = None,
    s1_desc_doys: Optional[np.ndarray] = None,
    s2_masks: Optional[np.ndarray] = None,
    batch_pixels: int = 1024,
    standardize: bool = True,
) -> np.ndarray:
    """Encode B independent pixels' time series into 128-d embeddings.

    Args:
        s2_bands     : (B, T_s2, 10)
        s2_doys      : (B, T_s2) or (T_s2,) ints (1..365)
        s1_asc_bands : (B, T_s1a, 2) raw (or None)
        s1_asc_doys  : (B, T_s1a) or (T_s1a,)
        s1_desc_bands: (B, T_s1d, 2) raw (or None)
        s1_desc_doys : (B, T_s1d) or (T_s1d,)
        s2_masks     : (B, T_s2) 1=valid, 0=cloud (or None -> all valid)
    Returns: (B, 128) float32.
    """
    B = s2_bands.shape[0]
    out = np.empty((B, EMBED_DIM), dtype=np.float32)
    if B == 0:
        return out

    T_s2 = s2_bands.shape[1]

    # Build per-pixel S1 (merged) arrays. asc and desc each have their own
    # mean/std - z-score per-source BEFORE concatenation so the merged S1
    # stream the model consumes is already standardized.
    if s1_asc_bands is not None and s1_asc_bands.size > 0:
        s1a_b = s1_asc_bands.astype(np.float32)
        s1a_valid = np.any((s1a_b != 0) & ~np.isnan(s1a_b), axis=-1)
        s1a_b = np.nan_to_num(s1a_b, nan=0.0)
        if standardize:
            s1a_b = (s1a_b - S1A_BAND_MEAN) / (S1A_BAND_STD + 1e-9)
        if s1_asc_doys.ndim == 1:
            s1a_d = np.broadcast_to(s1_asc_doys[None, :], (B, s1_asc_doys.shape[0])).copy()
        else:
            s1a_d = s1_asc_doys
    else:
        s1a_b = np.zeros((B, 0, 2), dtype=np.float32)
        s1a_d = np.zeros((B, 0), dtype=np.float32)
        s1a_valid = np.zeros((B, 0), dtype=bool)

    if s1_desc_bands is not None and s1_desc_bands.size > 0:
        s1d_b = s1_desc_bands.astype(np.float32)
        s1d_valid = np.any((s1d_b != 0) & ~np.isnan(s1d_b), axis=-1)
        s1d_b = np.nan_to_num(s1d_b, nan=0.0)
        if standardize:
            s1d_b = (s1d_b - S1D_BAND_MEAN) / (S1D_BAND_STD + 1e-9)
        if s1_desc_doys.ndim == 1:
            s1d_d = np.broadcast_to(s1_desc_doys[None, :], (B, s1_desc_doys.shape[0])).copy()
        else:
            s1d_d = s1_desc_doys
    else:
        s1d_b = np.zeros((B, 0, 2), dtype=np.float32)
        s1d_d = np.zeros((B, 0), dtype=np.float32)
        s1d_valid = np.zeros((B, 0), dtype=bool)

    if s1a_b.shape[1] + s1d_b.shape[1] > 0:
        s1_b_merged = np.concatenate([s1a_b, s1d_b], axis=1)
        s1_d_merged = np.concatenate([s1a_d, s1d_d], axis=1)
    else:
        s1_b_merged = np.zeros((B, 0, 2), dtype=np.float32)
        s1_d_merged = np.zeros((B, 0), dtype=np.float32)

    if s2_masks is not None:
        s2_v = s2_masks.astype(bool)
    else:
        s2_v = np.ones((B, T_s2), dtype=bool)
    s1_v = (np.concatenate([s1a_valid, s1d_valid], axis=1)
            if (s1a_valid.shape[1] + s1d_valid.shape[1]) > 0
            else np.zeros((B, 0), dtype=bool))

    n_s2 = s2_v.sum(axis=1)
    n_s1 = s1_v.sum(axis=1)
    s2_bin = _vec_get_bin_size(n_s2).astype(np.int32)
    s1_bin = _vec_get_bin_size(n_s1).astype(np.int32)
    keys = s2_bin * 1000 + s1_bin
    unique_keys, inverse = np.unique(keys, return_inverse=True)

    for ki, key in enumerate(unique_keys):
        s2_b_size = int(key // 1000)
        s1_b_size = int(key % 1000)
        idxs = np.where(inverse == ki)[0]
        if s2_b_size == 0 and s1_b_size == 0:
            continue
        s2_B = max(s2_b_size, 1)
        s1_B = max(s1_b_size, 1)
        for s in range(0, idxs.size, batch_pixels):
            chunk = idxs[s: s + batch_pixels]
            G = len(chunk)
            s2_in = np.zeros((G, s2_B, 11), dtype=np.float32)
            s1_in = np.zeros((G, s1_B, 3), dtype=np.float32)

            if s2_b_size > 0:
                src = _build_source_indices(s2_v[chunk], s2_B)
                gathered = np.take_along_axis(
                    s2_bands[chunk], src[:, :, None].repeat(10, axis=2), axis=1
                )
                if standardize:
                    gathered = (gathered - S2_BAND_MEAN) / (S2_BAND_STD + 1e-9)
                s2_in[:, :, :10] = gathered
                if s2_doys.ndim == 1:
                    s2_doys_pix = np.broadcast_to(s2_doys[None, :], (G, T_s2))
                else:
                    s2_doys_pix = s2_doys[chunk]
                s2_in[:, :, 10] = np.take_along_axis(s2_doys_pix, src, axis=1).astype(np.float32)

            if s1_b_size > 0:
                src = _build_source_indices(s1_v[chunk], s1_B)
                gathered = np.take_along_axis(
                    s1_b_merged[chunk], src[:, :, None].repeat(2, axis=2), axis=1
                )
                s1_in[:, :, :2] = gathered
                s1_in[:, :, 2] = np.take_along_axis(s1_d_merged[chunk], src, axis=1).astype(np.float32)

            emb = session.run(None, {"s2_x": s2_in, "s1_x": s1_in})[0]
            out[chunk] = np.asarray(emb, dtype=np.float32)
    return out


def encode_tile(
    session: ort.InferenceSession,
    s2_bands: np.ndarray,
    s2_doys: np.ndarray,
    s2_masks: Optional[np.ndarray] = None,
    s1_asc_bands: Optional[np.ndarray] = None,
    s1_asc_doys: Optional[np.ndarray] = None,
    s1_desc_bands: Optional[np.ndarray] = None,
    s1_desc_doys: Optional[np.ndarray] = None,
    batch_pixels: int = 1024,
    standardize: bool = True,
) -> np.ndarray:
    """Encode one tile into an (H, W, 128) embedding map.

    Args:
        s2_bands     : (T_s2, H, W, 10)  raw reflectance
        s2_doys      : (T_s2,)           day-of-year per S2 frame
        s2_masks     : (T_s2, H, W) or None (1=valid)
        s1_asc_bands : (T_s1a, H, W, 2) optional
        s1_asc_doys  : (T_s1a,)         optional
        s1_desc_bands: (T_s1d, H, W, 2) optional
        s1_desc_doys : (T_s1d,)         optional
    Returns: (H, W, 128) float32.
    """
    T_s2, H, W, _ = s2_bands.shape
    out = np.empty((H, W, EMBED_DIM), dtype=np.float32)

    # Spatial tiling: process rows in chunks so we never materialize the full
    # (H*W, T, C) flat array (which is a copy because of transpose+reshape).
    # ROW_CHUNK * W pixels per iteration keeps peak memory bounded regardless
    # of the openEO chunk size passed in.
    ROW_CHUNK = max(1, min(H, max(1, 16384 // max(W, 1))))

    for y0 in range(0, H, ROW_CHUNK):
        y1 = min(H, y0 + ROW_CHUNK)
        h = y1 - y0
        N = h * W

        s2_flat = s2_bands[:, y0:y1, :, :].transpose(1, 2, 0, 3).reshape(N, T_s2, 10)
        s2_masks_flat = (
            s2_masks[:, y0:y1, :].transpose(1, 2, 0).reshape(N, T_s2)
            if s2_masks is not None else None
        )

        if s1_asc_bands is not None and s1_asc_bands.size > 0:
            Ta = s1_asc_bands.shape[0]
            s1a_flat = s1_asc_bands[:, y0:y1, :, :].transpose(1, 2, 0, 3).reshape(N, Ta, 2)
            s1a_doys_flat = np.broadcast_to(s1_asc_doys[None, :], (N, Ta))
        else:
            s1a_flat = None
            s1a_doys_flat = None

        if s1_desc_bands is not None and s1_desc_bands.size > 0:
            Td = s1_desc_bands.shape[0]
            s1d_flat = s1_desc_bands[:, y0:y1, :, :].transpose(1, 2, 0, 3).reshape(N, Td, 2)
            s1d_doys_flat = np.broadcast_to(s1_desc_doys[None, :], (N, Td))
        else:
            s1d_flat = None
            s1d_doys_flat = None

        emb = encode_pixels(
            session, s2_flat, s2_doys,
            s1_asc_bands=s1a_flat, s1_asc_doys=s1a_doys_flat,
            s1_desc_bands=s1d_flat, s1_desc_doys=s1d_doys_flat,
            s2_masks=s2_masks_flat,
            batch_pixels=batch_pixels, standardize=standardize,
        )
        out[y0:y1] = emb.reshape(h, W, EMBED_DIM)
    return out


# =============================================================================
# openEO entry points.
# =============================================================================
def _output_width(context: dict) -> int:
    """The requested Matryoshka width, validated against the checkpoint cuts.

    ``embedding_length`` is optional; omitting it returns the full embedding.
    Only the cuts the model was trained with are meaningful, so an arbitrary
    prefix is rejected.
    """
    value = (context or {}).get("embedding_length")
    if value is None or value == "":
        return EMBED_DIM
    try:
        width = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"context['embedding_length'] must be an integer, got {value!r}"
        ) from None
    if width not in MATRYOSHKA_DIMS:
        raise ValueError(
            f"context['embedding_length']={width} is not a trained Matryoshka cut; "
            f"choose one of {list(MATRYOSHKA_DIMS)}"
        )
    return width


def apply_metadata(metadata: CubeMetadata, context: dict) -> CubeMetadata:
    # This UDF collapses the time dimension into output bands, so it must
    # be invoked via apply_dimension(dimension="t", target_dimension="bands")
    # (see TesseraEmbedding.ipynb). The metadata rename below assumes t is
    # already gone by the time the callback runs.
    width = _output_width(context)
    band_names = [f"tessera_{i}" for i in range(width)]
    return metadata.rename_labels(dimension="bands", target=band_names)


def _tessera_doy_values(t_coord_values: np.ndarray) -> np.ndarray:
    """Convert timestamps to TESSERA DOY values in [1, 365].

    TESSERA was trained with calendar-day DOY. Leap day (366) is folded to 365
    so leap and non-leap years stay on the same positional scale.
    """
    t_index = pd.DatetimeIndex(t_coord_values)
    doy = t_index.dayofyear.to_numpy().astype(np.float32)
    return np.minimum(doy, 365.0)


def apply_datacube(cube: XarrayDataCube, context: dict) -> XarrayDataCube:
    context = context or {}
    onnx_dir = context.get("onnx_dir", DEFAULT_ONNX_DIR)
    onnx_filename = context.get("onnx_filename", DEFAULT_ONNX_FILENAME)
    onnx_path = (Path(onnx_dir) / onnx_filename).as_posix()
    session = _load_session(onnx_path)
    da = cube.get_array()  # dims: t, bands, y, x

    available = set(da.coords["bands"].values.tolist())
    required = (
        set(S2_BAND_ORDER)
        | {S2_SCL_BAND}
        | {f"{b}_ASC" for b in S1_BAND_ORDER}
        | {f"{b}_DESC" for b in S1_BAND_ORDER}
    )
    missing = required - available
    if missing:
        raise ValueError(
            f"Input cube is missing required bands: {sorted(missing)}. "
            f"Expected S2 bands {S2_BAND_ORDER} + SCL, S1 asc VV_ASC/VH_ASC, "
            f"S1 desc VV_DESC/VH_DESC. Got: {sorted(available)}."
        )

    doy = _tessera_doy_values(da.coords["t"].values)

    def select(band_names, label: str):
        sub = da.sel(bands=band_names)
        valid = sub.notnull().any(dim=["bands", "y", "x"]).values
        idx = np.where(valid)[0]
        arr = sub.isel(t=idx).transpose("t", "y", "x", "bands").values.astype(np.float32)
        return arr, doy[idx]

    # 1) Prune merge-introduced NaN-only dates first by selecting S2-native dates.
    # 2) Then apply cloud masking from SCL to create cloud NaNs.
    s2_with_scl, s2_doys = select(S2_BAND_ORDER + [S2_SCL_BAND], "s2")
    s2_bands_raw = s2_with_scl[:, :, :, :len(S2_BAND_ORDER)]
    s2_scl = s2_with_scl[:, :, :, len(S2_BAND_ORDER)]
    # Treat SCL NaN as "invalid" (nodata) rather than letting float->int16 UB
    # decide. Class 0 is "no data" in SCL, but we also mark it invalid below.
    scl_nan = np.isnan(s2_scl)
    scl_int = np.where(scl_nan, 0, s2_scl).astype(np.int16)
    cloudy = np.isin(scl_int, S2_CLOUD_SCL_CLASSES) | scl_nan
    s2_bands_masked = np.where(cloudy[:, :, :, None], np.nan, s2_bands_raw)
    s2_masks = ~np.isnan(s2_bands_masked).any(axis=-1)  # bool (T, H, W)
    s2_bands = np.nan_to_num(s2_bands_masked)

    s1a_names = [f"{b}_ASC" for b in S1_BAND_ORDER]
    s1d_names = [f"{b}_DESC" for b in S1_BAND_ORDER]
    s1a_bands, s1a_doys = select(s1a_names, "s1_asc")
    s1d_bands, s1d_doys = select(s1d_names, "s1_desc")
    s1a_bands = np.nan_to_num(s1a_bands)
    s1d_bands = np.nan_to_num(s1d_bands)

    embedding = encode_tile(
        session, s2_bands, s2_doys, s2_masks=s2_masks,
        s1_asc_bands=s1a_bands, s1_asc_doys=s1a_doys,
        s1_desc_bands=s1d_bands, s1_desc_doys=s1d_doys,
    )  # (H, W, repr_dim)

    width = _output_width(context)
    embedding = embedding[:, :, :width]

    result = xr.DataArray(
        embedding.transpose(2, 0, 1),
        dims=["bands", "y", "x"],
        coords={"bands": [f"tessera_{i}" for i in range(width)], "y": da.coords["y"], "x": da.coords["x"]},
    )
    return XarrayDataCube(result)
