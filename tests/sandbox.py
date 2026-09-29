"""The only place a test may wipe a model library.

`tests/` has a pattern that predates this file: a fixture clears `paths.LLAMA_MODELS` so that
`rescan()` sees exactly the models the fixture wrote. The pattern is correct — `rescan()` reads the
directory, so leftover models come straight back and join every budget — and it is also the pattern
that deleted 13 model directories and 274 GiB of GGUFs from this machine's real library on
2026-09-25.

The failure had three parts, and only the third is a test bug:

  * `app.core.paths` resolves `HOME` from `AI_PORTAL_HOME` at import time, so a fixture cannot fix
    a home that was wrong when the module was imported;
  * a module that sets `AI_PORTAL_PASSWORD` but not `AI_PORTAL_HOME` does not error, it silently
    resolves `~/ai`;
  * and then `shutil.rmtree` over `paths.LLAMA_MODELS` does exactly what it says, on the owner's
    models, in a test whose own assertion never mentions the library at all.

So the destructive step lives here, where it can check what it is about to erase. A library outside
the temp directory is refused rather than wiped.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from app.core import paths

TEMP_ROOT = Path(tempfile.gettempdir()).resolve()


def _safe(root: Path, what: str) -> Path:
    """Return `root` if it is a fixture tree. Refuse anything else.

    Refusing is the point. There is no version of "clear it" that is safe against the real thing, so
    the safety has to live in the call rather than in the discipline of the caller -- the caller is a
    test, and the caller already got this wrong once.
    """
    home = Path(paths.HOME).resolve()
    if home == Path.home().resolve():
        raise RuntimeError(
            f"refusing to touch {what} in the real home ({home}); this test module imported "
            f"app.core.paths before setting AI_PORTAL_HOME")
    if TEMP_ROOT not in root.parents:
        raise RuntimeError(
            f"refusing to clear {root}: it is not under the temp directory ({TEMP_ROOT}), so it "
            f"is not fixture {what}")
    return root


def assert_test_home() -> Path:
    """The library path, if it is safely inside a throwaway home. Refuses everything else."""
    return _safe(Path(paths.LLAMA_MODELS).resolve(), "model library")


def clear_library() -> Path:
    """Delete the contents of the test library directory, and nothing that is not a test library."""
    root = assert_test_home()
    if root.is_dir():
        for child in root.iterdir():
            if child.is_symlink() or child.is_file():
                child.unlink(missing_ok=True)
            else:
                shutil.rmtree(child, ignore_errors=True)
    return root


def clear_snapshots() -> int:
    """Delete the presets.ini snapshots, if they are fixture snapshots.

    The other tree a test clears. Leftover snapshots are worse than leftover models for the rollback
    tests: a rollback there asserts that the restored bytes match a snapshot, and a snapshot from an
    earlier test makes that assertion true for a reason the test did not create.
    """
    from app.core import presets_store        # here, not at import: paths is what matters, not this
    base = _safe(Path(presets_store.directory()).resolve(), "snapshot tree")
    removed = 0
    if base.is_dir():
        for child in base.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
    return removed
