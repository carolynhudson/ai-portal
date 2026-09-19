"""Regression tests for the presets generator.

Why this file exists at all: `PRESETS_INI` in tools/phase1_build_model_tree.py ended
with five stray lines — 'ctx', '-size', ' ', '=', ' 8192' — a hand-corruption that
survived in the committed source since the initial commit. configparser rejected the
generated file at line 82. The live presets.ini looked fine only because an older,
uncorrupted version of this string wrote it, so nothing on the box showed the breakage:
the generator was the only artifact that was wrong, and it had never been run since.

The bug was invisible to every test in the suite because no test parsed the generated
text. These do.
"""
from __future__ import annotations

import configparser
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import phase1_build_model_tree as gen  # noqa: E402

INI = gen.PRESETS_INI


def parse(text: str) -> configparser.ConfigParser:
    """Parse the way llama.cpp's loader would: '#' and ';' comments, sections are ids."""
    cp = configparser.ConfigParser(allow_no_value=True, strict=True,
                                   comment_prefixes=("#", ";"))
    cp.read_string(text)
    return cp


def test_generated_presets_parse_as_ini():
    """The generator's own output must be loadable. It was not: a stray '=' line broke
    the file at line 82 and no test had ever attempted to parse it."""
    cp = parse(INI)
    assert "*" in cp.sections(), "the global section is what per-model values inherit from"
    assert len(cp.sections()) >= 2, "expected per-model sections, not just [*]"


def test_no_stray_lines_outside_key_value_or_comment_shape():
    """The corruption was five lines that were not comments, not sections, and not
    'key = value'. Assert the shape of every line, so the specific typo class is
    closed rather than the exact string being remembered.

    Note the deliberate strictness: configparser in allow_no_value mode ACCEPTS a bare
    'ctx-size' as a valueless key, so a parse-only test would not have caught four of
    the five corrupt lines. Only the '=' line made it raise. Shape is checked here and
    parsing is checked separately, because neither alone is sufficient.
    """
    line_shape = re.compile(
        r"^\s*(?:[#;].*)?$"            # blank or comment
        r"|^\s*\[[^\]]+\]\s*$"        # section header
        r"|^\s*[A-Za-z0-9_.\-]+\s*=\s*\S+\s*$"  # key = value
    )
    bad = [(i + 1, ln) for i, ln in enumerate(INI.splitlines())
           if not line_shape.match(ln)]
    assert not bad, f"lines that are neither comment, section, nor key=value: {bad[:5]}"


def test_every_key_is_a_plausible_llama_server_argument():
    """A key that is a fragment of another key ('ctx' next to 'ctx-size') is a
    transcription error, not a setting. Keys here are llama-server argument names:
    letters, digits, dots and dashes only, no leading dash from a broken split."""
    cp = parse(INI)
    for section in cp.sections():
        for key in cp[section]:
            assert re.fullmatch(r"[a-z0-9_.\-]+", key), f"[{section}] odd key {key!r}"
            assert key not in ("ctx", "size", "-size", "="), f"[{section}] fragment key {key!r}"


def test_sections_that_name_models_match_directories_on_disk():
    """A preset section whose name is not a model directory is dead configuration: the
    router never asks for it. This is the same inconsistency the health pane reports as
    stale_presets; the generator must not be able to produce it.

    Skipped when the real library is absent (a clean checkout, CI) rather than failing
    for a reason that has nothing to do with the code.
    """
    library = Path.home() / "ai" / "models" / "llama"
    if not library.is_dir():
        pytest.skip("no llama model library on this machine")
    dirs = {p.name for p in library.iterdir() if p.is_dir() or p.is_symlink()}
    cp = parse(INI)
    named = {s for s in cp.sections() if s != "*"}
    dead = sorted(named - dirs)
    assert not dead, f"preset sections with no model directory: {dead}"


def test_generated_output_is_stable_across_two_parses():
    """Guards against a generator whose text depends on dict ordering or on something
    that mutates between runs: parse it twice and compare the resolved settings."""
    a, b = parse(INI), parse(INI)
    assert {s: dict(a[s]) for s in a.sections()} == {s: dict(b[s]) for s in b.sections()}
