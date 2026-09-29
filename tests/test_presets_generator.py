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


def test_every_model_section_is_a_name_a_loader_could_ask_for():
    """Sections must be usable directory names, and one model may not be named twice.

    What this test *used* to do was compare the generator's sections against the live library, and
    that comparison stopped meaning anything the moment it was read as a live check: `PRESETS_INI`
    here is a frozen literal in `tools/phase1_build_model_tree.py`, a retired one-time record (its
    own header says so), while the library is whatever happens to be on the machine today. After the
    library was rebuilt on 2026-09-25 the test failed by naming thirteen models that had been gone for
    weeks -- a true statement about a snapshot, and not a defect in any code under test.

    The live inconsistency it was standing in for is checked where it actually matters: `health.py`
    compares the sections of the real presets.ini against the real library and reports them as
    `stale_presets`, asserted in tests/test_health.py::...Vanished-Model. What is left to assert here
    is the part that is about this file's own output.
    """
    cp = parse(INI)
    named = [s for s in cp.sections() if s != "*"]
    assert named, "the generator emitted no model sections at all"
    assert len(named) == len(set(named)), f"two sections name the same model: {named}"
    for section in named:
        # The router matches a section to a directory by name, so a section that cannot be a
        # directory name is configuration no loader will ever ask for.
        assert re.fullmatch(r"[^/\\\s]+", section), f"unusable section name {section!r}"
        assert not section.startswith("-"), f"section {section!r} looks like a broken split"


def test_generated_output_is_stable_across_two_parses():
    """Guards against a generator whose text depends on dict ordering or on something
    that mutates between runs: parse it twice and compare the resolved settings."""
    a, b = parse(INI), parse(INI)
    assert {s: dict(a[s]) for s in a.sections()} == {s: dict(b[s]) for s in b.sections()}
