"""The cache-id resolver in tools/mmproj_swap_probe.py.

Every rule here exists because we got it wrong twice on the same repo: first by
asking for `ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF:IQ3_S-mtp` (a tag can never
contain a dash, so that id names nothing), then by rejecting `...-IQ3_S-mtp.gguf`
as a sidecar because its name ends in "mtp". The file is the model; its head is
inside it. llama.cpp says so with the id IT lists: `:MTP`.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

REPO = "ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF"


def _tool():
    spec = importlib.util.spec_from_file_location(
        "mmproj_swap_probe", pathlib.Path(__file__).resolve().parent.parent / "tools" / "mmproj_swap_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hub(tmp_path, repo: str, names: list[str]) -> pathlib.Path:
    """A hub cache laid out the way huggingface-cli leaves it."""
    hub = tmp_path / "hub"
    snap = hub / f"models--{repo.replace('/', '--')}" / "snapshots" / ("a" * 40)
    snap.mkdir(parents=True)
    for n in names:
        (snap / n).write_bytes(b"GGUF")
    return hub


def test_a_tag_is_the_last_dash_token_uppercased_not_the_quant():
    import app.core.modelindex as mi
    # the name that started this: quant IQ3, head inside, tag MTP
    assert mi._hf_split("Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.gguf")[3] == "MTP"
    assert mi._hf_split("Qwen3.8-27B-Q8_K_XL.gguf")[3] == "Q8_K_XL"
    # a shard set keeps its quant tag; the split suffix is stripped first
    assert mi._hf_split("Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf")[3] == "IQ3_S"


def test_the_integrated_head_file_resolves_and_is_not_a_sidecar(tmp_path):
    hub = _hub(tmp_path, REPO, ["Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.gguf"])
    tool = _tool()
    got = tool.cached_model_file(f"{REPO}:MTP", hub)
    assert got is not None and got.name == "Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.gguf"
    # the exact tag is the id; a loose one is resolved the way the runtime would,
    # and recorded as such rather than trusted
    assert tool.cached_model_file(f"{REPO}:IQ3_S-MTP", hub).name == \
        "Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.gguf"


def test_an_exact_tag_beats_a_loose_one_when_both_would_match(tmp_path):
    """:IQ3_S must not quietly load the MTP-tagged file.

    llama.cpp's fallback substring search WOULD do exactly that on a single-file repo,
    and the mistake is invisible: you asked to price the IQ3_S quant and the runtime
    answered with whatever was there. Exact-tag-first is what keeps a measurement
    attached to the file it describes.
    """
    hub = _hub(tmp_path, REPO, ["Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.gguf",   # tag MTP
                               "Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_S.gguf"])        # tag IQ3_S
    tool = _tool()
    assert tool.cached_model_file(f"{REPO}:IQ3_S", hub).name.endswith("-IQ3_S.gguf")
    assert tool.cached_model_file(f"{REPO}:MTP", hub).name.endswith("IQ3_S-mtp.gguf")


def test_a_separate_draft_adapter_is_never_returned_as_the_model(tmp_path):
    """unsloth/Qwen3.8-27B-GGUF has an `mtp-Qwen3.8-27B-Q4_0.gguf` in the same folder.

    Its tag is MTP, so a tag-only search hands it back; it is not a chat model and
    measuring it would say nothing about the 27B.
    """
    repo = "unsloth/Qwen3.8-27B-GGUF"
    hub = _hub(tmp_path, repo, ["Qwen3.8-27B-Q8_K_XL.gguf", "mtp-Qwen3.8-27B-Q4_0.gguf",
                                "mmproj-BF16.gguf"])
    tool = _tool()
    assert tool.cached_model_file(f"{repo}:Q8_K_XL", hub).name == "Qwen3.8-27B-Q8_K_XL.gguf"
    assert tool.cached_model_file(f"{repo}:MTP", hub) is None
    assert tool.cached_model_file(f"{repo}:BF16", hub) is None


def test_only_shard_part_one_is_loadable(tmp_path):
    repo = "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF"
    hub = _hub(tmp_path, repo, ["Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00002-of-00002.gguf"])
    tool = _tool()
    assert tool.cached_model_file(f"{repo}:IQ3_S", hub) is None
    (hub / f"models--{repo.replace('/', '--')}" / "snapshots" / ("a" * 40) /
     "Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf").write_bytes(b"GGUF")
    got = tool.cached_model_file(f"{repo}:IQ3_S", hub)
    assert got is not None and got.name.endswith("00001-of-00002.gguf")


def test_an_absent_repo_is_absent(tmp_path):
    tool = _tool()
    assert tool.cached_model_file("nobody/Not-Here-GGUF:Q8_0", tmp_path / "hub") is None
