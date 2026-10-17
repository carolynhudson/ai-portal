"""The hipcc build shim (ROCm 10.1 headers missing scalar f32 ocml prototypes).

The shim itself is a file in ~/ai/config/shims/ — the portal's job is to feed it
to hipcc when (and only when) it exists, without disturbing anything else.
"""
from __future__ import annotations

import os

from app.core import paths
from app.providers import llamacpp


def test_the_shim_file_gates_the_flag(tmp_path, monkeypatch):
    shim = tmp_path / "hip_bf16_log2.h"
    monkeypatch.setattr(paths, "HIP_OCML_SHIM", shim)
    env = {"VULKAN_SDK": "/x"}
    args = llamacpp._hipcc_shim(env, lambda *a: None)
    assert args == [], "absent file = untouched env and no cmake args"
    assert "HIPCC_COMPILE_FLAGS_APPEND" not in env

    shim.write_text("// declarations only\n")
    args = llamacpp._hipcc_shim(env, lambda *a: None)
    assert env["HIPCC_COMPILE_FLAGS_APPEND"] == f"-include {shim}"
    assert args == [f"-DCMAKE_HIP_FLAGS=-include {shim}"], \
        "CMake drives clang++ directly — the flag must ride in CMAKE_HIP_FLAGS"


def test_an_existing_append_value_survives(tmp_path, monkeypatch):
    shim = tmp_path / "hip_bf16_log2.h"
    shim.write_text("//")
    monkeypatch.setattr(paths, "HIP_OCML_SHIM", shim)
    env = {"HIPCC_COMPILE_FLAGS_APPEND": "-O2"}
    llamacpp._hipcc_shim(env, lambda *a: None)
    assert env["HIPCC_COMPILE_FLAGS_APPEND"] == f"-include {shim} -O2"
