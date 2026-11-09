"""hfgrab: the -hf law — spec parsing, file-set planning, grab controls,
placement routing (ladder for GGUF, classifier for Comfy chains).

Network-free: the hub calls are monkeypatched and the fake download builds
a real HF cache layout under the isolated AI_PORTAL_HOME, so the GGUF test
walks the whole law through the live migration ladder."""
import json
import os
import shutil
import struct
import tempfile
import time

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-hg-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import db, hfgrab, paths  # noqa: E402
from app.main import app  # noqa: E402

db.init()


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    yield c


@pytest.fixture(autouse=True)
def _ctl_reset():
    yield
    hfgrab.set_command("running")


def make_gguf(path, name="Test Model 1B", arch="llama", ctx=4096):
    kvs = []

    def add_str(key, val):
        kb, vb = key.encode(), val.encode()
        kvs.append(struct.pack(">Q", len(kb)) + kb +
                   struct.pack(">I", 8) + struct.pack(">Q", len(vb)) + vb)

    def add_u32(key, val):
        kb = key.encode()
        kvs.append(struct.pack(">Q", len(kb)) + kb +
                   struct.pack(">I", 4) + struct.pack(">I", val))

    add_str("general.name", name)
    add_str("general.architecture", arch)
    add_u32(f"{arch}.context_length", ctx)
    path = str(path)
    with open(path, "wb") as f:
        f.write(b"GGUF" + b"\x00\x00\x00\x03" + struct.pack(">QQ", 1, 3)
                + b"".join(kvs) + b"\x00" * 32)


def make_safetensors(path, keys):
    hdr = {k: {"dtype": "F32", "shape": [1, 4]} for k in keys}
    hb = json.dumps(hdr).encode()
    pad = (8 - len(hb) % 8) % 8
    with open(str(path), "wb") as f:
        f.write(struct.pack("<Q", len(hb) + pad) + hb + b" " * pad + b"\x00" * 8)


# 2026-11-08: witnessed on LessThanThreeAI/Qwen3.8-27B-Humanlike-Chat-GGUF —
# a split file's shard-dash joins the extension side, so rstrip("-_") on the
# full name never touches it and the quant read as ''. BF16 vanished from its
# own repo's quant menu; the menu's trailing empty entry is the bug's shape.
def test_split_files_report_their_real_quant():
    n = "Qwen3.8-27B-Humanlike-Chat-BF16-00001-of-00002.gguf"
    assert hfgrab._quant_of(n) == "BF16"
    assert hfgrab._quant_of("repo/dir/Model-Q6_K.gguf") == "Q6_K"
    assert hfgrab._quant_of("My Model.gguf") == "My Model"


def test_parse_spec_is_the_hf_grammar():
    s = hfgrab.parse_spec("LessThanThreeAI/Qwen3.8-27B-Humanlike-Chat-GGUF:Q6_K")
    assert s["repo"] == "LessThanThreeAI/Qwen3.8-27B-Humanlike-Chat-GGUF"
    assert s["quant"] == "Q6_K" and s["label"].endswith(":Q6_K")
    # pasted CLI commands, with a named file (modeladd's job, not this law)
    s = hfgrab.parse_spec("hf download Some/Repo-QG some/file-Q4_K_M.gguf")
    assert s["repo"] == "Some/Repo-QG" and s["quant"] is None
    s = hfgrab.parse_spec("huggingface-cli download Some/Repo --include x")
    assert s["repo"] == "Some/Repo" and s["quant"] is None
    # comfy variant spec: the quant slot filters variants
    assert hfgrab.parse_spec("Comfy-Org/Qwen-Image_ComfyUI:fp8")["quant"] == "fp8"
    with pytest.raises(ValueError):
        hfgrab.parse_spec("no-slash")


# the real repo's (name, size) shape, condensed
REPO = "TestOrg/test-hf-GGUF"
SIBS = [
    ("Model-BF16-00001-of-00002.gguf", 25 * 2**30),
    ("Model-BF16-00002-of-00002.gguf", 25 * 2**30),
    ("Model-Q4_K_M.gguf", 15 * 2**30),
    ("Model-Q6_K.gguf", 20 * 2**30),
    ("mmproj/mmproj-BF16.gguf", 2**30),
    ("imatrix/imatrix.gguf", 2**20),
    ("lora/Model-LoRA-r320-F16.gguf", 4 * 2**30),
    ("images/cover.png", 10**5),
    ("README.md", 100),
]


def test_the_gguf_plan_is_quant_parts_plus_sidecars():
    pl = hfgrab.plan(REPO, "Q6_K", SIBS)
    assert [(f["role"], f["name"]) for f in pl["files"]] == [
        ("model", "Model-Q6_K.gguf"), ("sidecar", "mmproj/mmproj-BF16.gguf")]
    assert pl["kind"] == "gguf"
    # a split quant is ALL of its parts
    pl = hfgrab.plan(REPO, "bf16", SIBS)          # quant is case-free
    assert [f["name"] for f in pl["files"] if f["role"] == "model"] == [
        "Model-BF16-00001-of-00002.gguf", "Model-BF16-00002-of-00002.gguf"]
    # junk NEVER travels: no lora, no imatrix, no images
    names = " ".join(f["name"] for f in pl["files"])
    assert "lora" not in names and "imatrix" not in names and "images" not in names


def test_a_gguf_repo_without_a_quant_refuses_with_the_menu():
    with pytest.raises(ValueError) as e:
        hfgrab.plan(REPO, None, SIBS)
    assert "Q6_K" in str(e.value)
    with pytest.raises(ValueError) as e:
        hfgrab.plan(REPO, "Q9_XX", SIBS)
    assert "Q4_K_M" in str(e.value)


def test_quants_menu_sums_parts_and_drops_junk():
    q = hfgrab.quants_of(SIBS)
    assert [(x["tag"], x["files"]) for x in q] == [("Q4_K_M", 1), ("Q6_K", 1),
                                                   ("BF16", 2)]
    assert q[2]["bytes"] == 50 * 2**30


def test_the_chain_law_is_one_file_per_category_and_the_filter_never_amputates():
    chain = [
        ("split_files/diffusion_models/big_bf16.safetensors", 38 * 2**30),
        ("split_files/diffusion_models/big_fp8.safetensors", 19 * 2**30),
        ("non_official/diffusion_models/distill_fp8.safetensors", 19 * 2**30 + 1),
        ("split_files/text_encoders/te_fp8_scaled.safetensors", 8 * 2**30),
        ("split_files/text_encoders/te_bf16.safetensors", 15 * 2**30),
        ("split_files/vae/qwen_vae.safetensors", 2**28),   # no fp8 flavor!
        ("split_files/vae/README.md", 10),
    ]
    pl = hfgrab.plan("C/x", "fp8", chain)
    kept = [f["name"] for f in pl["files"]]
    assert kept == ["split_files/diffusion_models/big_fp8.safetensors",
                    "split_files/text_encoders/te_fp8_scaled.safetensors",
                    "split_files/vae/qwen_vae.safetensors"], \
        "the variant filter picked the SMALLEST match within each category " \
        "and the VAE role (no fp8 flavor) survived the filter"
    assert len(pl["pruned"]) == 3
    # bare spec: smallest per category, every role kept
    pl = hfgrab.plan("C/x", None, chain)
    assert {os.path.basename(f["name"]) for f in pl["files"]} == {
        "big_fp8.safetensors", "te_fp8_scaled.safetensors", "qwen_vae.safetensors"}
    with pytest.raises(ValueError):
        hfgrab.plan("C/x", "nope", chain)


def test_the_comfy_prior_is_the_deepest_folder(monkeypatch, tmp_path):
    d = tmp_path / "models"
    for c in ("vae", "diffusion_models", "text_encoders"):
        (d / c).mkdir(parents=True)
    monkeypatch.setattr(paths, "COMFY_MODELS", d)
    f = tmp_path / "x.safetensors"
    f.write_bytes(b"junk-not-safetensors")
    # keys inconclusive (unparseable file) -> prior = diffusion_models
    got = hfgrab._comfy_category(
        {"name": "split_files/diffusion_models/x.safetensors",
         "local": str(f)}, log=lambda m: None)
    assert got == "diffusion_models"
    with pytest.raises(RuntimeError) as e:
        hfgrab._comfy_category({"name": "root_only_file.safetensors",
                                "local": str(f)}, log=lambda m: None)
    assert "diffusion_models" in str(e.value)   # refusal names what it saw


def test_controls_are_file_granular_and_the_cache_is_the_resume(monkeypatch):
    calls = []

    def fake_dl(repo_id, filename, **kw):
        calls.append(filename)
        return f"/tmp/{filename}"

    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_dl)
    hfgrab._download("x/y", {"name": "a.gguf", "bytes": 7}, log=lambda m: None)
    assert calls == ["a.gguf"] and hfgrab._ctl["done_bytes"] >= 7
    hfgrab.set_command("cancel")
    with pytest.raises(hfgrab.GrabCancelled):
        hfgrab._download("x/y", {"name": "b.gguf", "bytes": 9},
                         log=lambda m: None)
    assert calls == ["a.gguf"], "cancel must stop NEW files, mid-file is the hub's"
    with pytest.raises(ValueError):
        hfgrab.set_command("explode")


def test_the_space_guard_refuses_before_the_first_byte(monkeypatch):
    class U:
        free = 2**30

    def fake_usage(_):
        return U()

    monkeypatch.setattr(shutil, "disk_usage", fake_usage)
    with pytest.raises(RuntimeError) as e:
        hfgrab._space_guard(100 * 2**30, log=lambda m: None)
    assert "free space" in str(e.value)


# ------------------------------------------------------------- route journeys

REPO2 = "TestOrg/test-grab-GGUF"
SIBS2 = [("Grab-Q4_K_M.gguf", 2**20), ("mmproj/mmproj-Q4.gguf", 2**15),
         ("lora/lo.gguf", 2**20)]
FILES2 = {}


def _gguf_bytes(tag):
    import io
    p = tempfile.mktemp(suffix=".gguf")
    make_gguf(p, name=f"Grab {tag}")
    data = open(p, "rb").read()
    os.unlink(p)
    return data


def _fake_cache_download(monkeypatch):
    def fake_dl(repo_id, filename, **kw):
        commit = "0123456789abcdef0123456789abcdef01234567"  # _COMMIT_RE-shaped
        # the REAL hub layout: $HF_HOME/hub/models--org--repo (hub_dir law)
        blob_root = paths.HF_CACHE / "hub" / f"models--{repo_id.replace('/', '--')}"
        snap = blob_root / "snapshots" / commit
        (blob_root / "refs").mkdir(parents=True, exist_ok=True)
        (blob_root / "refs" / "main").write_text(commit)   # the indexer reads refs
        target = snap / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() or target.is_symlink():
            return str(target)
        blob = blob_root / "blobs" / (filename.replace("/", "_") + ".main")
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(FILES2[(repo_id, filename)])
        os.symlink(os.path.relpath(blob, target.parent), target)
        return str(target)

    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_dl)
    monkeypatch.setattr(hfgrab, "_siblings", lambda repo: SIBS2)


def _wait_job(client, jid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        rows = db.query("SELECT status, log FROM jobs WHERE id=?", (jid,))
        assert rows, "job row vanished"
        if rows[0]["status"] != "running":
            return rows[0]
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_the_gguf_grab_lands_through_the_ladder(client, monkeypatch):
    """spec -> hub -> cache -> MIGRATION LADDER -> library dir, with the
    mmproj following into the SAME directory (the router's sidecar law).
    The fake download writes a real cache layout; the ladder runs for real."""
    FILES2[(REPO2, "Grab-Q4_K_M.gguf")] = _gguf_bytes("Q4_K_M")
    FILES2[(REPO2, "mmproj/mmproj-Q4.gguf")] = _gguf_bytes("mmproj")
    _fake_cache_download(monkeypatch)
    r = client.post("/api/models/grab", json={"spec": REPO2 + ":Q4_K_M"})
    assert r.status_code == 200, r.text
    job = _wait_job(client, r.json()["job"])
    assert job["status"] == "success", job["log"][-500:]
    lib = paths.LLAMA_MODELS
    dirs = [p for p in lib.rglob("Grab-Q4_K_M.gguf")]
    assert dirs, "the quant never reached the library"
    d = dirs[0].parent
    assert (d / "mmproj-Q4.gguf").is_file(), "the mmproj did not follow"
    # the ladder released the cache names (its law: refs follow, names last)
    snap = (paths.HF_CACHE / "hub" / f"models--{REPO2.replace('/', '--')}"
            / "snapshots")
    assert not list(snap.rglob("Grab-Q4_K_M.gguf")), \
        "the migrated quant's cache name survived"


def test_the_comfy_grab_places_by_tensor_keys_with_the_chain(client, monkeypatch,
                                                             tmp_path):
    d = tmp_path / "comfy-models"
    (d / "vae").mkdir(parents=True)
    (d / "diffusion_models").mkdir(parents=True)
    monkeypatch.setattr(paths, "COMFY_MODELS", d)

    def st(keys):
        p = tempfile.mktemp(suffix=".safetensors")
        make_safetensors(p, keys)
        data = open(p, "rb").read()
        os.unlink(p)
        return data

    repo = "TestOrg/test-chain"
    sibs = [("split_files/diffusion_models/m_fp8.safetensors", 2**20),
            ("split_files/diffusion_models/m_bf16.safetensors", 2**21),
            ("split_files/vae/vae.safetensors", 2**16)]
    FILES2[(repo, "split_files/diffusion_models/m_fp8.safetensors")] = st(
        ["diffusion_model.blocks.0.weight"])
    FILES2[(repo, "split_files/diffusion_models/m_bf16.safetensors")] = st(
        ["diffusion_model.blocks.0.weight"])
    FILES2[(repo, "split_files/vae/vae.safetensors")] = st(
        ["encoder.mid.a.weight", "decoder.mid.b.weight"])
    monkeypatch.setattr(hfgrab, "_siblings", lambda r: sibs)

    def fake_dl(repo_id, filename, **kw):
        p = tmp_path / "cache" / os.path.basename(filename)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(FILES2[(repo_id, filename)])
        return str(p)

    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_dl)

    r = client.post("/api/models/grab", json={"spec": repo})
    assert r.status_code == 200, r.text
    job = _wait_job(client, r.json()["job"])
    assert job["status"] == "success", job["log"][-500:]
    assert (d / "diffusion_models" / "m_fp8.safetensors").is_file()  # smallest
    assert not (d / "diffusion_models" / "m_bf16.safetensors").exists()  # pruned
    assert (d / "vae" / "vae.safetensors").is_file()
    # the cache copy stays for comfy (no ladder, cache stays truth)
    assert (tmp_path / "cache" / "vae.safetensors").is_file()


def test_grab_routes_are_guarded_and_honest(client):
    assert client.post("/api/models/grab", json={"spec": "no-slash"}
                       ).status_code == 400
    assert client.post("/api/models/grab/control",
                       json={"command": "explode"}).status_code == 400
    assert client.post("/api/models/grab/control",
                       json={"command": "pause"}).json()["command"] == "pause"
    hfgrab.set_command("running")
    anon = TestClient(app)
    assert anon.post("/api/models/grab", json={"spec": "a/b"}).status_code == 401
    assert anon.get("/api/models/quants?repo=a/b").status_code == 401
