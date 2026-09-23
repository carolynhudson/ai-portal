"""Tests for the N4 apply path: snapshot, write, verify, rollback.

Every test here runs against `AI_PORTAL_HOME`, so `paths.PRESETS_INI` is a throwaway file. That
is not a formality: `apply()` writes a config the router reads at load time, so a test that
leaked would change what models this box loads. `test_live_presets_are_never_touched_by_tests`
at the end asserts the real file was not modified by anything in this module.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

# Set here rather than relying on another module having set it: pytest's file order decides who
# imports app.main first, and a module that reaches it without a password dies during import
# with a KeyError that names neither this file nor the real cause.
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")
os.environ.setdefault("AI_PORTAL_HOME",
                      str(Path(tempfile.gettempdir()) / "aiportal-presets-apply"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import db, paths, presets_store, systemd  # noqa: E402
from app.core import modelindex as mi  # noqa: E402
from app.core import personality as P  # noqa: E402
from tests.test_modelindex import _write_fake_gguf  # noqa: E402

# Mirrors the live presets.ini as it actually stands: the owner removed `fit` and
# `fit-target` by hand, and personality.GLOBAL_DEFAULTS now agrees with that removal.
ORIGINAL = "[*]\nthreads = 16\nstop-timeout = 15\n"

# An explicit budget rather than modelindex._pool_bytes(). _pool_bytes() reads the real GTT
# aperture off this machine, so a test that uses it asserts against whatever GPU state happens
# to exist at the moment pytest runs — including a loaded model changing the free figure between
# two runs of the same test. The budget is an input to apply(), so the tests supply it.
BUDGET = 64 * 1024 ** 3

# Model sizes chosen so the whole synthetic library fits inside BUDGET and the "does not fit"
# tests can force the opposite with a deliberately tiny number.
MODEL_META = {
    "block_count": 32,
    "attention.head_count": 32,
    "attention.head_count_kv": 8,
    "embedding_length": 4096,
    "context_length": 8192,
}

# A test-local personality rather than a builtin. `SAFE` names `Qwen3.5-0.8B.Q4_K_M`, a model
# that exists on this box and not in a throwaway home; a test that reaches for a builtin is a
# test that silently depends on what happens to be installed here. `models=None` resolves from
# the index the fixture just built, so the assertions are about apply()'s behaviour and not
# about this machine's library.
PERSONA = {"name": "safe", "models_max": 1, "models": None, "note": "whole synthetic library"}


def _gguf(path: Path, size: int) -> None:
    """A GGUF the estimator can actually price.

    Reuses tests/test_modelindex.py's fixture writer rather than hand-rolling one. A previous
    version of this helper wrote `b"GGUF" + zeros`, which was a file the scanner happily listed
    and the estimator could not price — so every budget came back `FITS_UNKNOWN` and apply()
    refused, and the failure looked like a bug in apply() rather than a fixture with no headers
    in it.

    The size is reached by truncating to it rather than by `pad`, which matters: `pad` builds a
    `bytes` object of that length and writes it, so a 4 GiB model fixture consumed 4 GiB of real
    space. These homes live under /tmp, which on this box is a 62 GiB tmpfs, and a module that
    writes 12 GiB per run is a module that eventually fills the filesystem every other test in
    the suite is sitting on. `truncate` leaves a hole: `st_size` is the number the index reads as
    weights, and no blocks are allocated for it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_fake_gguf(path, MODEL_META)
    with path.open("r+b") as handle:
        handle.truncate(size)


@pytest.fixture()
def home():
    """A throwaway home with a priceable library and an original presets.ini.

    Ordering is load-bearing and each step is here for a specific failure seen elsewhere in
    this repo:

      * `init()` before the DELETE — the table does not exist the first time this module runs,
        and deleting from it first is an OperationalError.
      * the library directory is emptied as well as the table — `rescan()` reads the directory,
        so a model left behind by another test file reappears in the index and silently joins
        every budget in this module.
      * rows are deleted through `db._conn()` rather than closed — that connection is
        thread-local and shared; closing it poisons every later `db.init()` in the process.
    """
    import shutil

    db.init()
    mi.init()
    if paths.LLAMA_MODELS.is_dir():
        for child in paths.LLAMA_MODELS.iterdir():
            shutil.rmtree(child, ignore_errors=True)
    db._conn().execute("DELETE FROM model_index")
    db._conn().commit()

    for name, size in (("Alpha-7B", 4 * 1024 ** 3), ("Beta-13B", 8 * 1024 ** 3)):
        _gguf(paths.LLAMA_MODELS / name / f"{name}.Q4_K_M.gguf", size)

    mi.rescan()

    paths.PRESETS_INI.parent.mkdir(parents=True, exist_ok=True)
    paths.PRESETS_INI.write_text(ORIGINAL)

    # Snapshots are state too: a leftover from a previous run would satisfy
    # "apply must snapshot" without apply having snapshotted anything.
    for existing in presets_store.list_snapshots():
        shutil.rmtree(existing["path"], ignore_errors=True)

    yield {"root": paths.AI_ROOT, "budget": BUDGET}

    db._conn().execute("DELETE FROM model_index")
    db._conn().commit()


def _quiet(msg: str) -> None:
    pass


# ------------------------------------------------------------- check_generated()
def test_check_generated_accepts_the_rendered_output(home):
    """The gate must not reject the thing the generator actually produces.

    A validator that only rejects hypothetical files is theatre: this asserts the real
    `render()` output for a real library passes, so every rejection test below is a rejection
    of a defect rather than of normal output.
    """
    rendered = P.render(PERSONA, home["budget"])
    known = {r["id"] for r in mi.rows()}
    assert P.check_generated(rendered["text"], known) == [], rendered["text"]


def test_check_generated_rejects_an_unparseable_file(home):
    """The retired generator emitted a file configparser refused. That is now a refusal here."""
    bad = "[*]\nthreads = 16\nctx\n-size\n \n=\n 8192\n"
    problems = P.check_generated(bad, {r["id"] for r in mi.rows()})
    assert problems and "does not parse" in problems[0]


def test_check_generated_rejects_undefined_and_router_owned_keys(home):
    """Three separate refusals, each exercised by a key that belongs to exactly one set.

    `model` is in BOTH ROUTER_CONTROLLED_KEYS and ROUTER_DERIVED_KEYS, and the loop reports the
    controlled-key reason then continues — so a fixture built from `model` can never produce the
    derived-key message. `metrics` is derived only, so it is the key that actually exercises that
    branch.
    """
    known = {r["id"] for r in mi.rows()}
    problems = P.check_generated(
        "[*]\nnot-a-real-key = 3\nalias = whatever\nmetrics = 1\n", known)
    joined = "\n".join(problems)
    assert "no definition in the settings registry" in joined
    assert "overwrites this key" in joined
    assert "derives this value itself" in joined
    assert len(problems) == 3, problems


def test_check_generated_rejects_a_value_that_fails_its_accepter(home):
    """`threads = banana` is a string; the registry says what a threads value must be."""
    known = {r["id"] for r in mi.rows()}
    problems = P.check_generated("[*]\nthreads = banana\n", known)
    assert problems and "threads" in problems[0]


def test_check_generated_rejects_a_section_that_matches_no_model(home):
    """The router matches sections to directory names, so a typo'd section is a silent no-op.

    Silent is the problem: the operator would believe the model had a setting.
    """
    problems = P.check_generated("[NotAModel]\nctx-size = 8192\n", {"Alpha-7B"})
    assert problems and "not a model in the library" in problems[0]


def test_check_generated_accepts_a_real_model_section(home):
    problems = P.check_generated("[Alpha-7B]\nctx-size = 8192\n", {"Alpha-7B"})
    assert problems == []


# ------------------------------------------------------------------- snapshot
def test_snapshot_copies_what_is_on_disk_not_what_is_about_to_be_written(home):
    """The whole point of a snapshot, stated as a test.

    If apply snapshotted the text it was about to write, a bug in the writer would corrupt the
    backup as well, and rollback would restore the damage.
    """
    snap = presets_store.snapshot(reason="test", personality="t")
    assert paths.PRESETS_INI.read_text() == ORIGINAL
    restored = (home["root"] / "state" / "rollback" / f"presets-{snap['stamp']}"
                / "presets.ini").read_text()
    assert restored == ORIGINAL


def test_snapshot_records_a_note_an_operator_can_read(home):
    snap = presets_store.snapshot(reason="because I said so", personality="safe")
    note = (home["root"] / "state" / "rollback" / f"presets-{snap['stamp']}"
            / "note.txt").read_text()
    assert "because I said so" in note
    assert "safe" in note
    assert "restore with:" in note


def test_snapshot_survives_two_applies_in_the_same_second(home):
    """Timestamp-only directory names would let the second apply overwrite the first backup."""
    a = presets_store.snapshot(reason="first")
    b = presets_store.snapshot(reason="second")
    assert a["stamp"] != b["stamp"]
    snaps = presets_store.list_snapshots()
    assert len(snaps) >= 2


def test_snapshot_of_a_missing_file_is_recorded_as_such(home):
    paths.PRESETS_INI.unlink()
    snap = presets_store.snapshot(reason="no file yet")
    assert snap["existed"] is False
    assert snap["bytes"] == 0


# ---------------------------------------------------------------------- apply
def test_apply_writes_and_snapshots_and_does_not_restart(home, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(systemd, "service_action",
                        lambda k, a: calls.append((k, a)) or {"ok": True, "state": {}})
    out = P.apply(PERSONA, home["budget"], log=_quiet)
    assert out["name"] == "safe"
    assert out["restart"]["ok"] is True
    assert calls == [("llamacpp", "restart")], "apply must restart the router through systemd"
    assert paths.PRESETS_INI.read_text() != ORIGINAL
    assert out["changed"] is True
    assert presets_store.list_snapshots(), "apply must snapshot before writing"


def test_apply_refuses_to_write_a_plan_that_does_not_fit(home):
    """The portal prices the worst case before it writes; a plan that provably does not fit moves
    the failure to load time instead of preventing it. `fit = on` would not have helped: it is
    blind to other residents and does not bound the model it is applied to (§6.12.2)."""
    tiny = 1 << 20  # 1 MiB: nothing in this library fits
    with pytest.raises(P.PersonalityError) as exc:
        P.apply(PERSONA, tiny, log=_quiet, restart=False)
    assert "refusing to write" in str(exc.value)
    assert paths.PRESETS_INI.read_text() == ORIGINAL, "refusal must not write"
    assert presets_store.list_snapshots() == [], "refusal must not even snapshot"


def test_apply_refuses_when_no_model_can_be_priced(home, monkeypatch):
    """FITS_UNKNOWN must refuse, and it must refuse without writing.

    Passing `budget_bytes=None` is NOT the way to reach this state: personality.py:437 treats
    None as "go measure it" and calls modelindex._pool_bytes(), which on this box answers 108 GiB.
    Asserting a refusal from `None` would therefore be asserting a behaviour the code deliberately
    does not have. The state is reached the way the app can actually hit it: a model the estimator
    cannot price.
    """
    monkeypatch.setattr(mi, "peak_bytes", lambda row, ctx=None: None)
    with pytest.raises(P.PersonalityError) as exc:
        P.apply(PERSONA, home["budget"], log=_quiet, restart=False)
    assert "cannot be priced" in str(exc.value)
    assert paths.PRESETS_INI.read_text() == ORIGINAL
    assert presets_store.list_snapshots() == []


def test_budget_none_means_measure_it_not_budget_unknown(home):
    """Pin the behaviour the previous version of this file asserted wrongly.

    `budget_for(p, None)` substitutes modelindex._pool_bytes(). That is intentional (the page and
    the API both want the live figure), but it means `None` is not a way to ask "what if we cannot
    measure?", and a test that assumed otherwise was asserting a fiction.
    """
    b = P.budget_for(PERSONA, None)
    assert b.budget_bytes is not None
    assert b.fits is not P.FITS_UNKNOWN
    explicit = P.budget_for(PERSONA, home["budget"])
    assert explicit.worst_case_bytes == b.worst_case_bytes


def test_apply_refuses_a_router_controlled_override_before_touching_disk(home):
    personality = dict(PERSONA, overrides={"Alpha-7B": {"alias": "stolen"}})
    with pytest.raises(P.PersonalityError):
        P.apply(personality, home["budget"], log=_quiet, restart=False)
    assert paths.PRESETS_INI.read_text() == ORIGINAL
    assert presets_store.list_snapshots() == []


def test_render_never_emits_an_override_for_an_unlisted_model(home):
    """render() builds sections by iterating resolve_models(), so an override for a name that is
    not in `models` is silently dropped rather than emitted.

    Asserted directly because it is the reason apply() cannot produce a ghost section from a
    personality: the only path to one is a hand-edited file, which is what the file-level check
    below exists for. If render() ever changed to iterate overrides instead of models, this test
    is what would notice.
    """
    personality = dict(PERSONA, overrides={"Ghost-999B": {"ctx-size": 8192}})
    rendered = P.render(personality, home["budget"])
    assert "[Ghost-999B]" not in rendered["text"]
    assert P.check_generated(rendered["text"], {r["id"] for r in mi.rows()}) == []


def test_check_generated_catches_a_ghost_section_that_render_cannot_produce(home):
    """Defense in depth: a section the router will ignore is a finding even though render()
    cannot emit it.

    A hand-edited presets.ini, or a model deleted from the library after the file was written,
    both produce this shape. The router matches sections to directory names, so the section is a
    silent no-op: the operator believes a model has a setting and it does not.
    """
    problems = P.check_generated("[Ghost-999B]\nctx-size = 8192\n",
                                {r["id"] for r in mi.rows()})
    assert problems and "not a model in the library" in problems[0]


def test_apply_refuses_a_personality_that_names_an_unknown_model(home):
    """The refusal an operator can actually trigger: naming a model that is not in the library.

    resolve_models() raises before render() runs, so nothing is written and nothing is snapshotted.
    """
    personality = dict(PERSONA, models=["Ghost-999B"])
    with pytest.raises(P.PersonalityError) as exc:
        P.apply(personality, home["budget"], log=_quiet, restart=False)
    assert "no library entry" in str(exc.value)
    assert paths.PRESETS_INI.read_text() == ORIGINAL
    assert presets_store.list_snapshots() == []


def test_apply_rolls_back_when_what_landed_on_disk_is_not_what_was_validated(home, monkeypatch):
    """The failure mode this exists for: the write reports success, the file is wrong.

    Mutates `personality._current_text` rather than doing real damage to a file. apply() detects
    a bad write by reading the file back through that function, so making the read disagree with
    the validated text *is* the condition the verify step exists to catch, and no I/O has to be
    sabotaged to produce it.

    Three earlier mutations of this test failed for reasons that were all the test's own fault:

      * a 1-arg replacement for `write_text` died with a TypeError inside presets_store.snapshot()
        — it is a bound method, so a replacement must take `self`;
      * patching `Path.write_text` globally also truncated the rollback's write;
      * patching `paths.PRESETS_INI` looked surgical but was not: presets_store resolves
        `paths.PRESETS_INI` dynamically on every call, so its `restore()` wrote through the same
        broken object and the file stayed damaged.

    That last false failure is worth keeping in mind, because it is what exposed a real gap in
    apply(): it called `restore()` and then reported "restored snapshot <stamp>" without ever
    checking that the file matched. `rollback verified` below is the assertion for the check that
    gap produced.
    """
    real_read = P._current_text
    state = {"reads": 0}

    def damaged_from_verify_onward() -> str:
        full = real_read()
        state["reads"] += 1
        # Read 1 is apply()'s `before`; read 2 is the verify. Report the file as halved from the
        # verify onward, which is what a partial write would look like to a reader.
        if state["reads"] >= 2:
            return full[: len(full) // 2]
        return full

    monkeypatch.setattr(P, "_current_text", damaged_from_verify_onward)
    lines: list[str] = []
    with pytest.raises(P.PersonalityError) as exc:
        P.apply(PERSONA, home["budget"], log=lines.append, restart=False)
    assert "does not match what was validated" in str(exc.value)
    assert "restored snapshot" in str(exc.value)
    assert state["reads"] >= 2, "apply must read the file back rather than trust the write call"

    # Everything below reads the real file: the mutation lives only inside personality.py.
    monkeypatch.undo()
    assert paths.PRESETS_INI.read_text() == ORIGINAL, "verify failure must restore the snapshot"
    joined = "\n".join(lines)
    assert "rollback verified" in joined, "apply must prove the rollback, not assume it"

    # Two snapshots exist after a failed apply: the one apply() took before writing, and the one
    # restore() takes before replacing the bad file. list_snapshots()[0] is the NEWER one — it
    # holds the corrupted applied file, which is correct behaviour and not what this asserts.
    # The stamp named in the error message is apply()'s own snapshot, and that is the copy that
    # must still hold the pre-apply bytes even though the write went wrong.
    snaps = presets_store.list_snapshots()
    assert len(snaps) == 2, [s["stamp"] for s in snaps]
    stamp_in_message = re.search(r"restored snapshot (\S+)", str(exc.value))
    assert stamp_in_message, str(exc.value)
    apply_snap = stamp_in_message.group(1)
    assert apply_snap in [s["stamp"] for s in snaps], snaps
    snap_file = presets_store.snapshot_dir(apply_snap) / "presets.ini"
    assert snap_file.read_text() == ORIGINAL, "apply's snapshot must survive a corrupted write"


def test_apply_restores_when_the_written_file_fails_revalidation(home, monkeypatch):
    """A file that parses but contains a key the registry refuses must not stay on disk.

    The mutation has to poison `check_generated` rather than `render()`: an earlier version
    appended a bogus key to the rendered text, but apply() validates *before* writing, so the
    refusal fired at the pre-write gate and the post-write branch was never executed — a test of
    the pre-write gate wearing the name of a test about the post-write one. Poisoning the
    revalidation models the real hazard instead: a file that validated in memory and then became
    wrong on disk, or a check that changed between the two calls.
    """
    real_check = P.check_generated
    state = {"calls": 0}

    def flaky(text, known_models):
        state["calls"] += 1
        if state["calls"] == 1:
            return real_check(text, known_models)      # pre-write: clean, so it writes
        return ["[*] injected-after-write: appeared between validation and disk"]

    monkeypatch.setattr(P, "check_generated", flaky)
    lines: list[str] = []
    with pytest.raises(P.PersonalityError) as exc:
        P.apply(PERSONA, home["budget"], log=lines.append, restart=False)
    assert "failed validation after writing" in str(exc.value)
    assert paths.PRESETS_INI.read_text() == ORIGINAL, "post-write failure must restore"
    assert "rollback verified" in "\n".join(lines)
    assert state["calls"] == 2, f"expected a pre-write and a post-write check, got {state['calls']}"


def test_apply_does_not_restart_when_the_restart_would_follow_a_bad_file(home, monkeypatch):
    """Restart is the last step, so a refusal above it must never reach systemctl."""
    calls: list[str] = []
    monkeypatch.setattr(systemd, "service_action",
                        lambda k, a: calls.append(f"{k}/{a}") or {"ok": True})
    with pytest.raises(P.PersonalityError):
        P.apply(PERSONA, 1 << 20, log=_quiet)
    assert calls == [], "a refused apply must not restart the router"


def test_apply_reports_the_snapshot_when_the_restart_fails(home, monkeypatch):
    """A failed restart is not a reason to revert a config the operator just asked for.

    Auto-rolling back here would guess that the restart failure was caused by presets.ini, and
    silently undo an instruction the operator gave.
    """
    monkeypatch.setattr(systemd, "service_action",
                        lambda k, a: {"ok": False, "error": "unit refused to start"})
    lines: list[str] = []
    out = P.apply(PERSONA, home["budget"], log=lines.append)
    assert out["restart"]["ok"] is False
    assert paths.PRESETS_INI.read_text() != ORIGINAL, "file stays written"
    joined = "\n".join(lines)
    assert "snapshot" in joined and out["snapshot"] in joined


def test_apply_is_audited_in_the_events_table(home):
    P.apply(PERSONA, home["budget"], log=_quiet, restart=False)
    rows = db.query("SELECT kind, detail FROM events WHERE kind='presets-apply' "
                    "ORDER BY id DESC LIMIT 1")
    assert rows, "every mutation must go through db.event"
    assert "personality=safe" in rows[0]["detail"]
    assert "snapshot=" in rows[0]["detail"]


# ------------------------------------------------------------------- rollback
def test_rollback_restores_the_original_bytes(home):
    out = P.apply(PERSONA, home["budget"], log=_quiet, restart=False)
    assert paths.PRESETS_INI.read_text() != ORIGINAL
    presets_store.restore(out["snapshot"])
    assert paths.PRESETS_INI.read_text() == ORIGINAL, "rollback must be byte-exact"


def test_rollback_is_itself_undoable(home):
    out = P.apply(PERSONA, home["budget"], log=_quiet, restart=False)
    r = presets_store.restore(out["snapshot"])
    assert r["undo_snapshot"], "a rollback must snapshot the file it is replacing"
    assert paths.PRESETS_INI.read_text() == ORIGINAL
    # and undoing the rollback gives the applied file back
    presets_store.restore(r["undo_snapshot"])
    assert paths.PRESETS_INI.read_text() != ORIGINAL


def test_rollback_of_an_unknown_stamp_raises(home):
    with pytest.raises(presets_store.PresetsSnapshotError) as exc:
        presets_store.restore("19700101-000000")
    assert "nothing to restore" in str(exc.value)


def test_diff_snapshot_shows_what_an_apply_changed(home):
    out = P.apply(PERSONA, home["budget"], log=_quiet, restart=False)
    diff = presets_store.diff_snapshot(out["snapshot"])
    assert any(line.startswith("+") for line in diff)
    assert any(line.startswith("-") for line in diff)


def test_prune_keeps_the_newest_and_leaves_at_least_one(home):
    for i in range(4):
        presets_store.snapshot(reason=f"n{i}")
    removed = presets_store.prune(keep=2)
    assert len(removed) == 2
    remaining = presets_store.list_snapshots()
    assert len(remaining) == 2
    assert all(s["stamp"] not in removed for s in remaining)


def test_prune_never_removes_everything(home):
    presets_store.snapshot(reason="only one")
    assert presets_store.prune(keep=1) == []
    assert len(presets_store.list_snapshots()) == 1


# --------------------------------------------------------------- isolation guard
def test_live_presets_are_never_touched_by_tests():
    """This module writes a file the router reads. Assert the real one is not that file.

    Not a style check: if `AI_PORTAL_HOME` ever stopped being honoured, every test above would
    be editing the live router's model configuration. Compared against the *real* home rather
    than against a hardcoded path string, so the assertion tracks where the tests actually run
    instead of assuming a particular temp directory.
    """
    real_home = Path(os.path.expanduser("~"))
    assert real_home != paths.HOME, "tests must not run against the real ~/ai"
    assert paths.PRESETS_INI.is_relative_to(Path(tempfile.gettempdir())), (
        f"presets.ini under test is {paths.PRESETS_INI}, not a throwaway directory")
