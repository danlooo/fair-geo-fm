#!/usr/bin/env python3
"""Export the TESSERA v2 pixel student encoder to ONNX for the openEO UDF.

Run once, offline, with pinned torch (see ``build.yml`` for the CI invocation):

    uv venv .venv-export
    uv pip install --python .venv-export/bin/python \\
        --default-index https://download.pytorch.org/whl/cpu "torch==2.7.1+cpu"
    uv pip install --python .venv-export/bin/python \\
        "onnx==1.17.0" "onnxruntime==1.20.1" "onnxscript==0.7.2" "huggingface_hub==0.34.4"
    .venv-export/bin/python scripts/export-tessera-onnx.py \\
        --hf-revision bd08c90bf56ca6a1e7609a395cd47115fa12f1d8 --out weights

The exported graph takes the already standardized per-pixel sequences the UDF
builds:

    s2_x  (B, T_s2, 11)  ten Sentinel-2 bands plus day-of-year
    s1_x  (B, T_s1,  3)  VV, VH plus day-of-year (ascending and descending merged)

and returns the 128-dimensional Matryoshka embedding ``(B, 128)``. Both time
dimensions are dynamic: the UDF groups pixels by observation count and pads
each group to a bin edge before calling the graph, so one export serves every
group. Standardization and binning stay in NumPy because they depend on the
source of each observation; the graph is only the frozen transformer.

The script writes ``<model>.json`` next to the graph with the embedding width,
Matryoshka cuts, input widths and file sha256. Use those values for
``config.json`` and ``udf.py``.
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
from tessera_student import (  # noqa: E402
    S1_BAND_ORDER,
    S2_BAND_ORDER,
    AttentionPooling,
    TransformerEncoder,
    load_model,
)

MODEL = "tessera_v2"
HF_REPO = "geotessera/TESSERA-V-2.0-2B-N"
HF_FILENAME = "ckpt/student_nano.pt"

# Per-pixel input widths: bands plus the day-of-year channel.
S2_INPUT_CHANNELS = len(S2_BAND_ORDER) + 1
S1_INPUT_CHANNELS = len(S1_BAND_ORDER) + 1
# The Dynamo exporter (needed for dynamic time dimensions) supports opset 18
# and later; ONNX Runtime 1.20 in py311-onnx implements it.
OPSET = 18


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


class ExportAttentionPooling(nn.Module):
    """Branch-free ``AttentionPooling`` for export.

    The upstream module branches on the sequence length in Python (``T == 0``
    returns zeros, ``T == 1`` squeezes). Those branches create guards that make
    a dynamic time dimension impossible to export. The UDF never passes an empty
    sequence (zero-observation groups are skipped) and the softmax path over a
    single position is the identity, so the branch-free form is equivalent for
    every input the graph accepts.
    """

    def __init__(self, original: AttentionPooling) -> None:
        super().__init__()
        self.query = original.query

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        weights = torch.softmax(self.query(x), dim=1)
        return (weights * x).sum(dim=1)


class TesseraEncoderONNX(nn.Module):
    """Expose ``PixelStudent.encode`` as a two-input, time-dynamic graph."""

    def __init__(self, model: nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(self, s2_x: torch.Tensor, s1_x: torch.Tensor) -> torch.Tensor:
        return self.model.encode(s2_x, s1_x)


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


def parity_check(wrapper: nn.Module, onnx_path: Path, repr_dim: int) -> None:
    import onnxruntime as ort

    session = ort.InferenceSession(
        onnx_path.as_posix(), providers=["CPUExecutionProvider"]
    )
    generator = torch.Generator().manual_seed(11)
    cases = [(3, 16, 8), (1, 5, 6), (2, 1, 1), (4, 32, 32), (1, 256, 7)]
    for batch, t_s2, t_s1 in cases:
        s2 = torch.randn((batch, t_s2, S2_INPUT_CHANNELS), generator=generator)
        s1 = torch.randn((batch, t_s1, S1_INPUT_CHANNELS), generator=generator)
        with torch.inference_mode():
            expected = wrapper(s2, s1).numpy()
        actual = session.run(
            None, {"s2_x": s2.numpy(), "s1_x": s1.numpy()}
        )[0]
        np.testing.assert_allclose(actual, expected, rtol=1e-4, atol=1e-4)
        if expected.shape != (batch, repr_dim):
            raise SystemExit(
                f"T=({t_s2},{t_s1}) produced {expected.shape}, "
                f"expected {(batch, repr_dim)}"
            )
        print(
            f"parity B={batch} T_s2={t_s2} T_s1={t_s1}: "
            f"max abs diff {np.max(np.abs(actual - expected)):.3e}"
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

    model = load_model(weights_path).eval()
    # Nested tensors are a training/inference fast path the exporter cannot
    # represent; the non-nested path computes the same result. Replace the
    # length-dependent pooling branch so the time dimensions can stay dynamic.
    for module in model.modules():
        if isinstance(module, nn.TransformerEncoder):
            module.enable_nested_tensor = False
        if isinstance(module, TransformerEncoder) and isinstance(
            module.attn_pool, AttentionPooling
        ):
            module.attn_pool = ExportAttentionPooling(module.attn_pool)
    wrapper = TesseraEncoderONNX(model).eval()

    repr_dim = int(model.repr_dim)
    matryoshka_dims = [int(value) for value in model.matryoshka_dims]
    dummy = (
        torch.zeros(2, 16, S2_INPUT_CHANNELS),
        torch.zeros(2, 8, S1_INPUT_CHANNELS),
    )
    with torch.inference_mode():
        probe = wrapper(*dummy)
    if tuple(probe.shape) != (2, repr_dim):
        raise SystemExit(
            f"probe output {tuple(probe.shape)} != {(2, repr_dim)}; update "
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
        input_names=["s2_x", "s1_x"],
        output_names=["embedding"],
        dynamic_axes={
            "s2_x": {0: "batch", 1: "t_s2"},
            "s1_x": {0: "batch", 1: "t_s1"},
            "embedding": {0: "batch"},
        },
        opset_version=OPSET,
        do_constant_folding=True,
        # The legacy tracer bakes MultiheadAttention's sequence length in as a
        # constant; only the Dynamo exporter keeps both time dimensions dynamic.
        dynamo=True,
    )

    shape = inspect_graph(onnx_path)
    # The Dynamo exporter labels symbolic dimensions itself (s0, s1, ...), so
    # check the static widths and leave batch/time symbolic; the parity check
    # below varies both and would fail if either had been frozen.
    for name, channels in (
        ("s2_x", S2_INPUT_CHANNELS),
        ("s1_x", S1_INPUT_CHANNELS),
    ):
        dims = shape["inputs"][name]
        if (
            len(dims) != 3
            or dims[2] != channels
            or all(isinstance(dim, int) for dim in dims[:2])
        ):
            raise SystemExit(
                f"{onnx_path}: input {name} has shape {dims}, expected "
                f"[batch, time, {channels}] with dynamic batch and time"
            )
    embedding = shape["outputs"]["embedding"]
    if (
        len(embedding) != 2
        or embedding[1] != repr_dim
        or isinstance(embedding[0], int)
    ):
        raise SystemExit(
            f"{onnx_path}: expected dynamic embedding [batch, {repr_dim}], got "
            f"{embedding}"
        )

    parity_check(wrapper, onnx_path, repr_dim)

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
        "embedding_dim": repr_dim,
        "matryoshka_dims": matryoshka_dims,
        "s2_input_channels": S2_INPUT_CHANNELS,
        "s1_input_channels": S1_INPUT_CHANNELS,
        "s2_band_order": list(S2_BAND_ORDER),
        "s1_band_order": list(S1_BAND_ORDER),
        "files": [path.name for path in files],
        "sha256": {path.name: sha256(path) for path in files},
        "archive": archive.name,
    }
    metadata_path = args.out / f"{MODEL}.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    print(f"embedding width: {repr_dim}")
    print(f"matryoshka cuts: {matryoshka_dims}")
    print(f"archive:         {archive}")
    print(f"metadata:        {metadata_path}")


if __name__ == "__main__":
    sys.exit(main())
