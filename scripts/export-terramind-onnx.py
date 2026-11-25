#!/usr/bin/env python3
"""Export a TerraMind 1.0 encoder to ONNX for the openEO UDF.

Run once, offline, with pinned TerraTorch and torch versions:

    uv venv .venv-export
    uv pip install --python .venv-export/bin/python \\
        --extra-index-url https://download.pytorch.org/whl/cpu \\
        "torch==2.7.1+cpu" "terratorch==1.2.1"
    .venv-export/bin/python scripts/export-terramind-onnx.py \\
        --model terramind_v1_tiny \\
        --hf-revision 2b5ac0a3ed7dd7e922ccfd595b56607f342df343 \\
        --out weights

The exported graph takes three inputs in their raw value ranges:

    s2l1c (B, 13, 224, 224)   Sentinel-2 L1C digital numbers
    s1grd (B, 2, 224, 224)    Sentinel-1 GRD backscatter in decibels
    dem   (B, 1, 224, 224)    Copernicus DEM elevation in metres

and returns the last-layer patch tokens ``(B, 196, D)``. TerraTorch's
per-modality pretraining mean/std are baked into the graph as constants, so the
openEO UDF hands raw values straight to ONNX Runtime.

The S1 unit is not optional: ``v1_pretraining_mean`` for
``untok_sen1grd@224`` is ``[-12.599, -20.293]`` with a standard deviation of
about 5, i.e. decibels. The UDF converts the linear power from
``sar_backscatter`` with ``10 * log10(power)`` before inference.

The script also writes ``<model>.json`` next to the graph, recording the
embedding width, token count, input channel counts, S2 L1C band order and file
sha256. Use those values for ``config.json`` (``dimensions``) and ``udf.py``;
do not copy them from a notebook. The zip produced here is published under
``weights/`` and referenced from ``udf-dependency-archives``.
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

MODELS = (
    "terramind_v1_tiny",
    "terramind_v1_small",
    "terramind_v1_base",
    "terramind_v1_large",
)
# Required inputs for this adapter. The model supports more modalities, but a
# published graph must not silently drop one of these.
MODALITY_TOKENS = {
    "S2L1C": "untok_sen2l1c@224",
    "S1GRD": "untok_sen1grd@224",
    "DEM": "untok_dem@224",
}
INPUT_CHANNELS = {"S2L1C": 13, "S1GRD": 2, "DEM": 1}
PATCH = 224
OPSET = 17
# Shapes the published model directory assumes. A mismatch means config.json
# (dimensions) and udf.py (EMBED_DIM) must be updated together.
EXPECTED_SHAPES = {"terramind_v1_tiny": (196, 192)}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_versions(torch_version: str, terratorch_version: str) -> None:
    installed_torch = torch.__version__.split("+", 1)[0]
    if installed_torch != torch_version:
        raise SystemExit(
            f"installed torch {installed_torch} != --torch-version "
            f"{torch_version}; the export must run on the pinned versions"
        )
    from importlib.metadata import version

    installed_terratorch = version("terratorch")
    if installed_terratorch != terratorch_version:
        raise SystemExit(
            f"installed terratorch {installed_terratorch} != "
            f"--terratorch-version {terratorch_version}"
        )


def pretraining_stats(register) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    stats = {}
    for modality, token in MODALITY_TOKENS.items():
        try:
            mean = register.v1_pretraining_mean[token]
            std = register.v1_pretraining_std[token]
        except KeyError:
            available = ", ".join(sorted(register.v1_pretraining_mean))
            raise SystemExit(
                f"TerraTorch does not expose pretraining statistics for {token!r}. "
                f"Available tokens: {available}. This TerraTorch release cannot "
                "run the three-modality graph."
            ) from None
        stats[modality] = (
            np.asarray(mean, dtype=np.float32),
            np.asarray(std, dtype=np.float32),
        )
    return stats


class TerraMindEncoderONNX(nn.Module):
    """Standardize every modality, then return the last-layer tokens.

    The fixed reshape is deliberate: TerraMind merges modality tokens with a
    ``ReduceMean``, which the legacy ONNX tracer records with dynamic
    token/embedding dimensions. Reshaping to the concrete token count and width
    makes the exported output shape static, so ``config.json`` can state the
    embedding dimensions and a client can allocate the result.
    """

    def __init__(
        self,
        backbone: nn.Module,
        stats: dict[str, tuple[np.ndarray, np.ndarray]],
        token_count: int,
        embedding_dim: int,
    ):
        super().__init__()
        self.backbone = backbone
        self.token_count = token_count
        self.embedding_dim = embedding_dim
        for modality, (mean, std) in stats.items():
            self.register_buffer(f"{modality}_mean", torch.tensor(mean).view(1, -1, 1, 1))
            self.register_buffer(f"{modality}_std", torch.tensor(std).view(1, -1, 1, 1))

    def _standardize(self, tensor: torch.Tensor, modality: str) -> torch.Tensor:
        mean = getattr(self, f"{modality}_mean")
        std = getattr(self, f"{modality}_std")
        return (tensor - mean) / (std + 1e-9)

    def forward(
        self,
        s2l1c: torch.Tensor,
        s1grd: torch.Tensor,
        dem: torch.Tensor,
    ) -> torch.Tensor:
        inputs = {
            "S2L1C": self._standardize(s2l1c, "S2L1C"),
            "S1GRD": self._standardize(s1grd, "S1GRD"),
            "DEM": self._standardize(dem, "DEM"),
        }
        outputs = self.backbone(inputs)
        if isinstance(outputs, (list, tuple)):
            outputs = outputs[-1]
        if isinstance(outputs, dict):
            outputs = next(iter(outputs.values()))
        return outputs.reshape(
            outputs.shape[0], self.token_count, self.embedding_dim
        )


def inspect_graph(path: Path) -> dict:
    import onnx

    graph = onnx.load(path.as_posix(), load_external_data=False).graph
    inputs = {
        value.name: [
            dim.dim_value if dim.HasField("dim_value") else dim.dim_param
            for dim in value.type.tensor_type.shape.dim
        ]
        for value in graph.input
    }
    outputs = {
        value.name: [
            dim.dim_value if dim.HasField("dim_value") else dim.dim_param
            for dim in value.type.tensor_type.shape.dim
        ]
        for value in graph.output
    }
    return {"inputs": inputs, "outputs": outputs}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", choices=MODELS, default="terramind_v1_tiny")
    parser.add_argument(
        "--hf-revision",
        required=True,
        help="Hugging Face commit of the weights, not a branch",
    )
    parser.add_argument(
        "--torch-version", default="2.7.1", help="pinned torch version"
    )
    parser.add_argument(
        "--terratorch-version", default="1.2.1", help="pinned TerraTorch version"
    )
    parser.add_argument(
        "--out", type=Path, default=Path("dist"), help="output directory"
    )
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="print the TerraTorch registry tokens and exit without exporting",
    )
    args = parser.parse_args()

    from terratorch.models.backbones.terramind.model import (
        terramind_register as register,
    )
    from terratorch.registry import BACKBONE_REGISTRY

    stats = pretraining_stats(register)
    if args.inspect_only:
        print("modality tokens:", ", ".join(sorted(MODALITY_TOKENS.values())))
        print("available tokens:", ", ".join(sorted(register.v1_pretraining_mean)))
        print("S2 L1C bands:", ", ".join(register.PRETRAINED_BANDS["untok_sen2l1c@224"]))
        return

    check_versions(args.torch_version, args.terratorch_version)

    try:
        weights = register.pretrained_weights[args.model]
    except KeyError:
        raise SystemExit(
            f"{args.model} has no released weights in this TerraTorch release"
        ) from None
    weights_path = hf_hub_download(
        repo_id=weights["hf_hub_id"],
        filename=weights["hf_hub_filename"],
        revision=args.hf_revision,
    )
    print(f"weights: {weights['hf_hub_id']}/{weights['hf_hub_filename']} -> {weights_path}")

    # Load the revision-pinned checkpoint through TerraTorch's own filter,
    # exactly like its `pretrained=True` path, but without letting it download
    # the mutable branch. `ckpt_path` would load with strict=False and could
    # leave random weights in place if a key changed.
    backbone = BACKBONE_REGISTRY.build(
        args.model,
        pretrained=False,
        modalities=list(MODALITY_TOKENS),
        merge_method="mean",
    ).eval()
    state_dict = torch.load(
        weights_path, map_location="cpu", weights_only=True
    )
    state_dict = register.checkpoint_filter_fn(state_dict, backbone)
    backbone.load_state_dict(state_dict, strict=True)

    # Probe the eager model for the concrete token count and width. This also
    # fails early when a modality is not wired through the three-input forward.
    dummy = tuple(
        torch.zeros(1, INPUT_CHANNELS[modality], PATCH, PATCH)
        for modality in MODALITY_TOKENS
    )
    with torch.inference_mode():
        probe = backbone(
            {
                modality: torch.zeros(
                    1, INPUT_CHANNELS[modality], PATCH, PATCH
                )
                for modality in MODALITY_TOKENS
            }
        )
    if isinstance(probe, (list, tuple)):
        probe = probe[-1]
    if isinstance(probe, dict):
        probe = next(iter(probe.values()))
    if probe.ndim != 3:
        raise SystemExit(
            f"{args.model}: expected tokens [batch, tokens, width], got "
            f"{tuple(probe.shape)}"
        )
    token_count = int(probe.shape[1])
    embedding_dim = int(probe.shape[2])
    tokens_per_side = round(token_count**0.5)
    print(f"probe output: {tuple(probe.shape)}")
    expected = EXPECTED_SHAPES.get(args.model)
    if expected and (token_count, embedding_dim) != expected:
        raise SystemExit(
            f"{args.model} produced ({token_count}, {embedding_dim}), but the "
            f"published model assumes {expected}. Update "
            f"models/{args.model}/config.json and udf.py before exporting."
        )

    wrapped = TerraMindEncoderONNX(
        backbone, stats, token_count, embedding_dim
    ).eval()

    args.out.mkdir(parents=True, exist_ok=True)
    onnx_path = args.out / f"{args.model}_encoder.onnx"
    print(f"exporting {onnx_path} (opset {OPSET})")
    torch.onnx.export(
        wrapped,
        dummy,
        onnx_path.as_posix(),
        input_names=["s2l1c", "s1grd", "dem"],
        output_names=["tokens"],
        dynamic_axes={
            "s2l1c": {0: "batch"},
            "s1grd": {0: "batch"},
            "dem": {0: "batch"},
            "tokens": {0: "batch"},
        },
        opset_version=OPSET,
        do_constant_folding=True,
        dynamo=False,
    )

    # The explicit reshape in the wrapper must survive the export: the token
    # count and width have to be static so config.json can state the embedding
    # dimensions and a client can allocate the result.
    shape = inspect_graph(onnx_path)
    token_shape = shape["outputs"]["tokens"]
    if len(token_shape) != 3 or not all(
        isinstance(value, int) for value in token_shape[1:]
    ):
        raise SystemExit(
            f"{onnx_path}: the token count or width is dynamic; expected static "
            f"dimensions, got {token_shape}"
        )
    if (int(token_shape[1]), int(token_shape[2])) != (
        token_count,
        embedding_dim,
    ):
        raise SystemExit(
            f"{onnx_path}: exported shape {token_shape} does not match the "
            f"eager probe ({token_count}, {embedding_dim})"
        )

    sidecar = onnx_path.with_suffix(onnx_path.suffix + ".data")
    files = [onnx_path] + ([sidecar] if sidecar.is_file() else [])
    archive = args.out / f"{args.model}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, arcname=path.name)

    metadata = {
        "model": args.model,
        "hf_repo": weights["hf_hub_id"],
        "hf_filename": weights["hf_hub_filename"],
        "hf_revision": args.hf_revision,
        "torch_version": torch.__version__,
        "terratorch_version": args.terratorch_version,
        "embedding_dim": embedding_dim,
        "tokens_per_side": tokens_per_side,
        "input_channels": INPUT_CHANNELS,
        "s2l1c_bands": register.PRETRAINED_BANDS["untok_sen2l1c@224"],
        "files": [path.name for path in files],
        "sha256": {path.name: sha256(path) for path in files},
        "archive": archive.name,
    }
    metadata_path = args.out / f"{args.model}.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    print(f"embedding width: {embedding_dim}")
    print(f"tokens per tile: {tokens_per_side * tokens_per_side}")
    print(f"s2l1c bands:     {', '.join(metadata['s2l1c_bands'])}")
    print(f"archive:         {archive}")
    print(f"metadata:        {metadata_path}")
    print(f"Set config.json 'dimensions' to {embedding_dim}.")


if __name__ == "__main__":
    sys.exit(main())
