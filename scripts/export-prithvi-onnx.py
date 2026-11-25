#!/usr/bin/env python3
"""Export the Prithvi-EO-2.0 100M-TL encoder to ONNX for the openEO UDF.

Run once, offline, with pinned torch (see ``build.yml`` for the CI invocation):

    uv venv .venv-export
    uv pip install --python .venv-export/bin/python \\
        --default-index https://download.pytorch.org/whl/cpu "torch==2.7.1+cpu"
    uv pip install --python .venv-export/bin/python \\
        "onnx==1.17.0" "onnxruntime==1.20.1" "einops==0.8.2" "huggingface_hub==0.34.4"
    .venv-export/bin/python scripts/export-prithvi-onnx.py \\
        --hf-revision 2c84e383194986040f883cc43d7869002c425e1b --out weights

The exported graph takes three inputs:

    frames    (B, 4, 6, 224, 224)  raw HLS-like reflectance, NaN for nodata
    temporal  (B, 4, 2)            per-frame (year, day-of-year)
    location  (B, 2)               tile center (latitude, longitude)

and returns the mean-over-time patch tokens ``(B, 196, 768)``. The checkpoint's
per-band mean/std are baked into the graph as constants and the nodata value
is applied after normalization, so the UDF hands raw frames (with NaN for
masked pixels) straight to ONNX Runtime.

Prithvi's sinusoidal position embedding depends on the number of frames. The
released checkpoint is trained on four timesteps, so the graph is exported for
exactly ``NUM_FRAMES`` frames and the UDF always bins its time series into four
frames. Do not make the time dimension dynamic without reworking that
embedding.

The UDF upsamples the 14x14 token grid back to 224x224 pixels in NumPy; the
graph stops at the token grid so its output stays small. The script writes
``<model>.json`` next to the graph with the embedding width, token count, band
statistics and file sha256. Use those values for ``config.json`` and ``udf.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import torch
from huggingface_hub import hf_hub_download
from torch import nn

sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
from prithvi_mae import PrithviMAE  # noqa: E402

MODEL = "prithvi_eo_v2"
HF_REPO = "ibm-nasa-geospatial/Prithvi-EO-2.0-100M-TL"
HF_FILENAME = "Prithvi_EO_V2_100M_TL.pt"

# HLS band order and the statistics shipped with the 100M-TL checkpoint.
BANDS = ("B02", "B03", "B04", "B8A", "B11", "B12")
BAND_MEAN = (1087.0, 1342.0, 1433.0, 2734.0, 1958.0, 1363.0)
BAND_STD = (2248.0, 2179.0, 2178.0, 1850.0, 1242.0, 1049.0)

PATCH = 16
WINDOW = 224
NUM_FRAMES = 4
EMBED_DIM = 768
NODATA_FLOAT = 0.0001
OPSET = 17
TOKENS_PER_SIDE = WINDOW // PATCH
TOKEN_COUNT = TOKENS_PER_SIDE * TOKENS_PER_SIDE

MODEL_CONFIG = {
    "img_size": WINDOW,
    "num_frames": NUM_FRAMES,
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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_versions(torch_version: str) -> None:
    installed = torch.__version__.split("+", 1)[0]
    if installed != torch_version:
        raise SystemExit(
            f"installed torch {installed} != --torch-version {torch_version}; "
            "the export must run on the pinned version"
        )


class PrithviEncoderONNX(nn.Module):
    """Standardize raw frames, then return mean-over-time patch tokens.

    The checkpoint is trained on four timesteps, so the graph is static in time,
    space and embedding width; only the batch dimension is dynamic. Prithvi's
    position embedding is recomputed for a non-matching frame count, which a
    tracer cannot capture, hence the fixed ``NUM_FRAMES``.
    """

    def __init__(self, backbone: nn.Module) -> None:
        super().__init__()
        self.backbone = backbone
        mean = torch.tensor(BAND_MEAN, dtype=torch.float32).view(1, 1, -1, 1, 1)
        std = torch.tensor(BAND_STD, dtype=torch.float32).view(1, 1, -1, 1, 1)
        self.register_buffer("band_mean", mean)
        self.register_buffer("band_std", std)

    def forward(
        self,
        frames: torch.Tensor,
        temporal: torch.Tensor,
        location: torch.Tensor,
    ) -> torch.Tensor:
        # frames: (B, T, C, H, W) raw reflectance with NaN for masked pixels.
        normalized = (frames - self.band_mean) / (self.band_std + 1e-9)
        normalized = torch.where(
            torch.isfinite(normalized),
            normalized,
            torch.full_like(normalized, NODATA_FLOAT),
        )
        x = normalized.permute(0, 2, 1, 3, 4)  # (B, C, T, H, W)
        features = self.backbone.forward_features(
            x, temporal_coords=temporal, location_coords=location
        )
        tokens = features[-1] if isinstance(features, (list, tuple)) else features
        tokens = tokens[:, 1:, :]  # drop CLS
        batch = tokens.shape[0]
        tokens = tokens.reshape(batch, NUM_FRAMES, TOKEN_COUNT, EMBED_DIM)
        return tokens.mean(dim=1)


def inspect_graph(path: Path) -> dict:
    import onnx

    graph = onnx.load(path.as_posix(), load_external_data=False).graph

    def shape(value):
        return [
            dim.dim_value if dim.HasField("dim_value") else dim.dim_param
            for dim in value.type.tensor_type.shape.dim
        ]

    return {
        "inputs": {value.name: shape(value) for value in graph.input},
        "outputs": {value.name: shape(value) for value in graph.output},
    }


def load_backbone(weights_path: Path) -> nn.Module:
    model = PrithviMAE(**MODEL_CONFIG)
    state = torch.load(weights_path, map_location="cpu", weights_only=True)
    if not isinstance(state, dict):
        raise SystemExit(f"unexpected checkpoint object: {type(state).__name__}")
    # The released checkpoint fixes the position embeddings; the class builds
    # the sinusoidal ones for the runtime shape.
    if "encoder.pos_embed" in state:
        state["encoder.pos_embed"] = model.encoder.pos_embed
    if "decoder.decoder_pos_embed" in state:
        state["decoder.decoder_pos_embed"] = model.decoder.decoder_pos_embed
    model.load_state_dict(state, strict=True)
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return model


def parity_check(wrapper: nn.Module, onnx_path: Path) -> None:
    import onnxruntime as ort

    generator = torch.Generator().manual_seed(7)
    frames = torch.rand((2, NUM_FRAMES, len(BANDS), WINDOW, WINDOW), generator=generator)
    frames = frames * 4000.0
    frames[0, 0, 0, :8, :8] = float("nan")
    temporal = torch.tensor(
        [[[2024.0, 15.0]] * NUM_FRAMES, [[2024.0, 180.0]] * NUM_FRAMES]
    )
    location = torch.tensor([[50.92, 11.60], [50.90, 11.62]])
    with torch.inference_mode():
        expected = wrapper(frames, temporal, location).numpy()
    session = ort.InferenceSession(
        onnx_path.as_posix(), providers=["CPUExecutionProvider"]
    )
    actual = session.run(
        None,
        {
            "frames": frames.numpy(),
            "temporal": temporal.numpy(),
            "location": location.numpy(),
        },
    )[0]
    np.testing.assert_allclose(actual, expected, rtol=1e-4, atol=1e-4)
    print(
        "parity: max abs diff "
        f"{np.max(np.abs(actual - expected)):.3e} over {expected.shape}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--hf-revision",
        required=True,
        help="Hugging Face commit of the weights, not a branch",
    )
    parser.add_argument(
        "--torch-version", default="2.7.1", help="pinned torch version"
    )
    parser.add_argument(
        "--out", type=Path, default=Path("dist"), help="output directory"
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        help="use an already downloaded checkpoint instead of Hugging Face",
    )
    args = parser.parse_args()

    check_versions(args.torch_version)

    if args.checkpoint:
        weights_path = args.checkpoint
    else:
        weights_path = Path(
            hf_hub_download(
                repo_id=HF_REPO,
                filename=HF_FILENAME,
                revision=args.hf_revision,
            )
        )
    print(f"weights: {HF_REPO}/{HF_FILENAME} ({weights_path})")

    backbone = load_backbone(weights_path)
    wrapper = PrithviEncoderONNX(backbone).eval()

    dummy = (
        torch.zeros(1, NUM_FRAMES, len(BANDS), WINDOW, WINDOW),
        torch.zeros(1, NUM_FRAMES, 2),
        torch.zeros(1, 2),
    )
    with torch.inference_mode():
        probe = wrapper(*dummy)
    expected_shape = (1, TOKEN_COUNT, EMBED_DIM)
    if tuple(probe.shape) != expected_shape:
        raise SystemExit(
            f"probe output {tuple(probe.shape)} != {expected_shape}; update "
            "config.json and udf.py together"
        )
    print(f"probe output: {tuple(probe.shape)}")

    args.out.mkdir(parents=True, exist_ok=True)
    onnx_path = args.out / f"{MODEL}_encoder.onnx"
    print(f"exporting {onnx_path} (opset {OPSET})")
    torch.onnx.export(
        wrapper,
        dummy,
        onnx_path.as_posix(),
        input_names=["frames", "temporal", "location"],
        output_names=["tokens"],
        dynamic_axes={
            "frames": {0: "batch"},
            "temporal": {0: "batch"},
            "location": {0: "batch"},
            "tokens": {0: "batch"},
        },
        opset_version=OPSET,
        do_constant_folding=True,
        dynamo=False,
    )

    shape = inspect_graph(onnx_path)
    token_shape = shape["outputs"]["tokens"]
    if token_shape[1:] != [TOKEN_COUNT, EMBED_DIM]:
        raise SystemExit(
            f"{onnx_path}: expected static tokens [batch, {TOKEN_COUNT}, "
            f"{EMBED_DIM}], got {token_shape}"
        )
    expected_inputs = {
        "frames": ["batch", NUM_FRAMES, len(BANDS), WINDOW, WINDOW],
        "temporal": ["batch", NUM_FRAMES, 2],
        "location": ["batch", 2],
    }
    for name, dims in expected_inputs.items():
        if shape["inputs"][name] != dims:
            raise SystemExit(
                f"{onnx_path}: input {name} has shape {shape['inputs'][name]}, "
                f"expected {dims}"
            )

    parity_check(wrapper, onnx_path)

    sidecar = onnx_path.with_suffix(onnx_path.suffix + ".data")
    files = [onnx_path] + ([sidecar] if sidecar.is_file() else [])
    archive = args.out / f"{MODEL}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, arcname=path.name)

    metadata = {
        "model": MODEL,
        "hf_repo": HF_REPO,
        "hf_filename": HF_FILENAME,
        "hf_revision": args.hf_revision,
        "torch_version": torch.__version__,
        "embedding_dim": EMBED_DIM,
        "tokens_per_side": TOKENS_PER_SIDE,
        "num_frames": NUM_FRAMES,
        "patch_size": PATCH,
        "window": WINDOW,
        "input_channels": len(BANDS),
        "band_order": list(BANDS),
        "band_mean": list(BAND_MEAN),
        "band_std": list(BAND_STD),
        "nodata_after_normalization": NODATA_FLOAT,
        "files": [path.name for path in files],
        "sha256": {path.name: sha256(path) for path in files},
        "archive": archive.name,
    }
    metadata_path = args.out / f"{MODEL}.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    print(f"embedding width: {EMBED_DIM}")
    print(f"tokens per tile: {TOKEN_COUNT}")
    print(f"archive:         {archive}")
    print(f"metadata:        {metadata_path}")


if __name__ == "__main__":
    sys.exit(main())
