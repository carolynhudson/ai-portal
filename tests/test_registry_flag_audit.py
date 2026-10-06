"""Tests for tools/registry_flag_audit.py — the tool that asks a build which keys it takes.

None of these tests start a server. The tool's value is that it measures a real binary,
so its *tests* only have to prove it asks the right questions in the right shape: the key
list comes from the portal's registry, the flags are written the way the generator writes
them, and a bad invocation says so instead of reporting a clean audit.

docs/PORTAL_PLAN.md sec 6.12.21 records the measured audit; sec 6.12.22 records the run
that pinned the context-window predictor.
"""
import importlib.util
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _tool():
    path = ROOT / "tools" / "registry_flag_audit.py"
    spec = importlib.util.spec_from_file_location("registry_flag_audit", path)
    assert spec is not None and spec.loader is not None, path
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_audit_covers_every_key_the_editor_can_write():
    """The key list is the registry, read live — not a copy that drifts.

    A hardcoded list is how an audit starts lying: the registry gains a key, the audit
    keeps reporting the old set, and the new key goes unmeasured.
    """
    tool = _tool()
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("AI_PORTAL_HOME", str(pathlib.Path(os.environ["AI_PORTAL_HOME"])))
    from app.core import personality as P
    audited = {r["key"] for r in tool.registry_rows()}
    assert audited == set(P.SETTINGS), (len(audited), len(P.SETTINGS),
                                         sorted(audited ^ set(P.SETTINGS))[:6])
    # Every row carries what the report needs to be read later.
    for r in tool.registry_rows():
        assert r["kind"] and r["section"], r


def test_a_bool_key_is_probed_as_a_flag_with_no_value():
    """Measured on b11396: `--kv-unified true` is refused, `--kv-unified` is accepted.

    Probing a bool with a value measures a shape the generator never writes, so a pass
    would mean nothing.
    """
    tool = _tool()
    assert tool.argv_for("kv-unified", {"kind": "choice", "default": "on"}) == ["--kv-unified"]
    assert tool.argv_for("kv-unified", {"kind": "bool", "default": "true"}) == ["--kv-unified"]
    assert tool.argv_for("flash-attn", {"kind": "bool", "default": False}) == ["--no-flash-attn"]
    assert tool.argv_for("mlock", {"kind": "bool", "default": "off"}) == ["--no-mlock"]


def test_a_value_key_is_probed_with_its_default_value():
    """Int and float keys take the value the portal would write, not a made-up one."""
    tool = _tool()
    assert tool.argv_for("ctx-size", {"kind": "int", "default": 16384}) == ["--ctx-size", "16384"]
    assert tool.argv_for("temp", {"kind": "float", "default": 0.8}) == ["--temp", "0.8"]
    assert tool.argv_for("parallel", {"kind": "int", "default": 4}) == ["--parallel", "4"]


def test_the_audit_names_a_key_it_does_not_know_rather_than_skipping_it(tmp_path):
    """A bad key name must fail loudly.

    A silent skip reads as "nothing to report", which is how an audit starts lying.
    """
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "registry_flag_audit.py"),
                        "--keys", "kv-unified,definitely_not_a_setting"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 2, (r.returncode, r.stdout[-300:])
    assert "definitely_not_a_setting" in r.stdout, r.stdout[-300:]


def test_list_mode_reports_the_registry_without_starting_a_server():
    """`--list` is the safe entry point: read the key list, touch no binary, load no model."""
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "registry_flag_audit.py"),
                        "--list"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-300:]
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert len(lines) >= 30, len(lines)
    assert any(ln.startswith("kv-unified ") for ln in lines), lines[:3]


def test_a_missing_binary_says_which_path_it_tried(tmp_path):
    """The tool defaults to the portal's own binary path. When that is absent it must name it."""
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "registry_flag_audit.py"),
                        "--build", str(tmp_path / "nope"), "--keys", "kv-unified"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 2, (r.returncode, r.stdout[-200:])
    assert "no llama-server at" in r.stdout, r.stdout[-200:]


# ------------------------------------------- what a CLI probe cannot measure (b11429 run)
#
# A CLI audit answers one question: does this build accept this flag. Two things in the
# b11429 run made answers that were not about the flag: a key with no command-line form at
# all, and a probe model the server cannot build a context for.

def test_a_key_with_no_command_line_form_is_not_probed_on_the_command_line(tmp_path, monkeypatch):
    """`stop-timeout` is read from a preset by the router; `--kv-unified auto` is refused on
    the CLI and accepted from a preset, because the preset parser maps the value to a flag.

    Both are real registry keys. Probing either on a command line produces a refusal that
    says nothing about the portal's setting, and a reader who trusts the verdict turns off a
    working key. The tool must decline, and name the tool that can measure it.
    """
    tool = _tool()

    def no_spawn(*a, **k):  # any spawn here is the bug
        raise AssertionError("a preset-only key was spawned on a command line")

    monkeypatch.setattr(tool.subprocess, "Popen", no_spawn)
    rows = {r["key"]: r for r in tool.registry_rows()}
    for key in ("stop-timeout", "load-on-startup", "kv-unified"):
        rec = tool.run_one("/nonexistent/llama-server", pathlib.Path("x.gguf"),
                           rows[key], tmp_path)
        assert rec["verdict"] == "preset-only", (key, rec)
        assert rec["argv"] == [], (key, rec["argv"])
        assert "spec_preset_probe" in rec["why"], (key, rec["why"])


def test_the_probe_model_skips_a_pooled_file(tmp_path):
    """The smallest cached file is the wrong probe model, and there are two ways it is wrong.

    An embedder aborts on b11429 — `GGML_ASSERT(n_outputs_max <= cparams.n_outputs_max)` in
    `llama_context::output_reserve` — because its output count is the embedding width. An MTP
    draft segfaults (rc=-11) when loaded as a model. Both made the audit report every key as
    dead, which is a wall of failures and not one measurement. The picker therefore reuses
    the portal's own rules: modelindex._is_aux / _shard_of / NOT_A_MODEL_MARKERS for the
    naming, modelmeta.sniff_gguf's pooling_type for the header. No second rulebook.
    """
    tool = _tool()
    sys.path.insert(0, str(ROOT / "tests"))
    from gguf_fixture import _write_fake_gguf

    hub = tmp_path / "hub"
    pooled = hub / "models--orga--Embed-GGUF" / "snapshots" / ("a" * 40)
    side = hub / "models--orga--Big-GGUF" / "snapshots" / ("c" * 40)
    gen = hub / "models--orga--Gen-GGUF" / "snapshots" / ("b" * 40)
    pooled.mkdir(parents=True)
    side.mkdir(parents=True)
    gen.mkdir(parents=True)
    # The fixture writes `general.architecture` itself and prefixes the rest with
    # `testarch.`, so keys here are bare — `pooling_type` becomes `testarch.pooling_type`.
    # All three decoys are smaller than the file that must win, so only the rules decide.
    _write_fake_gguf(pooled / "embed.gguf", {"name": "Embed", "pooling_type": 1},
                     pad=12 * 1024 * 1024)
    _write_fake_gguf(side / "mtp-Qwen-Draft.gguf", {"name": "Draft", "context_length": 512},
                     pad=14 * 1024 * 1024)
    _write_fake_gguf(gen / "gen.gguf", {"name": "Gen", "context_length": 512},
                     pad=20 * 1024 * 1024)

    picked = tool.smallest_cached_model(hub)
    assert picked is not None and picked.name == "gen.gguf", (
        picked.name if picked else None,
        "the picker took a decoy: pooled, sidecar, or shard beats the only real model")


def test_a_declined_key_does_not_make_the_audit_fail(tmp_path, monkeypatch, capsys):
    """A key with no command-line form is not a defect, and must not read like one.

    Four registry keys are read from presets, not argv. On b11429 the audit reported
    "accepted 26 of 30" with four entries under "keys the build did not accept", exit 1 —
    the shape of a run that found problems, when it found none. A nightly check built on
    this tool would alert on the same four keys every night until someone turned them off,
    and the keys work.
    """
    tool = _tool()
    # main() wipes its work directory, so the probe file cannot live in it.
    probe_model = tmp_path / "probe" / "m.gguf"
    probe_model.parent.mkdir(parents=True)
    probe_model.write_bytes(b"GGUF" + b"\0" * 4096)  # main() stats the probe file
    monkeypatch.setattr(tool.sys, "argv", ["prog", "--work", str(tmp_path / "work")])
    monkeypatch.setattr(tool, "registry_rows", lambda: [
        {"key": "temp", "kind": "float", "section": "both", "default": 0.8, "choices": [],
         "preset_only": False, "preset_mapped": False},
        {"key": "stop-timeout", "kind": "int", "section": "both", "default": 10,
         "choices": [], "preset_only": True, "preset_mapped": False},
    ])
    monkeypatch.setattr(tool, "mem_available", lambda: 40 * tool.GIB)
    monkeypatch.setattr(tool, "smallest_cached_model", lambda hub: probe_model)
    monkeypatch.setattr(pathlib.Path, "is_file", lambda self: True)
    # the one CLI-probed key "accepts": the server reaches "listening"
    monkeypatch.setattr(tool, "run_one",
                        lambda b, m, row, work: {"key": row["key"], "kind": row["kind"],
                                                 "section": row["section"], "argv": [],
                                                 "verdict": "preset-only"
                                                 if (row.get("preset_only")
                                                     or row.get("preset_mapped"))
                                                 else "accepts",
                                                 "why": "", "log": ""})
    rc = tool.main()
    out = capsys.readouterr().out
    assert rc == 0, (rc, out)
    assert "did not accept" not in out, out
    assert "stop-timeout" in out and "no command-line form" in out, out
