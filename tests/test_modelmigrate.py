"""The cache→library migration law, enforced (PORTAL_PLAN §6.14.1zk).

The hub cache is seeded the way `hf download` lays it out — blobs plus
snapshot symlinks — because the whole shared-file design rides on inodes:
hardlinks as the sharing ledger, blob release only for the last user, and
"never delete what the destination has not verified". Fixtures live under
`paths.HF_CACHE` so `modelindex.hub_dir()` and the no-arg `rescan()` inside
`apply()` find them in the sandbox home. Each test seeds its OWN repo: the
suite home is shared, and a directory claimed by an earlier plan silently
pushes a later plan down the collision chain.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from app.core import db, model_config, modelindex, modelmigrate as mm, paths
from tests.gguf_fixture import _write_fake_gguf

_KV = {"block_count": 8, "attention.head_count": 4, "attention.head_count_kv": 2,
       "context_length": 4096}
_REV = "a" * 40
_REV_OLD = "b" * 40


@pytest.fixture(autouse=True)
def _hub_isolation(monkeypatch, tmp_path):
    """Every migration test gets its OWN hub AND its own library root. The
    suite home is shared; fixture repos under paths.HF_CACHE and fixture
    dirs under paths.LLAMA_MODELS leak into every later module's scans —
    measured as full-suite failures (personality routes deck, strips, api
    lists) before isolation. The teardown is the other half: rows in the
    shared DB (index rows pointing at the destroyed tmp dir, the test's
    own personality and config) are the residue later modules see."""
    hub = tmp_path / "hub"
    hub.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(modelindex, "hub_dir", lambda root=None: hub)
    lib = tmp_path / "llama"
    lib.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths, "LLAMA_MODELS", lib)
    _HUB["p"] = hub
    _LIB["p"] = lib
    yield
    conn = db._conn()
    p = str(tmp_path)
    conn.execute("DELETE FROM model_index WHERE path LIKE ?", (p + "%",))
    conn.execute("DELETE FROM models WHERE dir LIKE ?", (p + "%",))
    conn.execute("DELETE FROM model_files WHERE path LIKE ?", (p + "%",))
    conn.execute("DELETE FROM personalities WHERE name LIKE 'mig-%'")
    conn.execute("DELETE FROM model_configs WHERE name LIKE 'mig-%'")
    conn.commit()


_HUB: dict = {}
_LIB: dict = {}


def hub_root() -> Path:
    return _HUB["p"]


def seed_repo(hub: Path, repo: str, files: dict[str, str],
              revision: str = _REV, refs: bool = True) -> Path:
    """files: name → revision owning its blob (shared revisions share blobs).

    Real HF layout: content-addressed blobs, snapshots hold symlinks. The
    migration rules (hardlink, last-user, rollback) are all inode rules, so a
    fixture of plain files would let the sharing law pass vacuously.
    """
    folder = hub / f"models--{repo.replace('/', '--')}"
    blobs, snap = folder / "blobs", folder / "snapshots" / revision
    blobs.mkdir(parents=True, exist_ok=True)
    snap.mkdir(parents=True, exist_ok=True)
    for name, rev in files.items():
        if rev != revision:
            continue        # caller wires shared-revision links by hand
        p = snap / name
        _write_fake_gguf(p, _KV, pad=len(name))
        data = p.read_bytes()
        blob = blobs / hashlib.sha1(data).hexdigest()
        p.unlink()
        if not blob.exists():
            blob.write_bytes(data)
        os.symlink(os.path.relpath(blob, snap), p)
    if refs:
        (folder / "refs").mkdir(parents=True, exist_ok=True)
        (folder / "refs" / "main").write_text(revision + "\n")
    return snap


def seed(hub: Path, repo: str, names, revision: str = _REV) -> Path:
    return seed_repo(hub, repo, {n: revision for n in names}, revision)


def index_all(hub: Path) -> None:
    modelindex.rescan(cache_dir=hub)


def rows_by_id() -> dict[str, dict]:
    return {str(r["id"]): r for r in modelindex.rows()}


# ---------------------------------------------------------------- naming

def test_one_directory_per_quant_named_base_dash_quant(tmp_path):
    """The depth law made manifest: llama.cpp gives a directory ONE model id
    (the last .gguf iterated wins), so quants live in sibling directories,
    grouped lexically — the owner's gather-by-quant intent, depth-1 honest."""
    hub = hub_root()
    seed(hub, "org/Muse-GGUF", ["muse-Q4_K_M.gguf", "muse-Q8_0.gguf"])
    index_all(hub)
    p4 = mm.plan_for("org/Muse-GGUF", "Q4_K_M")
    p8 = mm.plan_for("org/Muse-GGUF", "Q8_0")
    assert p4["dest_name"] == "muse-Q4_K_M"
    assert p8["dest_name"] == "muse-Q8_0"
    assert not p4["problems"]


def test_the_base_name_failover_chain_ends_in_unknown():
    """Metadata → filename stem minus quant/shard tags → repo → the literal Unknown."""
    assert mm.derive_base({"name": "DeepSeek R1"}, "x-Q4_K_M.gguf", "org/r") \
        == "DeepSeek-R1"
    assert mm.derive_base({}, "Some Model-Q4_K_M.gguf", "org/r") == "Some-Model"
    assert mm.derive_base({}, "muse-Q4_K_M-00001-of-00002.gguf", "org/r") \
        == "muse"
    assert mm.derive_base({}, "Q4_K_M.gguf", "org/ManniX-ITA") == "ManniX-ITA"
    assert mm.derive_base({}, "Q4_K_M.gguf", "Q4_K_M") == "Unknown"


def test_the_collision_chain_is_deterministic(tmp_path):
    """Base-QUANT taken → org-Base-QUANT → hash-suffixed. PK collisions are
    silent overwrites (INSERT OR REPLACE), so the chain must never reuse."""
    taken = {modelindex.router_key("muse-Q4_K_M"),
             modelindex.router_key("org-muse-Q4_K_M")}
    d = mm.dir_name_for("org/Muse-GGUF", {}, "muse-Q4_K_M.gguf", taken)
    assert d.startswith("muse-Q4_K_M-") and len(d.split("-")[-1]) == 6
    same = mm.dir_name_for("org/Muse-GGUF", {}, "muse-Q4_K_M.gguf", taken)
    assert same == d, "re-planning must name the same directory"


def test_draft_files_are_renamed_onto_the_router_prefix():
    """`draft-*.gguf` is NOT a sidecar to llama.cpp (common/preset.cpp:370:
    mtp-/dspark-/dflash- only) — it hijacks the model slot. The migration
    renames it onto the visible prefix."""
    assert mm.file_role("draft-Foo.gguf") == "draft-rename"
    assert mm.migrate_name("draft-Foo.gguf", "draft-rename") == "mtp-Foo.gguf"
    assert mm.file_role("mtp-Foo.gguf") == "draft"
    assert mm.file_role("mmproj-Foo.gguf") == "mmproj"
    assert mm.file_role("model-Q4_K_M.gguf") == "model"


# ---------------------------------------------------------------- apply

def test_apply_lands_every_file_shared_sidecars_hardlinked(tmp_path):
    hub = hub_root()
    seed(hub, "org/Muse-GGUF", ["muse-Q4_K_M.gguf", "muse-Q8_0.gguf",
                                "mmproj-muse-f16.gguf", "draft-muse-Q8_0.gguf"])
    index_all(hub)
    plan = mm.plan_for("org/Muse-GGUF", "Q4_K_M")
    mm.apply(plan)
    dest = plan["dest"]
    assert sorted(p.name for p in dest.iterdir()) == \
        ["mmproj-muse-f16.gguf", "mtp-muse-Q8_0.gguf", "muse-Q4_K_M.gguf"]
    assert not list(paths.LLAMA_MODELS.rglob(".mig-*"))
    blob_dir = hub / "models--org--Muse-GGUF" / "blobs"
    for f in plan["files"]:
        assert (dest / f["name"]).stat().st_ino == Path(f["src"]).stat().st_ino, \
            f"{f['name']}: hardlinks share the blob inode — zero bytes, router-visible"
    assert len(list(blob_dir.glob("*"))) == 3, \
        "the migrated main's blob name is released; the three names Q8 still uses stay"
    rows = rows_by_id()
    assert "muse-Q4_K_M" in rows and rows["muse-Q4_K_M"]["source"] == "library"
    modelindex.rescan(cache_dir=hub)   # production's next scan prunes the cache row
    rows = rows_by_id()
    assert "org/Muse-GGUF:Q4_K_M" not in rows, "the cache row retired with its links"
    assert "org/Muse-GGUF:Q8_0" in rows, "the sibling quant the button did not press stays in cache"
    q8 = Path(rows["org/Muse-GGUF:Q8_0"]["path"])
    assert q8.is_symlink() and q8.is_file(), "Q8's snapshot link untouched"
    assert (dest.parent / "muse-Q8_0").exists() is False


def test_sidecar_links_stay_while_a_cache_sibling_still_uses_them(tmp_path):
    """The owner's shared-file law in its cache half: the sidecar NAME is a
    shared name — the snapshot link stays while any cached quant of the repo
    leans on it. The migrated main's blob is released."""
    hub = hub_root()
    snap = seed(hub, "org/Dual-GGUF", ["d-Q4_K_M.gguf", "d-Q8_0.gguf",
                                       "mmproj-d-f16.gguf"])
    index_all(hub)
    plan = mm.plan_for("org/Dual-GGUF", "Q4_K_M")
    mm.apply(plan)
    main_link = snap / "d-Q4_K_M.gguf"
    side_link = snap / "mmproj-d-f16.gguf"
    assert not main_link.exists(), "the migrated quant's main link goes"
    assert side_link.exists(), "Q8 is still in cache: the sidecar name stays"


def test_apply_releases_the_blob_only_for_the_last_user(tmp_path):
    """Two revisions share one blob. Migrating one leaves the blob (the other
    snapshot still names it); the migrated snapshot link goes."""
    hub = hub_root()
    seed_repo(hub, "org/Muse2-GGUF", {"muse-Q4_K_M.gguf": _REV},
              revision=_REV)
    q4 = hub / "models--org--Muse2-GGUF/snapshots" / _REV / "muse-Q4_K_M.gguf"
    old = hub / "models--org--Muse2-GGUF/snapshots" / _REV_OLD
    old.mkdir(parents=True, exist_ok=True)
    os.symlink(os.path.relpath(q4.resolve(), old), old / "muse-Q8_0.gguf")
    index_all(hub)
    plan = mm.plan_for("org/Muse2-GGUF", "Q4_K_M")
    mm.apply(plan)
    blob_dir = hub / "models--org--Muse2-GGUF" / "blobs"
    assert list(blob_dir.glob("*")), "the old revision still uses this blob"
    assert not q4.exists()
    assert (old / "muse-Q8_0.gguf").is_file()


def test_existing_destination_newer_wins_never_assume_skip(tmp_path):
    hub = hub_root()
    seed(hub, "org/Keep-GGUF", ["keep-Q4_K_M.gguf"])
    index_all(hub)
    plan = mm.plan_for("org/Keep-GGUF", "Q4_K_M")
    dest = plan["dest"]
    dest.mkdir(parents=True)
    keep = dest / "keep-Q4_K_M.gguf"
    _write_fake_gguf(keep, _KV, pad=16)          # same content ⇒ same size
    os.utime(keep, (time.time() + 60, time.time() + 60))
    plan = mm.plan_for("org/Keep-GGUF", "Q4_K_M")
    by = {f["name"]: f for f in plan["files"]}
    assert by["keep-Q4_K_M.gguf"]["exists"] == "keep"
    before = keep.stat().st_mtime
    mm.apply(plan)
    assert keep.stat().st_mtime == before, "valid, same size, newer: kept"
    assert not list(dest.rglob(".mig-*"))


def test_existing_destination_invalid_is_replaced(tmp_path):
    hub = hub_root()
    seed(hub, "org/Broken-GGUF", ["broken-Q4_K_M.gguf"])
    index_all(hub)
    plan = mm.plan_for("org/Broken-GGUF", "Q4_K_M")
    dest = plan["dest"]
    dest.mkdir(parents=True)
    (dest / "broken-Q4_K_M.gguf").write_bytes(b"not gguf")
    plan = mm.plan_for("org/Broken-GGUF", "Q4_K_M")
    by = {f["name"]: f for f in plan["files"]}
    assert by["broken-Q4_K_M.gguf"]["exists"] == "replace"
    mm.apply(plan)
    assert (dest / "broken-Q4_K_M.gguf").read_bytes(
    )[:4] == b"GGUF", "exists-rule: an invalid file is replaced, not skipped"


def test_apply_rolls_back_its_own_names_and_leaves_the_cache_whole(tmp_path):
    hub = hub_root()
    seed(hub, "org/RB-GGUF", ["rb-Q4_K_M.gguf"])
    index_all(hub)
    link = (hub / "models--org--RB-GGUF" / "snapshots" / _REV / "rb-Q4_K_M.gguf")
    monkey_final = mm._land

    def kaboom(f, tmp, final, stamp, replaced, landed, log):
        raise RuntimeError("simulated mid-apply failure")

    mm._land = kaboom
    try:
        with pytest.raises(RuntimeError):
            mm.apply(mm.plan_for("org/RB-GGUF", "Q4_K_M"))
    finally:
        mm._land = monkey_final
    dest = paths.LLAMA_MODELS / "rb-Q4_K_M"
    assert not dest.exists() or not list(dest.iterdir()), \
        "the rollback must leave no names behind"
    assert link.exists(), "the cache is untouched — deletion is the LAST phase"
    assert "org/RB-GGUF:Q4_K_M" in rows_by_id(), "the cache row survives a rollback"


def test_personality_and_config_references_follow_the_move(tmp_path):
    hub = hub_root()
    seed(hub, "org/Qwopus-GGUF", ["qwopus-Q8_0.gguf"])
    index_all(hub)
    db.execute("INSERT INTO personalities (name, models_max, models, global, "
               "created_at, updated_at) VALUES ('mig-safe', 1, ?, '{}', ?, ?)",
               (json.dumps(["org/Qwopus-GGUF:Q8_0", "other"]), time.time(),
                time.time()))
    model_config.save("mig-Qwopus-fast", "org/Qwopus-GGUF:Q8_0", {"temp": "0.6"})
    mm.apply(mm.plan_for("org/Qwopus-GGUF", "Q8_0"))
    ids = json.loads(db.query("SELECT models FROM personalities WHERE "
                              "name='mig-safe'")[0]["models"])
    assert ids == ["qwopus-Q8_0", "other"], "the cache id becomes the dir id"
    assert model_config.get("mig-Qwopus-fast")["base_model"] == "qwopus-Q8_0"


def test_the_placement_ledger_gains_the_new_rows(tmp_path):
    hub = hub_root()
    seed(hub, "org/Ledger-GGUF", ["led-Q4_K_M.gguf", "mmproj-led-f16.gguf"])
    index_all(hub)
    plan = mm.plan_for("org/Ledger-GGUF", "Q4_K_M")
    mm.apply(plan)
    dirs = [r["dir"] for r in db.query(
        "SELECT dir FROM models WHERE id='led-Q4_K_M'")]
    assert dirs == [str(plan["dest"])]
    assert db.query("SELECT path FROM model_files WHERE model='led-Q4_K_M'")


def test_shards_land_together_in_one_directory(tmp_path):
    hub = hub_root()
    seed(hub, "org/Shard-GGUF", ["shd-Q4_K_M-00001-of-00002.gguf",
                                 "shd-Q4_K_M-00002-of-00002.gguf"])
    index_all(hub)
    plan = mm.plan_for("org/Shard-GGUF", "Q4_K_M")
    assert plan["dest_name"] == "shd-Q4_K_M", "the shard marker does not reach the dir name"
    mm.apply(plan)
    assert len(list(plan["dest"].glob("*.gguf"))) == 2
    assert rows_by_id()["shd-Q4_K_M"]["shard_total"] == 2


# -------------------------------------------------------------- refusals

def test_partial_downloads_are_refused_never_migrated(tmp_path):
    """The owner's warning: metadata testing left partial models in the cache.
    A snapshot whose blob is gone has no index row (the scan reports the
    problem, not a model); a zero-byte blob refuses its plan."""
    hub = hub_root()
    seed(hub, "org/Partial-GGUF", ["part-Q4_K_M.gguf"])
    for b in (hub / "models--org--Partial-GGUF" / "blobs").glob("*"):
        b.unlink()
    index_all(hub)
    with pytest.raises(Exception):
        mm.apply(mm.plan_for("org/Partial-GGUF", "Q4_K_M"))

    hub2 = tmp_path / "hub2"
    seed(hub2, "org/Zero-GGUF", ["zero-Q4_K_M.gguf"])
    for b in (hub2 / "models--org--Zero-GGUF" / "blobs").glob("*"):
        b.write_bytes(b"")
    res = modelindex.rescan(cache_dir=hub2)
    assert "org/Zero-GGUF:Q4_K_M" not in rows_by_id(), \
        "a zero-byte blob is not a model — the index reports it, never lists it"
    with pytest.raises(Exception):
        mm.apply(mm.plan_for("org/Zero-GGUF", "Q4_K_M"))


def test_a_loaded_router_model_refuses_to_move_by_id(tmp_path, monkeypatch):
    """The router spawns cache workers with --hf-repo, not --model (measured):
    the file path never appears in the args, so the loaded ID is the binding
    — and it is exactly the key a plan is written under. This is the law
    that keeps the resident model (the one answering you) under its worker."""
    hub = hub_root()
    seed(hub, "org/Swift-GGUF", ["swift-IQ3_S.gguf"])
    index_all(hub)
    monkeypatch.setattr(mm, "usage_guard",
                        lambda: {"id:org/Swift-GGUF:IQ3_S":
                                 "router model (loaded)"})
    with pytest.raises(Exception, match="loaded on the router"):
        mm.plan_for("org/Swift-GGUF", "IQ3_S")
    flags = mm.migratable()
    assert flags["org/Swift-GGUF:IQ3_S"]["ok"] is False
    assert "loaded on the router" in flags["org/Swift-GGUF:IQ3_S"]["reason"]


def test_files_in_live_service_use_refuse_to_move(tmp_path, monkeypatch):
    """The RAG sidecar law generalized: no file named by a running unit's
    ExecStart (or the router's loaded model args) ever moves."""
    hub = hub_root()
    seed(hub, "org/Busy-GGUF", ["busy-Q4_K_M.gguf"])
    index_all(hub)
    src = (hub / "models--org--Busy-GGUF" / "snapshots" / _REV
           / "busy-Q4_K_M.gguf").resolve()
    monkeypatch.setattr(mm, "usage_guard",
                        lambda: {str(src): "ai-embed.service ExecStart"})
    plan = mm.plan_for("org/Busy-GGUF", "Q4_K_M")
    assert any("in use by ai-embed.service" in p for p in plan["problems"])
    with pytest.raises(Exception):
        mm.apply(plan)


def test_usage_guard_reads_installed_unit_files(tmp_path, monkeypatch):
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "ai-embed.service").write_text(
        "[Service]\nExecStart=/bin/llama-server --model "
        "/home/cortana/ai/models/sidecar/embeddinggemma-2-Q8_0.gguf\n")
    from pathlib import Path as P
    monkeypatch.setattr(P, "home", classmethod(lambda cls: tmp_path))
    used = mm.usage_guard()
    assert any("embeddinggemma" in p for p in used), \
        "the RAG sidecar's model must be on the refuse-to-move list"


def test_the_button_flag_only_shows_for_migratable_rows(tmp_path, monkeypatch):
    hub = hub_root()
    seed(hub, "org/Btn-GGUF", ["btn-Q4_K_M.gguf"])
    seed(hub, "org/BtnBusy-GGUF", ["btb-Q4_K_M.gguf"])
    index_all(hub)
    busy = (hub / "models--org--BtnBusy-GGUF" / "snapshots" / _REV
            / "btb-Q4_K_M.gguf").resolve()
    monkeypatch.setattr(mm, "usage_guard", lambda: {str(busy): "router model x"})
    flags = mm.migratable()
    assert flags["org/Btn-GGUF:Q4_K_M"]["ok"] is True
    assert flags["org/Btn-GGUF:Q4_K_M"]["dir"] == "btn-Q4_K_M"
    assert flags["org/BtnBusy-GGUF:Q4_K_M"]["ok"] is False
    assert flags["org/BtnBusy-GGUF:Q4_K_M"]["reason"]


def test_preflight_refuses_a_destination_outside_the_library(tmp_path):
    hub = hub_root()
    seed(hub, "org/PF-GGUF", ["pf-Q4_K_M.gguf"])
    index_all(hub)
    plan = mm.plan_for("org/PF-GGUF", "Q4_K_M")
    plan["dest"] = paths.HOME / "ai/models/npu/evil"
    assert any("escapes the library" in p for p in mm.preflight(plan))
