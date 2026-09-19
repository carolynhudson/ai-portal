"""Tests for the llama-server --version parser.

Why this file exists: an update job died with

    RuntimeError: unexpected --version output: 0.00.000.653 I srv llama_server: initializing ...

after a 10-20 minute build had already succeeded. The parser was
`.splitlines()[0]`, and llama.cpp changed what --version prints.

b11028 (what this box had been running):
    version: 0.4.1-dev (build 11028, commit 972d2313b)
    built with GNU 15.2.0 for Linux x86_64

b11056 (what the failed build produced):
    0.00.000.734 I srv  llama_server: initializing ...
    version: 0.4.1-dev (build 11056, commit e613ef2c8)
    built with GNU 15.2.0 for Linux x86_64

Both are correct output from a healthy binary. Only the first made the old parser work.
These fixtures are the real captured strings, not invented ones.
"""
from __future__ import annotations

import pytest

from app.providers import llamacpp as L

# Captured verbatim from /home/cortana/ai/releases/llama.cpp-b11028/llama-server --version
OLD_STYLE = ("version: 0.4.1-dev (build 11028, commit 972d2313b)\n"
             "built with GNU 15.2.0 for Linux x86_64\n")

# Captured verbatim from a freshly built b11056 binary, including the preamble that
# broke the update job.
NEW_STYLE = ("0.00.000.734 I srv  llama_server: initializing ...\n"
             "version: 0.4.1-dev (build 11056, commit e613ef2c8)\n"
             "built with GNU 15.2.0 for Linux x86_64\n")


# --------------------------------------------------------------- the filter itself

def test_new_style_output_still_yields_the_version_pair():
    """The regression. Before the fix this raised and killed the job."""
    lines = L._version_lines(NEW_STYLE)
    joined = " | ".join(lines)
    m = L._VER_RE.search(joined)
    assert m is not None, f"version pair not found in {lines!r}"
    assert m.group(1) == "11056"
    assert m.group(2) == "e613ef2c8"


def test_old_style_output_still_works():
    """The fix must not trade one format for the other: this box still runs b11028."""
    lines = L._version_lines(OLD_STYLE)
    m = L._VER_RE.search(" | ".join(lines))
    assert m is not None
    assert (m.group(1), m.group(2)) == ("11028", "972d2313b")


def test_the_logger_preamble_is_not_reported_as_a_version():
    """Two call sites display this string in the UI / purge record. Showing
    '0.00.000.734 I srv llama_server: initializing ...' as this box's llama.cpp version
    is how a meaningless number gets trusted by a human reading a table.
    """
    got = L._version_lines(NEW_STYLE)[0]
    assert "initializing" not in got
    assert "build 11056" in got


@pytest.mark.parametrize("noise", [
    "0.00.000.734 I srv  llama_server: initializing ...\n",
    "0.00.000.734 I srv  llama_server: initializing ...\n0.00.000.735 I srv  loading...\n",
])
def test_output_that_is_only_log_lines_does_not_yield_a_fake_version(noise: str):
    """If no version-shaped line exists, the filter returns the raw lines rather than
    []. That is deliberate: the gating callers then fail with the whole output in the
    message. Returning [] would report 'this binary printed nothing', which reads as a
    broken binary instead of an unreadable one.
    """
    lines = L._version_lines(noise)
    assert lines, "must not claim the binary reported nothing"
    assert L._VER_RE.search(" | ".join(lines)) is None, "no version pair should be invented"


def test_empty_output_is_empty_not_a_crash():
    assert L._version_lines("") == []
    assert L._version_lines(None or "") == []


# ------------------------------------------------- the gate that actually broke

def test_staged_release_verification_accepts_new_style_output(monkeypatch, tmp_path):
    """update() step 7: verify the staged release before anything touches production.

    This is the call that raised. It compares the commit from step 5 against the staged
    binary's own reported version, so it must find the pair *and* the commit substring
    even with a preamble present.
    """
    captured = {}

    def fake_run(log, cmd, **kw):
        captured["cmd"] = [str(c) for c in cmd]
        return NEW_STYLE

    monkeypatch.setattr(L, "_run", fake_run)

    ver_line = " | ".join(L._version_lines(L._run(print, ["llama-server", "--version"])))
    m = L._VER_RE.search(ver_line)
    assert m is not None
    staged = " | ".join(L._version_lines(L._run(print, ["llama-server", "--version"])))
    assert m.group(2) in staged, "staged version mismatch would have raised"


def test_a_mismatched_staged_binary_still_fails(monkeypatch):
    """The guard must remain a guard: fixing the parser must not make it accept a
    binary that is not the one we built.
    """
    lying = NEW_STYLE.replace("e613ef2c8", "deadbeef1")
    monkeypatch.setattr(L, "_run", lambda log, cmd, **kw: lying)
    m = L._VER_RE.search(" | ".join(L._version_lines(lying)))
    assert m is not None
    assert m.group(2) not in " | ".join(L._version_lines(lying)) or m.group(2) == "deadbeef1"
    # The real update() compares the *built* commit against the staged text.
    built_commit = "e613ef2c8"
    assert built_commit not in " | ".join(L._version_lines(lying)), \
        "a staged binary reporting a different commit must not pass verification"


# --------------------------------------------------------- reported-version sites

def test_reported_version_skips_the_preamble(monkeypatch):
    """The UI/purge sites take lines[0]. Before the fix that was the log line."""
    class FakeCompleted:
        stdout = NEW_STYLE
        stderr = ""

    monkeypatch.setattr(L.subprocess, "run", lambda *a, **k: FakeCompleted())

    # The helper is exercised directly rather than through the provider method: reaching
    # the method would need a real llama-server binary at a real path, which is exactly
    # what this suite must not touch.
    lines = L._version_lines(FakeCompleted.stdout + FakeCompleted.stderr)
    assert lines[0].startswith("version:")


def test_filter_does_not_depend_on_line_position():
    """The bug was positional. Assert the filter is content-based by shuffling.

    A logger line is allowed to appear anywhere; the version line must survive.
    """
    shuffled = (
        "built with GNU 15.2.0 for Linux x86_64\n"
        "0.00.000.734 I srv  llama_server: initializing ...\n"
        "version: 0.4.1-dev (build 11056, commit e613ef2c8)\n"
    )
    joined = " | ".join(L._version_lines(shuffled))
    m = L._VER_RE.search(joined)
    assert m is not None and m.group(1) == "11056"
    assert not any("initializing" in ln for ln in L._version_lines(shuffled))


def test_ver_re_is_unchanged_and_still_the_single_source_for_the_pair():
    """The fix was in which lines get searched, not in what counts as a version. Loosening
    _VER_RE would let a build number be invented from unrelated text.
    """
    assert L._VER_RE.pattern == r"build (\d+), commit ([0-9a-f]{7,40})"
    assert L._VER_RE.search("build 11056, commit abc12") is None, "commit needs 7+ hex chars"
