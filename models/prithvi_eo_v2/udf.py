"""openEO UDF: Prithvi-EO-2.0 (100M-TL) embeddings from CDSE Sentinel-2 L2A.

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
from HLS-based Prithvi inference. It is documented on the model page.

The model definition below is vendored from the official checkpoint repository
(`prithvi_mae.py`, Copyright (c) IBM Corp. 2024, Apache-2.0). Preprocessing
follows that repository: per-band `(reflectance - mean) / std` with the
checkpoint's band statistics, and nodata mapped to 0.0001.

The UDF runs inside `apply_dimension(dimension="t", target_dimension="bands")`:
it receives `[t, bands, y, x]`, bins the time series into at most four frames
(matching Prithvi's 4-timestep pretraining), masks clouds from SCL, runs the
frozen encoder, and returns `[768, y, x]`. Prithvi is a patch model: the token
grid is upsampled back to the input grid, so neighboring pixels share context.

Keep the openEO annotations as runtime objects: do not add
``from __future__ import annotations``. The openEO UDF runner locates its entry
point by comparing the evaluated parameter and return annotations with
``openeo.udf.XarrayDataCube``; postponed annotations become the plain string
``"XarrayDataCube"``, which never matches, so every executor fails the job with
``openeo.udf.OpenEoUdfException: No UDF found.`` even though ``apply_datacube``
is defined. Every name used in an annotation here is imported or defined at
module level before use, so the annotations evaluate at definition time.
"""
import functools
import math
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import warnings

_DEPS_DIR = os.environ.get("PRITHVI_TORCH_DEPS_DIR", "feature_deps")
if os.path.isdir(_DEPS_DIR):
    sys.path.insert(0, _DEPS_DIR)

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
import xarray as xr
from openeo.metadata import CubeMetadata
from openeo.udf import XarrayDataCube
from pyproj import CRS, Transformer

# Keep per-worker CPU usage predictable inside the openEO sandbox.
torch.set_num_threads(2)
try:
    torch.set_num_interop_threads(2)
except RuntimeError:
    pass

# =============================================================================
# CDSE / model constants
# =============================================================================
# Pinned to the Hugging Face repository commit, never to a mutable branch.
DEFAULT_WEIGHTS_URL = (
    "https://huggingface.co/ibm-nasa-geospatial/Prithvi-EO-2.0-100M-TL/"
    "resolve/2c84e383194986040f883cc43d7869002c425e1b/Prithvi_EO_V2_100M_TL.pt"
)

# Model input order: Blue, Green, Red, Narrow NIR, SWIR 1, SWIR 2.
BANDS = ("B02", "B03", "B04", "B8A", "B11", "B12")
SCL_BAND = "SCL"

# HLS band statistics shipped with the 100M-TL checkpoint.
BAND_MEAN = np.array(
    [1087.0, 1342.0, 1433.0, 2734.0, 1958.0, 1363.0], dtype=np.float32
)
BAND_STD = np.array(
    [2248.0, 2179.0, 2178.0, 1850.0, 1242.0, 1049.0], dtype=np.float32
)

# S2 L2A SCL classes treated as unusable: no data, saturated/defective, dark
# area, cloud shadow, cloud medium, cloud high. Thin cirrus (10) and
# unclassified (7) are kept, as in the TESSERA adapter.
INVALID_SCL = (0, 1, 2, 3, 8, 9)

PATCH = 16
MAX_FRAMES = 4
NODATA_FLOAT = 0.0001
EMBED_DIM = 768
# The official inference runs the encoder over 224x224 windows. Capping the
# UDF at the same window size bounds attention cost per forward pass on CDSE
# CPUs; the embedding is stitched back onto the chunk grid.
WINDOW = 224

MODEL_CONFIG = {
    "img_size": 224,
    "num_frames": MAX_FRAMES,
    "patch_size": (1, PATCH, PATCH),
    "in_chans": len(BANDS),
    "embed_dim": EMBED_DIM,
    "depth": 12,
    "num_heads": 12,
    "decoder_embed_dim": 512,
    "decoder_depth": 8,
    "decoder_num_heads": 16,
    "mlp_ratio": 4,
    "coords_encoding": ["time", "location"],
    "coords_scale_learn": True,
    "mask_ratio": 0.75,
    "norm_pix_loss": False,
}


# =============================================================================
# Minimal timm-compatible layers
# =============================================================================
# The vendored Prithvi runtime only needs timm's `to_2tuple` and ViT `Block`.
# Re-implementing them here keeps the CDSE dependency archive free of timm,
# torchvision and their numpy/pillow chain (numpy is supplied by the sandbox
# and must not be shipped in `feature_deps`). Parameter names and the eval-mode
# forward pass match timm so the released checkpoint loads with strict=True.
def to_2tuple(value):
    if isinstance(value, (tuple, list)):
        return tuple(value)
    return (value, value)


class _Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, attn_drop=0.0, proj_drop=0.0):
        super().__init__()
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.scale = self.head_dim ** -0.5
        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim, bias=True)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x):
        batch, tokens, channels = x.shape
        qkv = self.qkv(x).reshape(
            batch, tokens, 3, self.num_heads, self.head_dim
        ).permute(2, 0, 3, 1, 4)
        query, key, value = qkv.unbind(0)
        x = F.scaled_dot_product_attention(
            query, key, value,
            dropout_p=self.attn_drop.p if self.training else 0.0,
        )
        x = x.transpose(1, 2).reshape(batch, tokens, channels)
        return self.proj_drop(self.proj(x))


class _Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None,
                 act_layer=nn.GELU, drop=0.0, bias=True):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features, bias=bias)
        self.act = act_layer()
        self.drop1 = nn.Dropout(drop)
        self.norm = nn.Identity()
        self.fc2 = nn.Linear(hidden_features, out_features, bias=bias)
        self.drop2 = nn.Dropout(drop)

    def forward(self, x):
        x = self.drop1(self.act(self.fc1(x)))
        x = self.norm(x)
        return self.drop2(self.fc2(x))


class Block(nn.Module):
    """Pre-normalized ViT block with timm-compatible parameter names."""

    def __init__(self, dim, num_heads, mlp_ratio=4.0, qkv_bias=False,
                 norm_layer=nn.LayerNorm, drop_path=0.0, **_ignored):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = _Attention(dim, num_heads=num_heads, qkv_bias=qkv_bias)
        self.ls1 = nn.Identity()
        self.drop_path1 = nn.Identity()
        self.norm2 = norm_layer(dim)
        self.mlp = _Mlp(
            in_features=dim, hidden_features=int(dim * mlp_ratio),
            act_layer=nn.GELU,
        )
        self.ls2 = nn.Identity()
        self.drop_path2 = nn.Identity()

    def forward(self, x, **_ignored):
        x = x + self.drop_path1(self.ls1(self.attn(self.norm1(x))))
        x = x + self.drop_path2(self.ls2(self.mlp(self.norm2(x))))
        return x


# =============================================================================
# Checkpoint loading (one per worker and frame count)
# =============================================================================
def _download_checkpoint(url: str, timeout: int = 300) -> str:
    """Stream the checkpoint to a temporary file, with retries."""
    handle = tempfile.NamedTemporaryFile(suffix=".pt", delete=False)
    path = handle.name
    handle.close()
    request = urllib.request.Request(
        url, headers={"User-Agent": "fair-geo-fm-prithvi-udf/1"}
    )
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                with open(path, "wb") as target:
                    shutil.copyfileobj(response, target, length=1 << 20)
            return path
        except Exception as error:  # noqa: BLE001 - retry on any network error
            last_error = error
            try:
                os.unlink(path)
            except OSError:
                pass
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise RuntimeError(
        f"Failed to download Prithvi weights from {url}: {last_error}"
    )


@functools.lru_cache(maxsize=4)
def _get_model(weights_url: str, num_frames: int):
    model = PrithviMAE(**{**MODEL_CONFIG, "num_frames": int(num_frames)})
    path = _download_checkpoint(weights_url)
    try:
        state = torch.load(path, map_location="cpu", weights_only=True)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not isinstance(state, dict):
        raise ValueError(
            f"Unexpected Prithvi checkpoint object: {type(state).__name__}"
        )
    # The released checkpoints fix the positional embeddings to 4 frames.
    # Replace them with the runtime-shaped ones, as the official inference
    # script and rs-embed do.
    if "encoder.pos_embed" in state:
        state["encoder.pos_embed"] = model.encoder.pos_embed
    if "decoder.decoder_pos_embed" in state:
        state["decoder.decoder_pos_embed"] = model.decoder.decoder_pos_embed
    model.load_state_dict(state, strict=True)
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return model


# =============================================================================
# Temporal binning and preprocessing
# =============================================================================
def _num_frames(times: pd.DatetimeIndex) -> int:
    """Match Prithvi's 4-timestep, >=28-day-gap pretraining."""
    span_days = max((times.max() - times.min()).days, 0)
    return int(min(MAX_FRAMES, max(1, span_days // 28)))


def _bin_selections(
    times: pd.DatetimeIndex, num_frames: int
) -> list[tuple[np.ndarray, pd.Timestamp]]:
    """Equal-width date bins, with their representative (midpoint) date.

    Empty bins fall back to the whole window so the model still receives the
    requested number of frames; the midpoint date is kept explicit.
    """
    start, end = times.min(), times.max()
    span = end - start
    if span == pd.Timedelta(0):
        selection = np.ones(len(times), dtype=bool)
        return [(selection, start)] * num_frames

    selections: list[tuple[np.ndarray, pd.Timestamp]] = []
    for index in range(num_frames):
        low = start + span * (index / num_frames)
        high = start + span * ((index + 1) / num_frames)
        if index < num_frames - 1:
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


def _center_lon_lat(da: xr.DataArray, projection: object) -> tuple[float, float]:
    x = np.asarray(da.coords["x"].values, dtype=float)
    y = np.asarray(da.coords["y"].values, dtype=float)
    transformer = Transformer.from_crs(
        CRS.from_user_input(projection), CRS.from_epsg(4326), always_xy=True
    )
    lon, lat = transformer.transform(float(x.mean()), float(y.mean()))
    return float(lon), float(lat)


def _prepare_frames(
    da: xr.DataArray, scl_band: str
) -> tuple[np.ndarray, list[list[float]], int, int]:
    """Return normalized `[T,6,H,W]`, temporal coords and the original H/W."""
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

    num_frames = _num_frames(times)
    height, width = reflectance.shape[1], reflectance.shape[2]
    frames = np.full((num_frames, height, width, len(BANDS)), np.nan, np.float32)
    dates: list[pd.Timestamp] = []
    selections = _bin_selections(times, num_frames)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        for index, (selection, midpoint) in enumerate(selections):
            frames[index] = np.nanmedian(reflectance[selection], axis=0)
            dates.append(midpoint)

    normalized = (frames - BAND_MEAN) / BAND_STD
    normalized = np.where(np.isfinite(normalized), normalized, NODATA_FLOAT)
    return (
        normalized.transpose(0, 3, 1, 2).astype(np.float32),
        _temporal_coords(dates),
        height,
        width,
    )


# =============================================================================
# Forward pass
# =============================================================================
def _encode_window(
    model,
    window_tchw: np.ndarray,
    temporal_coords: list[list[float]],
    lon: float,
    lat: float,
) -> np.ndarray:
    """Run the encoder on one `[T,6,WINDOW,WINDOW]` window."""
    num_frames, _, height, width = window_tchw.shape
    tensor = torch.from_numpy(window_tchw).permute(1, 0, 2, 3).unsqueeze(0)
    temporal = torch.tensor([temporal_coords], dtype=torch.float32)
    location = torch.tensor([[float(lat), float(lon)]], dtype=torch.float32)

    with torch.inference_mode():
        features = model.forward_features(
            tensor, temporal_coords=temporal, location_coords=location
        )
        tokens = features[-1] if isinstance(features, (list, tuple)) else features

    tokens = tokens[0]
    patch_h = height // PATCH
    patch_w = width // PATCH
    expected = num_frames * patch_h * patch_w
    if tokens.shape[0] == expected + 1:
        tokens = tokens[1:]  # drop CLS
    elif tokens.shape[0] != expected:
        raise ValueError(
            f"Unexpected Prithvi token count {tokens.shape[0]}; expected "
            f"{expected} (+1 for CLS)."
        )

    grid = (
        tokens.reshape(num_frames, patch_h, patch_w, EMBED_DIM)
        .mean(dim=0)
        .permute(2, 0, 1)
        .unsqueeze(0)
    )
    grid = F.interpolate(
        grid, size=(height, width), mode="bilinear", align_corners=False
    )
    return grid[0].cpu().numpy().astype(np.float32)


def _encode(
    model,
    x_tchw: np.ndarray,
    temporal_coords: list[list[float]],
    lon: float,
    lat: float,
    height: int,
    width: int,
) -> np.ndarray:
    """Return a `[768, H, W]` embedding, encoded in 224x224 windows."""
    canvas_height = int(math.ceil(height / WINDOW) * WINDOW)
    canvas_width = int(math.ceil(width / WINDOW) * WINDOW)
    canvas = np.full(
        (x_tchw.shape[0], x_tchw.shape[1], canvas_height, canvas_width),
        NODATA_FLOAT,
        np.float32,
    )
    canvas[:, :, :height, :width] = x_tchw
    embedding = np.empty((EMBED_DIM, canvas_height, canvas_width), np.float32)
    for y0 in range(0, canvas_height, WINDOW):
        for x0 in range(0, canvas_width, WINDOW):
            window = canvas[:, :, y0:y0 + WINDOW, x0:x0 + WINDOW]
            embedding[:, y0:y0 + WINDOW, x0:x0 + WINDOW] = _encode_window(
                model, window, temporal_coords, lon, lat
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
    weights_url = context.get("weights_url") or DEFAULT_WEIGHTS_URL
    projection = context.get("projection")
    if projection is None:
        raise ValueError("context['projection'] (EPSG code) is required.")

    x_tchw, temporal_coords, height, width = _prepare_frames(da, scl_band)
    lon, lat = _center_lon_lat(da, projection)
    model = _get_model(weights_url, int(x_tchw.shape[0]))
    embedding = _encode(model, x_tchw, temporal_coords, lon, lat, height, width)

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
# Copyright (c) IBM Corp. 2024. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# --------------------------------------------------------
# References:
# timm: https://github.com/rwightman/pytorch-image-models/tree/master/timm
# transformers: https://github.com/huggingface/transformers
# --------------------------------------------------------

import warnings
import logging
import numpy as np
import torch
import torch.nn as nn
from einops import rearrange
# to_2tuple and Block are defined in the adapter header above.

logger = logging.getLogger(__name__)


def get_3d_sincos_pos_embed(embed_dim, grid_size, add_cls_token=False):
    """
    Create 3D sin/cos positional embeddings.

    Args:
        embed_dim (int):
            Embedding dimension.
        grid_size (tuple[int, int, int] | list[int]):
            The grid depth, height and width.
        add_cls_token (bool, *optional*, defaults to False):
            Whether or not to add a classification (CLS) token.

    Returns:
        (`torch.FloatTensor` of shape (grid_size[0]*grid_size[1]*grid_size[2], embed_dim) or
        (1+grid_size[0]*grid_size[1]*grid_size[2], embed_dim): the position embeddings (with or without cls token)
    """

    assert embed_dim % 16 == 0

    t_size, h_size, w_size = grid_size

    w_embed_dim = embed_dim // 16 * 6
    h_embed_dim = embed_dim // 16 * 6
    t_embed_dim = embed_dim // 16 * 4

    w_pos_embed = get_1d_sincos_pos_embed_from_grid(w_embed_dim, np.arange(w_size))
    h_pos_embed = get_1d_sincos_pos_embed_from_grid(h_embed_dim, np.arange(h_size))
    t_pos_embed = get_1d_sincos_pos_embed_from_grid(t_embed_dim, np.arange(t_size))

    w_pos_embed = np.tile(w_pos_embed, (t_size * h_size, 1))
    h_pos_embed = np.tile(np.repeat(h_pos_embed, w_size, axis=0), (t_size, 1))
    t_pos_embed = np.repeat(t_pos_embed, h_size * w_size, axis=0)

    pos_embed = np.concatenate((w_pos_embed, h_pos_embed, t_pos_embed), axis=1)

    if add_cls_token:
        pos_embed = np.concatenate([np.zeros([1, embed_dim]), pos_embed], axis=0)
    return pos_embed


def get_1d_sincos_pos_embed_from_grid(embed_dim, pos):
    """
    embed_dim: output dimension for each position pos: a list of positions to be encoded: size (M,) out: (M, D)
    """
    if embed_dim % 2 != 0:
        raise ValueError("embed_dim must be even")

    omega = np.arange(embed_dim // 2, dtype=float)
    omega /= embed_dim / 2.0
    omega = 1.0 / 10000**omega  # (D/2,)

    pos = pos.reshape(-1)  # (M,)
    out = np.einsum("m,d->md", pos, omega)  # (M, D/2), outer product

    emb_sin = np.sin(out)  # (M, D/2)
    emb_cos = np.cos(out)  # (M, D/2)

    emb = np.concatenate([emb_sin, emb_cos], axis=1)  # (M, D)
    return emb


def _get_1d_sincos_embed_from_grid_torch(embed_dim: int, pos: torch.Tensor):
    """ Modified torch version of *get_1d_sincos_pos_embed_from_grid()*.

        embed_dim: output dimension for each position
        pos: a list of positions to be encoded: size (M,) - must be float dtype!
        out: (M, D)
    """
    assert embed_dim % 2 == 0
    assert pos.dtype in [torch.float32, torch.float16, torch.bfloat16]

    omega = torch.arange(embed_dim // 2, dtype=pos.dtype).to(pos.device)
    omega /= embed_dim / 2.0
    omega = 1.0 / 10000**omega  # (D/2,)

    pos = pos.reshape(-1)  # (M,)
    out = torch.einsum("m,d->md", pos, omega)  # (M, D/2), outer product

    emb_sin = torch.sin(out)  # (M, D/2)
    emb_cos = torch.cos(out)  # (M, D/2)

    emb = torch.cat([emb_sin, emb_cos], dim=1)  # (M, D)

    return emb


def _init_weights(module):
    """Initialize the weights"""
    if isinstance(module, nn.Linear):
        nn.init.xavier_uniform_(module.weight)
        if module.bias is not None:
            module.bias.data.zero_()
    elif isinstance(module, nn.LayerNorm):
        module.bias.data.zero_()
        module.weight.data.fill_(1.0)


def _interpolate_pos_encoding(
        pos_embed: torch.Tensor,
        grid_size: tuple[int, int, int] | list[int],
        patch_size: tuple[int, int, int] | list[int],
        shape: tuple[int, int, int],
        embed_dim: int,
):
    """
    Adapted from:
    - transformers.models.vit.modeling_vit.ViTEmbeddings.interpolate_pos_encoding,
    - https://github.com/facebookresearch/dino/blob/de9ee3df6cf39fac952ab558447af1fa1365362a/vision_transformer.py#L174-L194
    """
    t, h, w = shape
    t_patches = t // patch_size[0]
    h_patches = h // patch_size[1]
    w_patches = w // patch_size[2]

    if [t_patches, h_patches, w_patches] == grid_size:
        # No interpolation needed
        return pos_embed
    if t_patches != grid_size[0]:
        # Re-compute pos embedding to handle changed num_frames
        new_grid_size = (t_patches, *grid_size[1:])
        new_pos_embed = get_3d_sincos_pos_embed(pos_embed.shape[-1], new_grid_size, add_cls_token=True)
        new_pos_embed = torch.from_numpy(new_pos_embed).float().unsqueeze(0)
    else:
        new_grid_size = grid_size
        new_pos_embed = pos_embed

    class_pos_embed, patch_pos_embed = new_pos_embed[:, :1], new_pos_embed[:, 1:]

    patch_pos_embed = patch_pos_embed.reshape(*new_grid_size, embed_dim).permute(0, 3, 1, 2)

    patch_pos_embed = nn.functional.interpolate(
        patch_pos_embed,
        size=(h_patches, w_patches),
        mode='bicubic',
        align_corners=True,
    )
    patch_pos_embed = patch_pos_embed.permute(0, 2, 3, 1).view(1, -1, embed_dim)

    return torch.cat((class_pos_embed, patch_pos_embed), dim=1)


class PatchEmbed(nn.Module):
    """3D version of timm.models.vision_transformer.PatchEmbed"""
    def __init__(
            self,
            input_size: tuple[int, int, int] = (1, 224, 224),
            patch_size: tuple[int, int, int] = (1, 16, 16),
            in_chans: int = 3,
            embed_dim: int = 768,
            norm_layer: nn.Module | None = None,
            flatten: bool = True,
            bias: bool = True,
    ):
        super().__init__()
        self.input_size = input_size
        self.patch_size = patch_size
        self.grid_size = [s // p for s, p in zip(self.input_size, self.patch_size)]
        assert self.grid_size >= [1, 1, 1], "Patch size is bigger than input size."
        self.num_patches = self.grid_size[0] * self.grid_size[1] * self.grid_size[2]
        self.flatten = flatten

        self.proj = nn.Conv3d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size, bias=bias)
        self.norm = norm_layer(embed_dim) if norm_layer else nn.Identity()

    def forward(self, x):
        B, C, T, H, W = x.shape

        if T / self.patch_size[0] % 1 or H / self.patch_size[1] % 1 or W / self.patch_size[2] % 1:
            warnings.warn(f"Input {x.shape[-3:]} is not divisible by patch size {self.patch_size}."
                          f"The border will be ignored, add backbone_padding for pixel-wise tasks.")

        x = self.proj(x)
        if self.flatten:
            x = x.flatten(2).transpose(1, 2)  # B,C,T,H,W -> B,C,L -> B,L,C
        x = self.norm(x)
        return x


class TemporalEncoder(nn.Module):
    def __init__(self, embed_dim: int, trainable_scale: bool = False):
        super().__init__()
        self.embed_dim = embed_dim
        self.year_embed_dim = embed_dim // 2
        self.julian_day_embed_dim = embed_dim - self.year_embed_dim

        # If trainable, initialize scale with small number
        if trainable_scale:
            self.scale = nn.Parameter(torch.full((1,), 0.1))
        else:
            self.register_buffer('scale', torch.ones(1))

    def forward(self, temporal_coords: torch.Tensor, tokens_per_frame: int | None = None):
        """
        temporal_coords: year and day-of-year info with shape (B, T, 2).
        tokens_per_frame: number of tokens for each frame in the sample. If provided, embeddings will be
            repeated over T dimension, and final shape is (B, T*tokens_per_frame, embed_dim).
        """
        shape = temporal_coords.shape[:2] + (-1,)  # B, T, -1

        year = _get_1d_sincos_embed_from_grid_torch(
            self.year_embed_dim, temporal_coords[:, :, 0].flatten()).reshape(shape)
        julian_day = _get_1d_sincos_embed_from_grid_torch(
            self.julian_day_embed_dim, temporal_coords[:, :, 1].flatten()).reshape(shape)

        embedding = self.scale * torch.cat([year, julian_day], dim=-1)

        if tokens_per_frame is not None:
            embedding = torch.repeat_interleave(embedding, tokens_per_frame, dim=1)

        return embedding  # B, T*tokens_per_frame, embed_dim


class LocationEncoder(nn.Module):
    def __init__(self, embed_dim: int, trainable_scale: bool = False):
        super().__init__()
        self.embed_dim = embed_dim
        self.lat_embed_dim = embed_dim // 2
        self.lon_embed_dim = embed_dim - self.lat_embed_dim

        # If trainable, initialize scale with small number
        if trainable_scale:
            self.scale = nn.Parameter(torch.full((1,), 0.1))
        else:
            self.register_buffer('scale', torch.ones(1))

    def forward(self, location_coords: torch.Tensor):
        """
        location_coords: lat and lon info with shape (B, 2).
        """
        shape = location_coords.shape[:1] + (1, -1)  # B, 1, -1

        lat = _get_1d_sincos_embed_from_grid_torch(
                self.lat_embed_dim, location_coords[:, 0].flatten()).reshape(shape)
        lon = _get_1d_sincos_embed_from_grid_torch(
                self.lon_embed_dim, location_coords[:, 1].flatten()).reshape(shape)

        embedding = self.scale * torch.cat([lat, lon], dim=-1)

        return embedding  # B, 1, embed_dim


class PrithviViT(nn.Module):
    """ Prithvi ViT Encoder"""
    def __init__(self,
                 img_size: int | tuple[int, int] = 224,
                 patch_size: int | tuple[int, int, int] = (1, 16, 16),
                 num_frames: int = 1,
                 in_chans: int = 3,
                 embed_dim: int = 1024,
                 depth: int = 24,
                 num_heads: int = 16,
                 mlp_ratio: float = 4.,
                 norm_layer: nn.Module = nn.LayerNorm,
                 coords_encoding: list[str] | None = None,
                 coords_scale_learn: bool = False,
                 drop_path: float = 0.,
                 ** kwargs,
                ):
        super().__init__()

        self.in_chans = in_chans
        self.num_frames = num_frames
        self.embed_dim = embed_dim
        self.img_size = to_2tuple(img_size)
        if isinstance(patch_size, int):
            patch_size = (1, patch_size, patch_size)

        # 3D patch embedding
        self.patch_embed = PatchEmbed(
            input_size=(num_frames,) + self.img_size,
            patch_size=patch_size,
            in_chans=in_chans,
            embed_dim=embed_dim,
        )
        self.out_channels = [embed_dim * self.patch_embed.grid_size[0]] * depth

        # Optional temporal and location embedding
        coords_encoding = coords_encoding or []
        self.temporal_encoding = 'time' in coords_encoding
        self.location_encoding = 'location' in coords_encoding
        if self.temporal_encoding:
            assert patch_size[0] == 1, f"With temporal encoding, patch_size[0] must be 1, received {patch_size[0]}"
            self.temporal_embed_enc = TemporalEncoder(embed_dim, coords_scale_learn)
        if self.location_encoding:
            self.location_embed_enc = LocationEncoder(embed_dim, coords_scale_learn)

        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.register_buffer("pos_embed", torch.zeros(1, self.patch_embed.num_patches + 1, embed_dim))

        # Transformer layers
        self.blocks = []
        for i in range(depth):
            self.blocks.append(Block(embed_dim, num_heads, mlp_ratio, qkv_bias=True, norm_layer=norm_layer,
                                     drop_path=drop_path,))
        self.blocks = nn.ModuleList(self.blocks)

        self.norm = norm_layer(embed_dim)

        self.initialize_weights()

    def initialize_weights(self):
        # initialize (and freeze) position embeddings by sin-cos embedding
        pos_embed = get_3d_sincos_pos_embed(
            self.pos_embed.shape[-1], self.patch_embed.grid_size, add_cls_token=True
        )
        self.pos_embed.data.copy_(torch.from_numpy(pos_embed).float().unsqueeze(0))

        # initialize patch_embeddings like nn.Linear (instead of nn.Conv2d)
        w = self.patch_embed.proj.weight.data
        torch.nn.init.xavier_uniform_(w.view([w.shape[0], -1]))

        # timm's trunc_normal_(std=.02) is effectively normal_(std=0.02) as cutoff is too big (2.)
        torch.nn.init.normal_(self.cls_token, std=0.02)
        self.apply(_init_weights)

    def random_masking(self, sequence, mask_ratio, noise=None):
        """
        Perform per-sample random masking by per-sample shuffling. Per-sample shuffling is done by argsort random
        noise.

        Args:
            sequence (`torch.FloatTensor` of shape `(batch_size, sequence_length, dim)`)
            mask_ratio (float): mask ratio to use.
            noise (`torch.FloatTensor` of shape `(batch_size, sequence_length)`, *optional*) which is
                mainly used for testing purposes to control randomness and maintain the reproducibility
        """
        batch_size, seq_length, dim = sequence.shape
        len_keep = int(seq_length * (1 - mask_ratio))

        if noise is None:
            noise = torch.rand(batch_size, seq_length, device=sequence.device)  # noise in [0, 1]

        # sort noise for each sample
        ids_shuffle = torch.argsort(noise, dim=1).to(sequence.device)  # ascend: small is keep, large is remove
        ids_restore = torch.argsort(ids_shuffle, dim=1).to(sequence.device)

        # keep the first subset
        ids_keep = ids_shuffle[:, :len_keep]
        sequence_unmasked = torch.gather(sequence, dim=1, index=ids_keep.unsqueeze(-1).repeat(1, 1, dim))

        # generate the binary mask: 0 is keep, 1 is remove
        mask = torch.ones([batch_size, seq_length], device=sequence.device)
        mask[:, :len_keep] = 0
        # unshuffle to get the binary mask
        mask = torch.gather(mask, dim=1, index=ids_restore)

        return sequence_unmasked, mask, ids_restore

    def interpolate_pos_encoding(self, sample_shape: tuple[int, int, int]):

        pos_embed = _interpolate_pos_encoding(
            pos_embed=self.pos_embed,
            grid_size=self.patch_embed.grid_size,
            patch_size=self.patch_embed.patch_size,
            shape=sample_shape,
            embed_dim=self.embed_dim,
        )
        return pos_embed

    def forward(
        self, x: torch.Tensor,
        temporal_coords: None | torch.Tensor = None,
        location_coords: None | torch.Tensor = None,
        mask_ratio=0.75
    ):
        if len(x.shape) == 4 and self.patch_embed.input_size[0] == 1:
            # add time dim
            x = x.unsqueeze(2)
        sample_shape = x.shape[-3:]

        # embed patches
        x = self.patch_embed(x)

        pos_embed = self.interpolate_pos_encoding(sample_shape)
        # add pos embed w/o cls token
        x = x + pos_embed[:, 1:, :]

        if self.temporal_encoding and temporal_coords is not None:
            num_tokens_per_frame = x.shape[1] // self.num_frames
            temporal_encoding = self.temporal_embed_enc(temporal_coords, num_tokens_per_frame)
            x = x + temporal_encoding
        if self.location_encoding and location_coords is not None:
            location_encoding = self.location_embed_enc(location_coords)
            x = x + location_encoding

        # masking: length -> length * mask_ratio
        x, mask, ids_restore = self.random_masking(x, mask_ratio)

        # append cls token
        cls_token = self.cls_token + pos_embed[:, :1, :]
        cls_tokens = cls_token.expand(x.shape[0], -1, -1)
        x = torch.cat((cls_tokens, x), dim=1)

        # apply Transformer blocks
        for block in self.blocks:
            x = block(x)
        x = self.norm(x)

        return x, mask, ids_restore

    def forward_features(
        self,
        x: torch.Tensor,
        temporal_coords: None | torch.Tensor = None,
        location_coords: None | torch.Tensor = None,
    ) -> list[torch.Tensor]:
        if len(x.shape) == 4 and self.patch_embed.input_size[0] == 1:
            # add time dim
            x = x.unsqueeze(2)
        sample_shape = x.shape[-3:]

        # embed patches
        x = self.patch_embed(x)

        pos_embed = self.interpolate_pos_encoding(sample_shape)
        # add pos embed w/o cls token
        x = x + pos_embed[:, 1:, :]

        if self.temporal_encoding and temporal_coords is not None:
            num_tokens_per_frame = x.shape[1] // self.num_frames
            temporal_encoding = self.temporal_embed_enc(temporal_coords, num_tokens_per_frame)
            x = x + temporal_encoding
        if self.location_encoding and location_coords is not None:
            location_encoding = self.location_embed_enc(location_coords)
            x = x + location_encoding

        # append cls token
        cls_token = self.cls_token + pos_embed[:, :1, :]
        cls_tokens = cls_token.expand(x.shape[0], -1, -1)
        x = torch.cat((cls_tokens, x), dim=1)

        # apply Transformer blocks
        out = []
        for block in self.blocks:
            x = block(x)
            out.append(x.clone())

        x = self.norm(x)
        out[-1] = x
        return out

    def prepare_features_for_image_model(self, features: list[torch.Tensor]) -> list[torch.Tensor]:
        out = []
        effective_time_dim = self.patch_embed.input_size[0] // self.patch_embed.patch_size[0]
        for x in features:
            x_no_token = x[:, 1:, :]
            number_of_tokens = x_no_token.shape[1]
            tokens_per_timestep = number_of_tokens // effective_time_dim
            h = int(np.sqrt(tokens_per_timestep))
            encoded = rearrange(
                x_no_token,
                "batch (t h w) e -> batch (t e) h w",
                e=self.embed_dim,
                t=effective_time_dim,
                h=h,
            )
            out.append(encoded)
        return out


class MAEDecoder(nn.Module):
    """ Transformer Decoder used in the Prithvi MAE"""
    def __init__(self,
                 patch_size: int | tuple[int, int, int] = (1, 16, 16),
                 grid_size: list[int] | tuple[int, int, int] = (3, 14, 14),
                 in_chans: int = 3,
                 encoder_embed_dim: int = 1024,
                 decoder_embed_dim: int = 512,
                 depth: int = 8,
                 num_heads: int = 16,
                 mlp_ratio: float = 4.,
                 norm_layer: nn.Module = nn.LayerNorm,
                 coords_encoding: list[str] | None = None,
                 coords_scale_learn: bool = False,
                 ):
        super().__init__()

        self.decoder_embed = nn.Linear(encoder_embed_dim, decoder_embed_dim, bias=True)
        self.decoder_embed_dim = decoder_embed_dim
        self.grid_size = grid_size
        if isinstance(patch_size, int):
            patch_size = (1, patch_size, patch_size)
        self.patch_size = patch_size
        self.num_frames = self.grid_size[0] * patch_size[0]
        num_patches = self.grid_size[0] * self.grid_size[1] * self.grid_size[2]

        # Optional temporal and location embedding
        coords_encoding = coords_encoding or []
        self.temporal_encoding = 'time' in coords_encoding
        self.location_encoding = 'location' in coords_encoding
        if self.temporal_encoding:
            self.temporal_embed_dec = TemporalEncoder(decoder_embed_dim, coords_scale_learn)
        if self.location_encoding:
            self.location_embed_dec = LocationEncoder(decoder_embed_dim, coords_scale_learn)

        self.mask_token = nn.Parameter(torch.zeros(1, 1, decoder_embed_dim))

        self.register_buffer("decoder_pos_embed", torch.zeros(1, num_patches + 1, decoder_embed_dim))

        self.decoder_blocks = nn.ModuleList(
            [Block(decoder_embed_dim, num_heads, mlp_ratio, qkv_bias=True, norm_layer=norm_layer) for _ in range(depth)]
        )

        self.decoder_norm = norm_layer(decoder_embed_dim)
        self.decoder_pred = nn.Linear(decoder_embed_dim,
                                      patch_size[0] * patch_size[1] * patch_size[2] * in_chans,
                                      bias=True)

        self.initialize_weights()

    def initialize_weights(self):
        # initialize (and freeze) position embeddings by sin-cos embedding
        decoder_pos_embed = get_3d_sincos_pos_embed(
            self.decoder_pos_embed.shape[-1], self.grid_size, add_cls_token=True
        )
        self.decoder_pos_embed.data.copy_(torch.from_numpy(decoder_pos_embed).float().unsqueeze(0))

        # timm's trunc_normal_(std=.02) is effectively normal_(std=0.02) as cutoff is too big (2.)
        torch.nn.init.normal_(self.mask_token, std=0.02)
        self.apply(_init_weights)

    def interpolate_pos_encoding(self, sample_shape: tuple[int, int, int]):

        pos_embed = _interpolate_pos_encoding(
            pos_embed=self.decoder_pos_embed,
            grid_size=self.grid_size,
            patch_size=self.patch_size,
            shape=sample_shape,
            embed_dim=self.decoder_embed_dim,
        )

        return pos_embed

    def forward(
        self,
        hidden_states: torch.Tensor,
        ids_restore: torch.Tensor,
        temporal_coords: None | torch.Tensor = None,
        location_coords: None | torch.Tensor = None,
        input_size: list[int] = None,
    ):
        # embed tokens
        x = self.decoder_embed(hidden_states)
        cls_token = x[:, :1, :]

        # append mask tokens to sequence
        mask_tokens = self.mask_token.repeat(x.shape[0], ids_restore.shape[1] + 1 - x.shape[1], 1)
        x = torch.cat([x[:, 1:, :], mask_tokens], dim=1)  # no cls token
        # unshuffle
        x = torch.gather(x, dim=1, index=ids_restore.unsqueeze(-1).repeat(1, 1, x.shape[2]).to(x.device))

        # add pos embed
        decoder_pos_embed = self.interpolate_pos_encoding(input_size[-3:])
        cls_token = cls_token + decoder_pos_embed[:, :1, :]
        x = x + decoder_pos_embed[:, 1:, :]

        if self.temporal_encoding and temporal_coords is not None:
            num_tokens_per_frame = x.shape[1] // self.num_frames
            temporal_encoding = self.temporal_embed_dec(temporal_coords, num_tokens_per_frame)
            # Add temporal encoding w/o cls token
            x = x + temporal_encoding
        if self.location_encoding and location_coords is not None:
            location_encoding = self.location_embed_dec(location_coords)
            # Add location encoding w/o cls token
            x = x + location_encoding

        # append cls token
        x = torch.cat([cls_token, x], dim=1)

        # apply Transformer layers (blocks)
        for block in self.decoder_blocks:
            x = block(x)
        x = self.decoder_norm(x)

        # predictor projection
        pred = self.decoder_pred(x)

        # remove cls token
        pred = pred[:, 1:, :]

        return pred


class PrithviMAE(nn.Module):
    """ Prithvi Masked Autoencoder"""

    def __init__(self,
                 img_size: int | tuple[int, int] = 224,
                 patch_size: int | tuple[int, int, int] = (1, 16, 16),
                 num_frames: int = 4,
                 in_chans: int = 6,
                 embed_dim: int = 768,
                 depth: int = 12,
                 num_heads: int = 12,
                 decoder_embed_dim: int = 512,
                 decoder_depth: int = 8,
                 decoder_num_heads: int = 16,
                 mlp_ratio: float = 4.,
                 norm_layer: nn.Module = nn.LayerNorm,
                 norm_pix_loss: bool = False,
                 coords_encoding: list[str] | None = None,
                 coords_scale_learn: bool = False,
                 drop_path: float = 0.,
                 mask_ratio: float = 0.75,
                 **kwargs,
                 ):
        super().__init__()

        self.encoder = PrithviViT(
            img_size=img_size,
            num_frames=num_frames,
            patch_size=patch_size,
            in_chans=in_chans,
            embed_dim=embed_dim,
            depth=depth,
            num_heads=num_heads,
            mlp_ratio=mlp_ratio,
            norm_layer=norm_layer,
            coords_encoding=coords_encoding,
            coords_scale_learn=coords_scale_learn,
            drop_path=drop_path,
        )

        self.decoder = MAEDecoder(
            patch_size=patch_size,
            grid_size=self.encoder.patch_embed.grid_size,
            in_chans=in_chans,
            encoder_embed_dim=embed_dim,
            decoder_embed_dim=decoder_embed_dim,
            depth=decoder_depth,
            num_heads=decoder_num_heads,
            mlp_ratio=mlp_ratio,
            norm_layer=norm_layer,
            coords_encoding=coords_encoding,
            coords_scale_learn=coords_scale_learn,
        )

        self.mask_ratio = mask_ratio
        self.norm_pix_loss = norm_pix_loss
        self.out_channels = self.encoder.out_channels

    def patchify(self, pixel_values):
        """
        Args:
            pixel_values (torch.FloatTensor of shape `(batch_size, num_channels, time, height, width)`):
                Pixel values.

        Returns:
            torch.FloatTensor of shape
                `(batch_size, num_patches, patch_size[0]*patch_size[1]*patch_size[2] * num_channels)`:
                Patchified pixel values.
        """
        patch_size_t, patch_size_h, patch_size_w = self.encoder.patch_embed.patch_size
        num_channels = self.encoder.in_chans

        # patchify
        patchified_pixel_values = rearrange(pixel_values, 'b c (t s) (h p) (w q) -> b (t h w) (s p q c)',
                                            c=num_channels, s=patch_size_t, p=patch_size_h, q=patch_size_w)

        return patchified_pixel_values

    def unpatchify(self, patchified_pixel_values, image_size: tuple[int, int] | None = None):
        """
        Args:
            patchified_pixel_values (`torch.FloatTensor` of shape
                `(batch_size, num_patches, patch_size[0]*patch_size[1]*patch_size[2] * num_channels))`:
                Patchified pixel values.
            image_size (`tuple[int, int]`, *optional*):
                Original image size.

        Returns:
            `torch.FloatTensor` of shape `(batch_size, num_channels, height, width)`:
                Pixel values.
        """
        patch_size_t, patch_size_h, patch_size_w = self.encoder.patch_embed.patch_size
        image_size = to_2tuple(image_size) if image_size is not None else self.encoder.img_size
        original_height, original_width = image_size
        num_patches_h = original_height // patch_size_h
        num_patches_w = original_width // patch_size_w
        num_channels = self.encoder.in_chans

        pixel_values = rearrange(patchified_pixel_values, 'b (t h w) (s p q c) -> b c (t s) (h p) (w q)',
                                 c=num_channels, h=num_patches_h, w=num_patches_w,
                                 s=patch_size_t, p=patch_size_h, q=patch_size_w)
        return pixel_values

    def forward_loss(self, pixel_values, pred, mask):
        """
        Args:
            pixel_values (`torch.FloatTensor` of shape `(batch_size, num_channels, time, height, width)`):
                Pixel values.
            pred (`torch.FloatTensor` of shape
                `(batch_size, num_patches, patch_size[0]*patch_size[1]*patch_size[2] * num_channels)`:
                Predicted pixel values.
            mask (`torch.FloatTensor` of shape `(batch_size, sequence_length)`):
                Tensor indicating which patches are masked (1) and which are not (0).

        Returns:
            `torch.FloatTensor`: Pixel reconstruction loss.
        """
        target = self.patchify(pixel_values)
        if self.norm_pix_loss:
            mean = target.mean(dim=-1, keepdim=True)
            var = target.var(dim=-1, keepdim=True)
            target = (target - mean) / (var + 1.0e-6) ** 0.5

        loss = (pred - target) ** 2
        loss = loss.mean(dim=-1)  # [N, L], mean loss per patch
        loss = (loss * mask).sum() / mask.sum()  # mean loss on removed patches
        return loss

    def forward(
        self,
        pixel_values: torch.Tensor,
        temporal_coords: None | torch.Tensor = None,
        location_coords: None | torch.Tensor = None,
        mask_ratio: float = None,
    ):
        if len(pixel_values.shape) == 4 and self.encoder.patch_embed.input_size[0] == 1:
            # add time dim
            pixel_values = pixel_values.unsqueeze(2)

        mask_ratio = mask_ratio or self.mask_ratio
        latent, mask, ids_restore = self.encoder(pixel_values, temporal_coords, location_coords, mask_ratio)
        pred = self.decoder(latent, ids_restore, temporal_coords, location_coords, input_size=pixel_values.shape)
        loss = self.forward_loss(pixel_values, pred, mask)
        return loss, pred, mask

    def forward_features(
        self,
        x: torch.Tensor,
        temporal_coords: None | torch.Tensor = None,
        location_coords: None | torch.Tensor = None,
    ) -> list[torch.Tensor]:
        return self.encoder.forward_features(x, temporal_coords, location_coords)
