"""Vendored TESSERA v2 pixel student (ucam-eo/tessera).

Copied from `tessera_infer_v2/student/model.py` in
https://github.com/ucam-eo/tessera, including the `load_model` helper.
The openEO UDF no longer imports this module: it is the source the ONNX
export script uses, and the exported graph is what the UDF runs.

Do not edit the model math here without re-exporting and re-running the
parity check in scripts/export-tessera-onnx.py.
"""
import math
from typing import Tuple

import numpy as np
import torch
import torch.nn as nn

# =============================================================================
# Vendored from tessera_infer_v2/student/model.py
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

class TemporalPositionalEncoder(nn.Module):
    """Sinusoidal positional encoding using the (raw integer) DOY value."""

    def __init__(self, d_model: int) -> None:
        super().__init__()
        self.d_model = d_model

    def forward(self, doy: torch.Tensor) -> torch.Tensor:
        position = doy.unsqueeze(-1).float()
        div_term = torch.exp(
            torch.arange(0, self.d_model, 2, dtype=torch.float, device=doy.device)
            * -(math.log(10000.0) / self.d_model)
        )
        pe = torch.zeros(doy.shape[0], doy.shape[1], self.d_model, device=doy.device)
        pe[:, :, 0::2] = torch.sin(position * div_term)
        pe[:, :, 1::2] = torch.cos(position * div_term)
        return pe


class AttentionPooling(nn.Module):
    """Plain single-head softmax attention pool over T."""

    def __init__(self, input_dim: int) -> None:
        super().__init__()
        self.query = nn.Linear(input_dim, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, T, D = x.shape
        if T == 0:
            return torch.zeros(B, D, device=x.device, dtype=x.dtype)
        if T == 1:
            return x.squeeze(1)
        w = torch.softmax(self.query(x), dim=1)
        return (w * x).sum(dim=1)


class TransformerEncoder(nn.Module):
    """Per-pixel band embedding + DOY positional + Transformer + attention pooling."""

    def __init__(self, band_num: int, latent_dim: int, nhead: int = 4,
                 num_encoder_layers: int = 3, dim_feedforward: int = 1024,
                 dropout: float = 0.1) -> None:
        super().__init__()
        input_dim = band_num
        self.embedding = nn.Sequential(
            nn.Linear(input_dim, latent_dim * 4),
            nn.ReLU(),
            nn.Linear(latent_dim * 4, latent_dim * 4),
        )
        self.temporal_encoder = TemporalPositionalEncoder(d_model=latent_dim * 4)

        encoder_layer = nn.TransformerEncoderLayer(
            d_model=latent_dim * 4, nhead=nhead,
            dim_feedforward=dim_feedforward, dropout=dropout,
            activation="relu", batch_first=True,
        )
        self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers=num_encoder_layers)
        self.attn_pool = AttentionPooling(latent_dim * 4)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, T, band_num + 1) - last channel is the (raw integer) DOY value
        bands = x[:, :, :-1]
        doy = x[:, :, -1]
        bands_embedded = self.embedding(bands)
        temporal_encoding = self.temporal_encoder(doy)
        x = bands_embedded + temporal_encoding
        x = self.transformer_encoder(x)
        return self.attn_pool(x)


class PixelStudent(nn.Module):
    """2-backbone pixel encoder. emb_dim = 128 (Matryoshka-ordered).

    Inputs (per pixel):
        s2_x : (B, T_s2, 11)   10 bands + 1 DOY (raw integer 1..365)
        s1_x : (B, T_s1,  3)    2 bands + 1 DOY (raw integer; s1a + s1d MERGED)

    encode() returns (B, 128). The first K dims are usable on their own for
    K in {16, 32, 64, 128}.
    """

    def __init__(
        self,
        repr_dim: int = 128,
        latent_dim: int = 64,
        num_layers: int = 4,
        nhead: int = 4,
        dim_feedforward: int = 1024,
        dropout: float = 0.0,
        max_seq_len: int = 256,
        matryoshka_dims: Tuple[int, ...] = (16, 32, 64, 128),
    ) -> None:
        super().__init__()
        self.repr_dim = int(repr_dim)
        self.matryoshka_dims = tuple(int(d) for d in matryoshka_dims)
        for d in self.matryoshka_dims:
            if d > repr_dim:
                raise ValueError(f"matryoshka cut {d} > repr_dim {repr_dim}")

        def make_enc(band_num: int) -> TransformerEncoder:
            return TransformerEncoder(
                band_num=band_num, latent_dim=latent_dim,
                nhead=nhead, num_encoder_layers=num_layers,
                dim_feedforward=dim_feedforward, dropout=dropout,
            )

        self.s2_backbone = make_enc(10)
        self.s1_backbone = make_enc(2)

        backbone_out = latent_dim * 4
        fused_in = 2 * backbone_out
        self.dim_reducer = nn.Sequential(
            nn.Linear(fused_in, fused_in * 2),
            nn.LayerNorm(fused_in * 2),
            nn.ReLU(inplace=False),
            nn.Dropout(0.2),
            nn.Linear(fused_in * 2, repr_dim),
            # Final non-affine LayerNorm: normalizes the 128-d output to
            # per-pixel mean 0 / std 1. No learnable params.
            nn.LayerNorm(repr_dim, elementwise_affine=False),
        )

    def encode(self, s2_x: torch.Tensor, s1_x: torch.Tensor) -> torch.Tensor:
        s2 = self.s2_backbone(s2_x)
        s1 = self.s1_backbone(s1_x)
        fused = torch.cat([s2, s1], dim=-1)
        return self.dim_reducer(fused)

    def forward(self, s2_x: torch.Tensor, s1_x: torch.Tensor) -> torch.Tensor:
        return self.encode(s2_x, s1_x)


def load_model(ckpt_source, device: torch.device = torch.device("cpu")):
    """Load a pretrained pixel student (encoder) from a .pt file path or file-like object.

    Uses ``weights_only=True`` so a malicious checkpoint cannot execute arbitrary
    code via pickle. The TESSERA payload is ``{"args": {...}, "model": state_dict}``
    which contains only safe types.
    """
    payload = torch.load(ckpt_source, map_location=device, weights_only=True)
    cfg = payload.get("args", {}) or {}
    matryoshka_dims = tuple(
        int(d) for d in str(cfg.get("matryoshka_dims", "16,32,64,128")).split(",")
    )
    model = PixelStudent(
        repr_dim=int(cfg.get("repr_dim", 128)),
        latent_dim=int(cfg.get("latent_dim", 64)),
        num_layers=int(cfg.get("num_layers", 4)),
        nhead=int(cfg.get("nhead", 4)),
        dim_feedforward=int(cfg.get("dim_feedforward", 1024)),
        dropout=0.0,
        matryoshka_dims=matryoshka_dims,
    ).to(device)
    model.load_state_dict(payload["model"])
    model.eval()
    return model

