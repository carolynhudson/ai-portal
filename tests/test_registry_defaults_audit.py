"""Tests for tools/registry_defaults_audit.py — the tool that asks a build what its defaults are.

The flag audit (`tools/registry_flag_audit.py`, docs/PORTAL_PLAN.md §6.12.21 and §6.12.25)
answers which keys a build accepts. This answers the other half: what the build says each key
defaults to, because `personality.render()` omits every key equal to `SERVER_DEFAULTS`. If the
portal's copy of a default drifts from the build's, the generated presets.ini either carries a
line that changes nothing (§4.10.0) or drops a line the worker needed — both silent.

No test here starts a server. The tool reads `--help`, so these tests read a captured help file
(tests/fixtures/llama_server_help_sample.txt, taken from b11429's layout) and prove the reader,
the verdicts, and the exit codes. The value of the instrument is that it measures a real binary;
the value of its tests is that it asks the right questions of the text.
"""
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "llama_server_help_sample.txt"


def _tool():
    path = ROOT / "tools" / "registry_defaults_audit.py"
    spec = importlib.util.spec_from_file_location("registry_defaults_audit", path)
    assert spec is not None and spec.loader is not None, path
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _help_map():
    return _tool().parse_help(FIXTURE.read_text(encoding="utf-8"))


def _rows(tool=None, registry=None, server_defaults=None):
    tool = tool or _tool()
    sys.path.insert(0, str(ROOT))
    from app.core import personality as P
    return tool.compare(registry or P.SETTINGS,
                        server_defaults if server_defaults is not None else P.SERVER_DEFAULTS,
                        _help_map())


# ---------------------------------------------------------------- the reader
def test_a_block_declares_the_flags_in_its_prefix_and_not_the_ones_in_its_prose():
    """Measured on this tool's first run: `--fit` came back with a default of 4096.

    The `--fit-ctx` line reads "minimum ctx size that can be set by --fit option, default: 4096",
    so a parser that keys a block by every `--flag` in its text makes `--fit-ctx` declare `fit`,
    and the audit reports the fit default as 4096 while the build says 'on'. A report like that is
    worse than no report: it reads as drift in a registry row that was correct.
    """
    hm = _help_map()
    assert hm["fit"]["stated"].strip("'\"") == "on", hm["fit"]["decl"]
    assert hm["fit-ctx"]["stated"] == "4096", hm["fit-ctx"]["decl"]
    assert "fit" in hm["fit-ctx"]["text"], "the fixture line must keep naming --fit in prose"


def test_a_multi_character_short_flag_line_is_a_declaration():
    """Short forms are not single letters, and that is what broke the first parser.

    Measured: the first version accepted `-x, --flag`, so every line whose short form is longer
    than one letter was skipped and 11 registry keys came back as "not in --help" -- among them
    `-fa, --flash-attn`, `-ngl, --gpu-layers, --n-gpu-layers`, `-ctxcp, --ctx-checkpoints,
    --swa-checkpoints`, `-cram, --cache-ram`, `-fitt, --fit-target` and `-lzm, --lazy-mode`.
    A run that reports a missing measurement as a verdict is the failure shape this repo has been
    bitten by before (§6.12.17, §6.13).
    """
    hm = _help_map()
    for key in ("flash-attn", "parallel", "n-gpu-layers", "fit", "fit-target", "fit-ctx",
                "lazy-mode", "cache-ram", "ctx-checkpoints"):
        assert key in hm, f"{key} was not parsed from a line with a multi-character short form"


def test_a_line_declares_every_long_form_it_lists_before_its_description():
    """Real b11429 lines name a key three ways, and the preset can use any of them.

    `-ngl, --gpu-layers, --n-gpu-layers N` and `-ctxcp, --ctx-checkpoints, --swa-checkpoints N`
    are verbatim in the fixture. Keying only the first long form would report
    `n-gpu-layers` as absent from a build whose help line contains it, and a key the audit cannot
    find is a key nobody measures -- the same failure as the flag audit's hardcoded key list.
    """
    hm = _help_map()
    assert "n-gpu-layers" in hm and "gpu-layers" in hm, hm.get("n-gpu-layers")
    assert "ctx-checkpoints" in hm and "swa-checkpoints" in hm
    # The short form is not a preset key, and must not become one.
    assert "ngl" not in hm and "ctxcp" not in hm


def test_the_scanner_walks_a_value_placeholder_instead_of_stopping_the_line():
    """Defensive, and labelled as such: b11429's help has no line of this shape.

    `grep -c '^[A-Za-z0-9-]+ [A-Z]+,? --' /tmp/help_b11429.txt` is 0 -- llama.cpp puts the
    placeholder after every long form (`-np, --parallel N`). So nothing on the deployed build
    depends on walking over `N`. The scanner does it anyway, because a release that writes
    `-np N, --parallel N` would otherwise lose the key silently, and "silently lose" is the
    failure this file has been written against. Provenance stated because a test that claims a
    measurement it never made is worse than no test: this one was first described as if
    `-np N, --parallel N` were a real line, and mutation-checking it found no test could fail.
    """
    parsed = _tool().parse_help("-np N, --parallel N            number of server slots (default: -1)")
    assert "parallel" in parsed, parsed


def test_a_section_header_of_dashes_is_not_a_flag():
    """`----- common params -----` is a header. A line of only dashes declares nothing."""
    hm = _help_map()
    assert not [k for k in hm if set(k) <= {"-", " "} or k == ""], hm.keys()
    assert "help" in hm and "version" in hm


def test_a_default_that_states_no_value_is_not_treated_as_a_value():
    """`default: unused` and `default: unset` say nothing, and comparing them to 0 would "pass".

    `--kv-unified-per-slot` states `default: unset`; a naive comparison of 0 against the string
    "unset" is a failure, and a naive comparison against "unused" is a pass nobody earned.
    """
    tool = _tool()
    parsed = tool.parse_help("-dk, --demo-key N                   something (default: unused)")
    assert parsed["demo-key"]["stated"] == "unused", parsed
    rows = {r["key"]: r for r in _rows()}
    assert rows["kv-unified-per-slot"]["verdict"] == "no default stated"


# ---------------------------------------------------------------- the verdicts
def test_the_audit_covers_every_key_the_registry_defines():
    """A key nobody compares is a key nobody measures — the same rule as the flag audit's key list."""
    sys.path.insert(0, str(ROOT))
    from app.core import personality as P
    keys = {r["key"] for r in _rows()}
    assert set(P.SETTINGS) <= keys
    # Server defaults the registry never names are reported, not silently skipped.
    informational = {r["key"] for r in _rows()
                     if "no registry definition" in str(r["verdict"])}
    assert set(P.SERVER_DEFAULTS) - set(P.SETTINGS) == informational


def test_a_registry_default_equal_to_the_build_is_not_a_finding():
    rows = {r["key"]: r for r in _rows()}
    assert rows["ctx-size"]["verdict"] == "match"
    assert rows["cache-ram"]["verdict"] == "match"
    assert rows["checkpoint-min-step"]["verdict"] == "match"


def test_a_number_written_in_a_different_spelling_is_not_drift():
    """The build prints 0.80, 0.00, `enabled`. The registry stores 0.8, 0.0, True.

    Comparing those as text turns seven correct rows into findings, and a report full of false
    findings is how a real one gets skimmed past.
    """
    rows = {r["key"]: r for r in _rows()}
    for key in ("temp", "top-k", "min-p", "cache-prompt", "cache-idle-slots", "jinja",
                "flash-attn"):
        assert rows[key]["verdict"] in ("match", "spelling"), (key, rows[key]["verdict"])
    assert not _tool().drift(_rows()), _tool().drift(_rows())


def test_a_recorded_server_default_is_a_documented_deviation_not_drift():
    """`kv-unified` omits at 'auto' while b11429 defaults to enabled — deliberately, and written down.

    'auto' measures as on (§6.12.21), so omitting the line reproduces the server's own behaviour.
    The deviation is only safe while it is recorded: the row carries `server_default`, so a build
    that changes the real default produces DRIFT here instead of absorbing the change.
    """
    sys.path.insert(0, str(ROOT))
    from app.core import personality as P
    assert P.SETTINGS["kv-unified"]["server_default"] == "enabled"
    rows = {r["key"]: r for r in _rows()}
    assert rows["kv-unified"]["verdict"] == "documented", rows["kv-unified"]
    assert not _tool().drift(_rows())


def test_a_key_the_editor_can_write_with_no_server_default_entry_is_drift():
    """The hole this tool found on its first run against the real registry.

    With no entry, nothing is ever recognised as the default, so a value equal to the build's
    behaviour is written as a line: `dedup-cache-models = off` restates a router that hides
    nothing. Checked first, before the "no CLI form" branch, because being preset-only is not an
    excuse for having no default.
    """
    sys.path.insert(0, str(ROOT))
    from app.core import personality as P
    stripped = {k: dict(v) for k, v in P.SETTINGS.items()}
    stripped["dedup-cache-models"] = dict(stripped["dedup-cache-models"])
    defaults = {k: v for k, v in P.SERVER_DEFAULTS.items() if k != "dedup-cache-models"}
    rows = {r["key"]: r for r in _rows(registry=stripped, server_defaults=defaults)}
    assert rows["dedup-cache-models"]["verdict"].startswith("DRIFT"), rows["dedup-cache-models"]
    assert len(_tool().drift(list(rows.values()))) == 1


def test_a_registry_default_that_moves_below_the_build_is_drift():
    """The failure the tool exists for: the portal stops omitting a line the server already applied.

    `ctx-size` is the clean example. If the registry said 4096, `ctx-size = 4096` would be written
    into every section while the server's own default is 0 = loaded from the model — the cap a
    personality was never meant to set (§4.10.1).
    """
    sys.path.insert(0, str(ROOT))
    from app.core import personality as P
    registry = {k: dict(v) for k, v in P.SETTINGS.items()}
    defaults = dict(P.SERVER_DEFAULTS, **{"ctx-size": 4096})
    rows = {r["key"]: r for r in _rows(registry=registry, server_defaults=defaults)}
    found = _tool().drift(list(rows.values()))
    assert [r["key"] for r in found] == ["ctx-size"], found
    assert "4096" in found[0]["verdict"] and "build defaults to '0'" in found[0]["verdict"]


def test_a_preset_only_key_is_expected_to_have_no_cli_line():
    """`load-on-startup`, `stop-timeout`, `dedup-cache-models` have no CLI form at all.

    §6.12.25 measured them as "declined" from the flag audit; §6.12.26 pinned why — v0.5.0 and
    v0.6.0 declare exactly these three in `common_params_add_preset_options()`, never as `--flags`.
    Saying "absent, as expected" is what keeps them out of the findings list.
    """
    rows = {r["key"]: r for r in _rows()}
    for key in ("load-on-startup", "stop-timeout", "dedup-cache-models"):
        assert key not in _help_map(), f"{key} should not be parseable as a CLI flag"
        assert rows[key]["verdict"] == "no CLI form (preset-only)", rows[key]


# ---------------------------------------------------------------- the instrument's own honesty
def test_a_missing_binary_reports_nothing_rather_than_clean(tmp_path, monkeypatch):
    """An instrument that cannot measure must not exit 0.

    Every "feature" this repo has had to re-verify started with a probe that returned a confident
    answer for the wrong target (§6.13, §6.12.17). The suite runs with AI_PORTAL_HOME in a temp
    home, so the sandbox has no llama-server: the tool has to say so and exit 2.
    """
    tool = _tool()
    sys.path.insert(0, str(ROOT))
    from app.core import paths
    assert tool.server_binary() is None, f"sandbox home unexpectedly has a server at {paths.AI_BIN}"
    monkeypatch.setattr(sys, "argv", ["registry_defaults_audit.py"])
    assert tool.main() == 2


def test_a_build_can_be_asked_by_path_without_touching_the_release_symlink(tmp_path):
    """--build measures the build you point at; ~/ai/bin/llama-server is never moved to do it."""
    tool = _tool()
    fake = tmp_path / "llama-server"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    assert tool.server_binary(str(fake)) == fake
    assert tool.server_binary(str(tmp_path / "nope")) is None


def test_help_text_is_parsed_from_a_file_so_a_run_can_be_reproduced(tmp_path):
    """--help-text is the reproducibility path: a captured run stays auditable after the build moves.

    The rows have to survive the round trip, because the report an operator reads and the report a
    later session compares against are the same artifact. Rows written and read back with an empty
    drift list is the claim being tested, not the file existing.
    """
    import json
    sys.path.insert(0, str(ROOT))
    from app.core import personality as P
    tool = _tool()
    out = tmp_path / "rows.json"
    hm = tool.parse_help(FIXTURE.read_text(encoding="utf-8"))
    rows = tool.compare(P.SETTINGS, P.SERVER_DEFAULTS, hm)
    out.write_text(json.dumps({"source": "fixture", "flags": len(hm), "rows": rows}),
                   encoding="utf-8")
    back = json.loads(out.read_text(encoding="utf-8"))["rows"]
    assert back == rows, "the report must survive being written and read back"
    assert back and tool.drift(back) == [], tool.drift(back)


def test_the_fixture_covers_the_keys_the_verdicts_are_asserted_on():
    """A fixture that silently loses a flag turns a real finding into a parsing artifact.

    Every key the verdict tests above assert on has to be present in the captured help, or the
    test passes for the wrong reason: "not in --help" is not a clean verdict.
    """
    hm = _help_map()
    asserted = ("ctx-size", "cache-ram", "checkpoint-min-step", "kv-unified", "temp", "top-k",
                "min-p", "cache-prompt", "cache-idle-slots", "jinja", "flash-attn", "fit",
                "fit-ctx", "fit-target", "parallel", "n-gpu-layers", "lazy-mode")
    missing = [k for k in asserted if k not in hm]
    assert missing == [], f"fixture is missing flags the tests assert on: {missing}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
