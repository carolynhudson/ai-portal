"""modeladd: source parsing + offline fetch/placement/indexing.

The fetch tests use local files (and, for the HF path, a monkeypatched
huggingface_hub) so the suite stays deterministic and network-free.
"""
import json
import os
import struct
import tempfile
import time

# Isolation (same contract as the other test files): never touch the real ~/ai.
os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-ma-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import db, modeladd, paths  # noqa: E402
from app.main import app  # noqa: E402

db.init()  # idempotent; direct add_model() tests don't go through a login


# ------------------------------------------------------------------ fixtures

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
    with open(path, "wb") as f:
        f.write(b"GGUF" + b"\x00\x00\x00\x03" + struct.pack(">QQ", 1, 3)
                + b"".join(kvs) + b"\x00" * 32)


def make_safetensors(path, keys, metadata=None):
    hdr = {k: {"dtype": "F32", "shape": [1, 4]} for k in keys}
    if metadata:
        hdr["__metadata__"] = metadata
    hb = json.dumps(hdr).encode()
    pad = (8 - len(hb) % 8) % 8
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(hb) + pad) + hb + b" " * pad + b"\x00" * 8)


@pytest.fixture
def scratch():
    d = tempfile.mkdtemp(prefix="aipp-ma-fx-")
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password-123"})
    yield c


def wait_job(jid, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = db.query("SELECT status, log FROM jobs WHERE id=?", (jid,))
        assert j, "job row vanished"
        if j[0][0] != "running":
            return j[0]
        time.sleep(0.2)
    raise AssertionError("job did not finish in time")


# --------------------------------------------------------------- parse_source

def test_parse_hf_cli_with_file():
    s = modeladd.parse_source(
        hf_cli="huggingface-cli download unsloth/Qwen3-8B-GGUF Q4_K_M.gguf "
               "--local-dir /tmp/x")
    assert s["kind"] == "hf"
    assert s["repo_id"] == "unsloth/Qwen3-8B-GGUF"
    assert s["filename"] == "Q4_K_M.gguf"


def test_parse_hf_cli_bare_repo():
    s = modeladd.parse_source(hf_cli="hf download org/repo-name")
    assert s["kind"] == "hf"
    assert s["repo_id"] == "org/repo-name"
    assert s["filename"] is None


def test_parse_hf_repo():
    s = modeladd.parse_source(hf_repo="org/repo")
    assert s["kind"] == "hf" and s["repo_id"] == "org/repo"


def test_parse_url_and_filename():
    s = modeladd.parse_source(url="https://huggingface.co/a/b/resolve/main/w.bin")
    assert s["kind"] == "url"
    assert s["filename"] == "w.bin"


def test_parse_local_path(scratch):
    p = f"{scratch}/local.gguf"
    make_gguf(p)
    s = modeladd.parse_source(path=p)
    assert s["kind"] == "local"
    assert s["filename"] == "local.gguf"


def test_parse_rejects_multiple_and_none():
    with pytest.raises(ValueError):
        modeladd.parse_source(url="https://x/y.gguf", path="/tmp/a")
    with pytest.raises(ValueError):
        modeladd.parse_source()


def test_parse_rejects_bad_values(scratch):
    with pytest.raises(ValueError):
        modeladd.parse_source(hf_cli="git clone org/repo")
    with pytest.raises(ValueError):
        modeladd.parse_source(hf_repo="no-slash-here")
    with pytest.raises(ValueError):
        modeladd.parse_source(url="ftp://x/y.gguf")
    with pytest.raises(ValueError):
        modeladd.parse_source(path=f"{scratch}/missing.gguf")
    with pytest.raises(ValueError):
        modeladd.parse_source(url="https://x/")


# ------------------------------------------------------------------ placement

def test_add_gguf_local_places_and_indexes(scratch):
    src = f"{scratch}/Qwen3-8B-Q4_K_M.gguf"
    make_gguf(src, name="Qwen3 8B")
    res = modeladd.add_model(modeladd.parse_source(path=src),
                             log=lambda m: None)
    d = paths.LLAMA_MODELS / "Qwen3-8B-Q4_K_M"
    assert res["model"] == "Qwen3-8B-Q4_K_M"
    assert (d / "Qwen3-8B-Q4_K_M.gguf").is_file()
    row = db.query("SELECT role_files, size_bytes FROM models WHERE id=?",
                   ("Qwen3-8B-Q4_K_M",))[0]
    info = json.loads(row[0])
    assert info["role"] == "llama"
    assert info["files"] == ["Qwen3-8B-Q4_K_M.gguf"]
    frow = db.query("SELECT sha256, role FROM model_files WHERE model=?",
                    ("Qwen3-8B-Q4_K_M",))[0]
    assert frow[0] and len(frow[0]) == 64
    assert frow[1] == "gguf"
    # staging is cleaned up (the job's own staging dir must be gone)
    assert not (paths.STATE / "downloads" / f"add-{os.getpid()}").exists()


def test_reindex_keeps_other_files_and_reads_their_metadata(scratch):
    """Adding the second file to a model dir re-indexes the whole dir, and the
    files that were already there are *read back* from the DB rather than
    re-hashed (a 270 GB gguf is not re-sniffed for a sibling).

    That read-back path is one missing `import json` away from a NameError, so
    this test exists: two files in the directory, and the stored meta of the
    first must survive the second's arrival.
    """
    first = f"{scratch}/DeepSeek-R1-Q4_K_M.gguf"
    make_gguf(first, name="DeepSeek R1")
    modeladd.add_model(modeladd.parse_source(path=first), log=lambda m: None)
    d = paths.LLAMA_MODELS / "DeepSeek-R1-Q4_K_M"
    (d / "other-split-00001-of-00002.gguf").write_bytes(b"\0" * 512)

    modeladd._index_llama_dir(d, d / "other-split-00001-of-00002.gguf",
                              "0" * 64, {"general.name": "other"})

    rows = {r[0]: r for r in db.query(
        "SELECT path, sha256, meta FROM model_files WHERE model=?",
        ("DeepSeek-R1-Q4_K_M",))}
    assert len(rows) == 2, "one of the two files lost its row"
    kept = [r for pth, r in rows.items() if pth.endswith("Q4_K_M.gguf")][0]
    assert len(kept["sha256"] or "") == 64, "pre-existing hash was dropped"
    assert json.loads(kept["meta"])["name"] == "DeepSeek R1"


def test_add_mmproj_nests_under_parent(scratch):
    parent = paths.LLAMA_MODELS / "Qwen3.6-35B-A3B-Q4_K_M"
    parent.mkdir(parents=True, exist_ok=True)
    (parent / "Qwen3.6-35B-A3B-Q4_K_M.gguf").write_bytes(b"\x00" * 16)
    src = f"{scratch}/mmproj-Qwen3.6-35B-A3B-BF16.gguf"
    make_gguf(src)
    res = modeladd.add_model(modeladd.parse_source(path=src),
                             log=lambda m: None)
    assert res["model"] == "Qwen3.6-35B-A3B-Q4_K_M"
    assert (parent / "mmproj-Qwen3.6-35B-A3B-BF16.gguf").is_file()
    frow = db.query("SELECT role FROM model_files WHERE model=? "
                    "AND path LIKE '%mmproj%'",
                    ("Qwen3.6-35B-A3B-Q4_K_M",))[0]
    assert frow[0] == "mmproj"


def test_add_safetensors_auto_category(scratch):
    src = f"{scratch}/t5xxl.safetensors"
    make_safetensors(src, ["cond_stage_model.transformer.text_model.x.weight"])
    res = modeladd.add_model(modeladd.parse_source(path=src),
                             log=lambda m: None)
    assert res["model"] == "comfyui/clip/t5xxl.safetensors"
    assert (paths.COMFY_MODELS / "clip" / "t5xxl.safetensors").is_file()


def test_add_safetensors_unclassifiable_requires_category(scratch):
    src = f"{scratch}/mystery.safetensors"
    make_safetensors(src, ["mystery.tensor.weight"])
    with pytest.raises(ValueError, match="category"):
        modeladd.add_model(modeladd.parse_source(path=src), log=lambda m: None)
    # explicit category overrides
    res = modeladd.add_model(modeladd.parse_source(path=src),
                             category="checkpoints", log=lambda m: None)
    assert res["model"] == "comfyui/checkpoints/mystery.safetensors"


def test_add_rejects_unknown_format(scratch):
    src = f"{scratch}/junk.gguf"
    with open(src, "wb") as f:
        f.write(os.urandom(256))
    with pytest.raises(ValueError, match="unrecognized"):
        modeladd.add_model(modeladd.parse_source(path=src), log=lambda m: None)


def test_add_gguf_refuses_clobber(scratch):
    src = f"{scratch}/Clobber-Model-Q4_K_M.gguf"
    make_gguf(src)
    modeladd.add_model(modeladd.parse_source(path=src), log=lambda m: None)
    with pytest.raises(ValueError, match="already exists"):
        modeladd.add_model(modeladd.parse_source(path=src),
                           log=lambda m: None)


# ------------------------------------------------------------------ HF (mock)

def test_add_hf_picks_single_weight_file(scratch, monkeypatch):
    hf = pytest.importorskip("huggingface_hub")

    def fake_list(repo_id, repo_type="model"):
        return ["README.md", "Q4_K_M.gguf"]

    def fake_download(repo_id, filename, local_dir):
        dest = os.path.join(local_dir, filename)
        make_gguf(dest, name="HF Model")
        return dest

    monkeypatch.setattr(hf, "list_repo_files", fake_list)
    monkeypatch.setattr(hf, "hf_hub_download", fake_download)
    res = modeladd.add_model(
        modeladd.parse_source(hf_cli="hf download org/repo"), log=lambda m: None)
    assert res["model"] == "Q4_K_M"
    assert (paths.LLAMA_MODELS / "Q4_K_M" / "Q4_K_M.gguf").is_file()


def test_add_hf_ambiguous_lists_files(monkeypatch):
    hf = pytest.importorskip("huggingface_hub")
    monkeypatch.setattr(hf, "list_repo_files",
                        lambda repo_id, repo_type="model":
                        ["Q4_K_M.gguf", "Q8_0.gguf"])
    with pytest.raises(ValueError, match="several weight files"):
        modeladd.add_model(modeladd.parse_source(hf_repo="org/repo"),
                           log=lambda m: None)


# ------------------------------------------------------------------- endpoint

def test_endpoint_add_local_gguf(client, scratch):
    src = f"{scratch}/Endpoint-Model-Q8_0.gguf"
    make_gguf(src, name="Endpoint Model")
    r = client.post("/api/models/add",
                    json={"path": src})
    assert r.status_code == 200, r.text
    jid = r.json()["job"]
    status, log = wait_job(jid)
    assert status == "success", log
    assert (paths.LLAMA_MODELS / "Endpoint-Model-Q8_0"
            / "Endpoint-Model-Q8_0.gguf").is_file()
    # and it's visible through the job API the UI polls
    jr = client.get(f"/api/jobs/{jid}")
    assert jr.status_code == 200
    assert jr.json()["status"] == "success"


def test_endpoint_add_rejects_bad_input(client):
    r = client.post("/api/models/add", json={})
    assert r.status_code == 400
    r = client.post("/api/models/add", json={"url": "https://x",
                                             "hf_repo": "org/repo"})
    assert r.status_code == 400


def test_endpoint_unauthenticated_rejected():
    c = TestClient(app)
    r = c.post("/api/models/add", json={"hf_repo": "org/repo"})
    assert r.status_code == 401
