"""The smoke test that gates a llama.cpp deploy, and how it chooses a model.

This code had no tests: every existing update test passes ``smoke=False``, so the
one step in the update path that touches real models was the one step never
exercised. It promptly failed a healthy build in production by loading an 11 MB
file that was the first shard of a 94 GB model, because that made it the
"smallest model on disk". See ``_model_candidates``.

Files are sparse (``truncate``), so a 94 GB model costs one inode.
"""
from __future__ import annotations

import os
import tempfile

# Isolation: same contract as tests/test_updates.py — never touch the real ~/ai.
os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-smoke-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402

from app.providers import llamacpp as L  # noqa: E402

GB = 1 << 30
MB = 1 << 20


def mk(path, size: int):
    """A file of `size` bytes that costs no disk (sparse)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)
    return path


@pytest.fixture
def library(tmp_path, monkeypatch):
    """An empty model library to point the selector at (selection never runs a
    server, so nothing else needs stubbing)."""
    root = tmp_path / "models"
    root.mkdir()
    monkeypatch.setattr(L.paths, "LLAMA_MODELS", root)
    return root


# ------------------------------------------------------------- what is a model

def test_a_shard_is_not_the_smallest_model(library):
    """The live failure, in miniature.

    Part 1 of a three-part model was 11 MB while parts 2 and 3 were ~48 GB each,
    so `min(size)` called it the smallest model on disk. llama.cpp reads a shard's
    header — which describes the whole model — and allocated 37 GB. A shard is a
    fraction of a model; its cost is the set, never the file.
    """
    mk(library / "Big/Big-00001-of-00003.gguf", 11 * MB)
    mk(library / "Big/Big-00002-of-00003.gguf", 48 * GB)
    mk(library / "Big/Big-00003-of-00003.gguf", 44 * GB)
    small = mk(library / "Small/Small.Q4_K_M.gguf", 500 * MB)

    cands = L._model_candidates(library)
    assert cands[0][0] == small, "a shard was chosen over a real small model"
    big = [c for c in cands if "Big" in c[2]]
    assert len(big) == 1, "the three shards must collapse into one candidate"
    assert big[0][1] == 11 * MB + 48 * GB + 44 * GB
    assert "3 parts" in big[0][2]


def test_a_shard_set_loads_from_its_first_part(library):
    """If a set is the right size to try, llama.cpp must be pointed at 00001."""
    one = mk(library / "S/S-00001-of-00002.gguf", 2 * GB)
    mk(library / "S/S-00002-of-00002.gguf", 2 * GB)
    cands = L._model_candidates(library)
    assert cands[0][0].name == one.name


def test_an_incomplete_shard_set_is_not_a_model_at_all(library):
    """Two of three parts cannot load, so offering it would blame the build."""
    mk(library / "P/P-00001-of-00003.gguf", 3 * GB)
    mk(library / "P/P-00002-of-00003.gguf", 3 * GB)
    assert L._model_candidates(library) == []


def test_projectors_and_calibration_files_are_not_models(library):
    """0.5–0.9 GB each, filed next to real models, and none loads standalone."""
    real = mk(library / "Real/Real.gguf", 4 * GB)
    for junk in ("mmproj-Real-BF16.gguf", "imatrix-Real.gguf",
                 "mtp-Real-Q4_0.gguf", "draft-Real.gguf",
                 "sub/mmproj-Other.gguf"):
        mk(library / "Real" / junk, 1 * MB)      # tiny: the wrong answer otherwise
    assert [c[0] for c in L._model_candidates(library)] == [real]


def test_a_placeholder_or_truncated_file_is_not_a_model(library):
    """An LFS pointer or a download that died early is ~100 bytes."""
    mk(library / "A/pointer.gguf", 135)
    real = mk(library / "A/real.gguf", 900 * MB)
    assert [c[0] for c in L._model_candidates(library)] == [real]


def test_candidates_come_back_smallest_first(library):
    mk(library / "Z/z.gguf", 9 * GB)
    mk(library / "B/b.gguf", 700 * MB)
    mk(library / "M/m.gguf", 3 * GB)
    assert [c[1] for c in L._model_candidates(library)] == [700 * MB, 3 * GB, 9 * GB]


def test_a_missing_directory_is_no_models(library):
    assert L._model_candidates(library / "nowhere") == []


def test_the_rule_this_replaced_chose_the_shard(library):
    """Not a new behaviour — the executable statement of the old one being wrong.

    `min(every *.gguf, key=size)` is what shipped. On the fixture above, and on
    the real machine, it returns a shard. If anyone reintroduces size-over-files,
    this is the test that says what it costs.
    """
    mk(library / "Big/Big-00001-of-00003.gguf", 11 * MB)
    mk(library / "Big/Big-00002-of-00003.gguf", 48 * GB)
    mk(library / "Big/Big-00003-of-00003.gguf", 44 * GB)
    mk(library / "Small/Small.Q4_K_M.gguf", 500 * MB)
    old_answer = min([f for f in library.rglob("*.gguf") if f.is_file()],
                     key=lambda f: f.stat().st_size)
    assert old_answer.name == "Big-00001-of-00003.gguf"
    assert L._model_candidates(library)[0][0].name == "Small.Q4_K_M.gguf"


# ------------------------------------------------------------ what fits in RAM

def test_models_that_do_not_fit_are_never_attempted(monkeypatch, tmp_path):
    """The gate runs on a live box: 12 GB free is not an invitation to load 87 GB."""
    root = tmp_path / "m"
    mk(root / "big/big.gguf", 87 * GB)
    monkeypatch.setattr(L.paths, "LLAMA_MODELS", root)
    monkeypatch.setattr(L, "_mem_available", lambda: 12 * GB)
    log: list[str] = []
    with pytest.raises(RuntimeError) as e:
        L.LlamaCpp()._smoke_worker(tmp_path, log.append)
    msg = str(e.value)
    assert "no model on disk fits" in msg
    assert "NOT" in msg and "deploy" in msg, "must say the build was not deployed"
    assert "smoke off" in msg, "must name the way out"
    assert "big (87.0 GB)" in "\n".join(log), "the skip should be visible in the log"


def test_headroom_is_not_counted_as_free(monkeypatch, tmp_path):
    """A model the size of available memory will still be the one thing that OOMs."""
    root = tmp_path / "m"
    mk(root / "edge/edge.gguf", 12 * GB - 10 * MB)     # fits, but not *fits + headroom*
    monkeypatch.setattr(L.paths, "LLAMA_MODELS", root)
    monkeypatch.setattr(L, "_mem_available", lambda: 12 * GB)
    with pytest.raises(RuntimeError) as e:
        L.LlamaCpp()._smoke_worker(tmp_path, lambda m: None)
    assert "no model on disk fits" in str(e.value)


def test_no_models_on_disk_skips_rather_than_fails(monkeypatch, tmp_path):
    """Nothing to load is not a broken build — the release still deploys."""
    monkeypatch.setattr(L.paths, "LLAMA_MODELS", tmp_path / "empty")
    L.LlamaCpp()._smoke_worker(tmp_path, lambda m: None)   # no raise


# -------------------------------------------------- whose fault is a failure?

def _stub_run(monkeypatch, results):
    """Pretend `_run_worker` returns `results` in order; record what it was given."""
    seen: list = []

    def fake(rel, model, log, label, size):
        seen.append(model.name)
        return results[len(seen) - 1] if len(seen) <= len(results) else (False, "")
    monkeypatch.setattr(L, "_run_worker", fake)
    return seen


def test_an_allocation_failure_is_not_blamed_on_the_build(monkeypatch, tmp_path):
    """`unable to allocate` is the machine saying no, not the binary being broken.

    So: fall back to a smaller candidate instead of failing the update. This is
    what the live job should have done — and it is the difference between "the
    build is bad" and "something else is holding your memory".
    """
    root = tmp_path / "m"
    mk(root / "a/a.gguf", 1 * GB)
    mk(root / "b/b.gguf", 2 * GB)
    monkeypatch.setattr(L.paths, "LLAMA_MODELS", root)
    monkeypatch.setattr(L, "_mem_available", lambda: 12 * GB)
    seen = _stub_run(monkeypatch, [
        (False, "0.21.032 E alloc_tensor_range: failed to allocate ROCm0 buffer"
                " of size 37048859392"),
        (True, ""),
    ])
    L.LlamaCpp()._smoke_worker(tmp_path, lambda m: None)
    assert len(seen) == 2, "should have tried the next candidate after OOM"


def test_a_broken_binary_fails_at_once_without_shopping_for_models(monkeypatch, tmp_path):
    """A wrong build must fail on the first candidate: trying smaller models
    cannot make a bad binary good, it only hides the reason."""
    root = tmp_path / "m"
    for n in ("a", "b", "c"):
        mk(root / n / f"{n}.gguf", 1 * GB)
    monkeypatch.setattr(L.paths, "LLAMA_MODELS", root)
    monkeypatch.setattr(L, "_mem_available", lambda: 12 * GB)
    seen = _stub_run(monkeypatch, [
        (False, "llama-server: symbol lookup error: undefined symbol: ggml_backend_init")])
    log: list[str] = []
    with pytest.raises(RuntimeError) as e:
        L.LlamaCpp()._smoke_worker(tmp_path, log.append)
    assert len(seen) == 1
    assert "did not serve /health" in str(e.value)
    assert "symbol lookup error" in "\n".join(log), "the output must reach the job log"


def test_every_candidate_out_of_memory_blames_memory(monkeypatch, tmp_path):
    root = tmp_path / "m"
    for n in ("a", "b", "c", "d"):
        mk(root / n / f"{n}.gguf", 1 * GB)
    monkeypatch.setattr(L.paths, "LLAMA_MODELS", root)
    monkeypatch.setattr(L, "_mem_available", lambda: 12 * GB)
    _stub_run(monkeypatch, [(False, "unable to allocate CUDA0 buffer")] * 4)
    with pytest.raises(RuntimeError) as e:
        L.LlamaCpp()._smoke_worker(tmp_path, lambda m: None)
    msg = str(e.value)
    assert "memory" in msg.lower()
    assert "NOT deployed" in msg
    assert "symbol" not in msg.lower(), "must not accuse the build of being broken"


def test_only_a_few_candidates_are_tried(monkeypatch, tmp_path):
    """A stuck server × 30 models would be a 90-minute update."""
    root = tmp_path / "m"
    for i in range(30):
        mk(root / f"m{i}" / f"m{i}.gguf", (i + 1) * 100 * MB)
    monkeypatch.setattr(L.paths, "LLAMA_MODELS", root)
    monkeypatch.setattr(L, "_mem_available", lambda: 12 * GB)
    seen = _stub_run(monkeypatch,
                     [(False, "failed to allocate")] * L._SMOKE_ATTEMPTS)
    with pytest.raises(RuntimeError):
        L.LlamaCpp()._smoke_worker(tmp_path, lambda m: None)
    assert len(seen) == L._SMOKE_ATTEMPTS


# ------------------------------------------------------------------ memory read

def test_the_memory_figure_comes_from_the_kernel():
    """The gate trusts this number, so check it is a real one, not a default."""
    avail = L._mem_available()
    assert avail > 100 * MB
    real = 0
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            real = int(line.split()[1]) * 1024
    assert abs(avail - real) < real * 0.2 + MB


def test_a_kernel_that_wont_say_falls_back_to_assuming_modest():
    class NoProc:
        @staticmethod
        def read_text():
            raise OSError("masked")
    orig = L.Path
    L.Path = lambda *a: NoProc()
    try:
        assert L._mem_available() == 8 * GB
    finally:
        L.Path = orig


# --------------------------------------------------- index-backed selection
# The index (§4.9 N2) orders candidates by weights + estimated KV instead of file
# size. These tests pin the two properties that matter: the index is preferred
# when it exists, and it can never block an update when it does not.

def _index_rows(monkeypatch, rows):
    """Make modelindex.fits_query return `rows`.

    Imported by name rather than reached through `L.modelindex`: same module object
    (sys.modules identity), but the target of the patch is legible in one line
    instead of depending on a re-export being visible to a static reader.
    """
    from app.core import modelindex
    assert modelindex is L.modelindex, "patching a different module than the SUT"
    monkeypatch.setattr(modelindex, "fits_query", lambda need, limit=3: rows[:limit])


def test_index_order_beats_file_size_order(monkeypatch, library, tmp_path):
    """The point of the index: a big-weights/small-cache model can be the cheaper
    bet than a small-weights model that needs a large cache. Selection follows the
    index, not the byte count."""
    big = mk(library / "BigCache" / "big.gguf", 30 << 30)
    small = mk(library / "SmallCache" / "small.gguf", 10 << 30)
    # Index says SmallCache is the cheaper overall bet.
    _index_rows(monkeypatch, [
        {"path": str(small), "bytes": 10 << 30, "id": "SmallCache",
         "shard_total": 1},
        {"path": str(big), "bytes": 30 << 30, "id": "BigCache", "shard_total": 1},
    ])
    got = L._indexed_candidates(100 << 30, lambda m: None)
    assert [p.name for p, _, _ in got] == ["small.gguf", "big.gguf"]


def test_index_can_exclude_a_model_file_size_would_have_picked(monkeypatch, library):
    """A model whose KV pushes it over the budget must not be offered even though
    its weights fit. This is the 4096-token failure mode: enough for weights, not
    for caches."""
    fits = mk(library / "Fits" / "f.gguf", 5 << 30)
    cache_bomb = mk(library / "CacheBomb" / "cb.gguf", 6 << 30)
    _index_rows(monkeypatch, [
        {"path": str(fits), "bytes": 5 << 30, "id": "Fits", "shard_total": 1},
        # fits_query applies est_total, so CacheBomb simply is not returned.
    ])
    got = L._indexed_candidates(20 << 30, lambda m: None)
    assert [p.name for p, _, _ in got] == ["f.gguf"]
    assert cache_bomb.name not in [p.name for p, _, _ in got]


def test_an_empty_index_falls_back_to_the_directory_walk(monkeypatch, library):
    """The index is an optimization. An empty or unpopulated one must send the
    smoke test back to walking the disk, never to "no models"."""
    mk(library / "Real" / "r.gguf", 3 << 30)
    _index_rows(monkeypatch, [])
    assert L._indexed_candidates(100 << 30, lambda m: None) == []
    # and the caller's fallback still finds the model
    monkeypatch.setattr(L, "_indexed_candidates", lambda avail, log: [])
    cands = L._model_candidates(library)
    assert [p.name for p, _, _ in cands] == ["r.gguf"]


def test_a_broken_index_falls_back_and_says_so(monkeypatch, library):
    """A corrupt DB, a missing table, or an import error must degrade to the walk
    with a log line — not raise, and not silently change which model is chosen
    without leaving a trace."""
    mk(library / "Real" / "r.gguf", 3 << 30)

    def boom(need, limit=3):
        raise RuntimeError("no such table: model_index")

    monkeypatch.setattr(L.modelindex, "fits_query", boom)
    notes: list[str] = []
    assert L._indexed_candidates(100 << 30, notes.append) == []
    assert any("model index unavailable" in n for n in notes), notes
    assert any("no such table" in n for n in notes), notes


def test_index_rows_pointing_at_deleted_files_are_skipped(monkeypatch, library):
    """The index is a cache of disk state and goes stale. A row whose file is gone
    must be dropped rather than handed to a worker that would fail confusingly."""
    good = mk(library / "Good" / "g.gguf", 3 << 30)
    gone = library / "Gone" / "x.gguf"
    _index_rows(monkeypatch, [
        {"path": str(gone), "bytes": 3 << 30, "id": "Gone", "shard_total": 1},
        {"path": str(good), "bytes": 3 << 30, "id": "Good", "shard_total": 1},
    ])
    got = L._indexed_candidates(100 << 30, lambda m: None)
    assert [p.name for p, _, _ in got] == ["g.gguf"]


def test_no_headroom_left_is_no_candidates(monkeypatch, library):
    """If avail is already at or below the headroom there is nothing to run in.
    Returning [] defers to the walk rather than offering a zero-budget model."""
    mk(library / "Any" / "a.gguf", 1 << 30)
    _index_rows(monkeypatch, [{"path": "x", "bytes": 0, "id": "x", "shard_total": 1}])
    assert L._indexed_candidates(L._SMOKE_HEADROOM, lambda m: None) == []


def test_shard_count_reaches_the_log_label(monkeypatch, library):
    """'3 parts' in the smoke log is what tells a reader the whole model was
    considered, which is the bug this whole path exists to prevent."""
    p = mk(library / "Sharded" / "s-00001-of-00003.gguf", 3 << 30)
    _index_rows(monkeypatch, [{"path": str(p), "bytes": 9 << 30, "id": "Sharded",
                               "shard_total": 3}])
    _, _, label = L._indexed_candidates(100 << 30, lambda m: None)[0]
    assert "3 parts" in label
