#!/usr/bin/env python3
"""PLE micro-ONNX: the exact op shape EmbeddingGemma-2's PLE block uses.

Oct 17 finding (from model.safetensors): the EG2 text trunk carries NO conv
weights in the PLE block — per-layer tensors are exactly
per_layer_input_gate (512x512), per_layer_projection (512x512),
post_per_layer_input_norm (512), plus a per-layer layer_scalar. So PLE here
is GEMM + Tanh gate + Mul + RMSNorm + scaled residual — the depthwise-conv
risk that gated the Path F arc (Gemma 3n's ple_conv) does not exist at 300M.

This script builds that shape as a fixed-shape fp32 ONNX (S=64, hidden=512)
to feed the RyzenAI 1.8 NLP flow in ~/ai/venvs/ryzenai-b:
`onnx_utils auto` preprocess+partition tells us, at compile time and without
the AIE, whether DD's matcher covers the PLE pattern; `--execute` is the
compile-on-device step (needs the custom-op library, DML on Windows; the
Linux EP path is the follow-up). Verdict lands in PLAN §4a.
"""
import argparse

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

H = 512
S = 64


def rmsnorm(name: str, g_in: str, eps: float = 1e-6) -> list:
    """y = x / sqrt(mean(x^2)+eps) * g  (Gemma-style pre-normalized RMSNorm).
    opset 18: ReduceMean takes axes as an input and always keeps dims."""
    return [
        helper.make_node("Mul", [g_in, g_in], [f"{name}_sq"]),
        helper.make_node("ReduceMean", [f"{name}_sq", "axes_2"], [f"{name}_ms"]),
        helper.make_node("Add", [f"{name}_ms", "eps"], [f"{name}_mse"]),
        helper.make_node("Sqrt", [f"{name}_mse"], [f"{name}_rms"]),
        helper.make_node("Div", [g_in, f"{name}_rms"], [f"{name}_n"]),
        helper.make_node("Mul", [f"{name}_n", "post_norm_w"], [f"{name}_out"]),
    ]


def build(path: str) -> None:
    rng = np.random.default_rng(7)
    inits = [
        numpy_helper.from_array(np.array(1e-6, np.float32), "eps"),
        numpy_helper.from_array(np.array([2], np.int64), "axes_2"),
        numpy_helper.from_array((rng.standard_normal((H, H)).astype(np.float32) * 0.02), "gate_w"),
        numpy_helper.from_array((rng.standard_normal((H, H)).astype(np.float32) * 0.02), "proj_w"),
        numpy_helper.from_array(np.ones(H, np.float32), "post_norm_w"),
        numpy_helper.from_array(np.array(0.1, np.float32), "layer_scalar"),
    ]
    nodes = [
        helper.make_node("MatMul", ["x", "gate_w"], ["g_lin"]),
        helper.make_node("Tanh", ["g_lin"], ["gate"]),          # the PLE gate
        helper.make_node("MatMul", ["x", "proj_w"], ["p_lin"]),
        helper.make_node("Mul", ["p_lin", "gate"], ["ple"]),    # gated projection
    ] + rmsnorm("ple", "ple") + [
        helper.make_node("Mul", ["ple_out", "layer_scalar"], ["ple_scaled"]),
        helper.make_node("Add", ["x", "ple_scaled"], ["y"]),    # residual
    ]
    graph = helper.make_graph(
        nodes, "ple_micro",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, S, H])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, S, H])],
        inits,
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 18)])
    model.ir_version = 10  # onnx 1.23 emits IR 14; the AMD ort build caps at 13
    onnx.checker.check_model(model)
    onnx.save(model, path)
    ops = sorted({n.op_type for n in nodes})
    print(f"wrote {path}: S={S} H={H} | ops: {', '.join(ops)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/ple_micro.onnx")
    build(ap.parse_args().out)
