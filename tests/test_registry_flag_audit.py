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
