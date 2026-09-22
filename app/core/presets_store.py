"""Snapshot and rollback for `presets.ini` — the safety net the apply path depends on.

`presets.ini` is the first file this portal writes that the router reads at load time, so
"undo" cannot be an idea, it has to be an artifact. Every apply therefore copies the file it is
about to replace into `~/ai/state/rollback/` first, in the same timestamped-directory shape
`app/core/units.py` and `llamacpp.py` already use for unit files and release repoints.

Two rules this module exists to enforce:

1. **A snapshot is taken from disk, never from memory.** The point of a snapshot is to be able
   to dispute what the code believes it wrote. If `apply()` snapshotted the text it was about to
   write, a bug in the writer would corrupt the backup too, and rollback would restore the
   damage.
2. **Rollback restores bytes, not a re-render.** Rolling back must not depend on the current
   model index, the current personality definitions, or `render()` still behaving the way it did
   when the file was written. A snapshot is a file and a name; that is all rollback needs.

Snapshots are kept on disk indefinitely by default and pruned to `KEEP` newest entries, because
a `presets.ini` that took a week to notice is wrong is still worth being able to diff against.
"""
from __future__ import annotations

import difflib
import shutil
import time
from pathlib import Path

from . import logsetup, paths

# Snapshot directories older than this many are deleted after a successful apply. Keeping a
# few is undo; keeping thousands is a disk leak in a state directory that also holds secrets.
KEEP = 20

PREFIX = "presets-"
NOTE_NAME = "note.txt"
SNAPSHOT_NAME = "presets.ini"


class PresetsSnapshotError(Exception):
    """Raised when a snapshot or rollback cannot be performed safely."""


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def directory() -> Path:
    """Where snapshots live. Public so the UI and tests point at the same place."""
    return paths.ROLLBACK_DIR


def snapshot_dir(stamp: str) -> Path:
    return directory() / f"{PREFIX}{stamp}"


def list_snapshots() -> list[dict]:
    """Newest-first list of what can be rolled back to.

    A directory that is missing its `presets.ini` is reported with `readable: False` rather
    than skipped: an operator asking "what can I go back to" needs to know a backup exists and
    is broken, not to be shown a shorter list.
    """
    out: list[dict] = []
    base = directory()
    if not base.is_dir():
        return out
    for child in sorted(base.iterdir(), reverse=True):
        if not child.is_dir() or not child.name.startswith(PREFIX):
            continue
        snap = child / SNAPSHOT_NAME
        entry: dict = {
            "stamp": child.name[len(PREFIX):],
            "path": str(child),
            "readable": False,
            "bytes": 0,
            "note": "",
        }
        try:
            entry["bytes"] = snap.stat().st_size
            entry["readable"] = True
        except OSError:
            # stat() failing is the finding: this entry is reported as unreadable below rather
            # than skipped, so the failure is surfaced to the caller instead of being hidden by
            # a shorter list.
            pass
        try:
            entry["note"] = (child / NOTE_NAME).read_text().strip()
        except OSError:
            entry["note"] = ""
        out.append(entry)
    return out


def _read_current() -> str | None:
    """The file as it is on disk right now, or None if it does not exist.

    `None` and `""` are different facts and are kept different: a missing presets.ini and an
    empty one need different rollbacks (delete vs write).
    """
    try:
        return paths.PRESETS_INI.read_text()
    except OSError:
        return None


def snapshot(reason: str, personality: str = "") -> dict:
    """Copy the current `presets.ini` to a timestamped snapshot. Returns the snapshot record.

    Called *before* the write, from disk, by the apply path. Raises if the file exists but
    cannot be read: proceeding would overwrite the only copy of a config the router is using,
    with no way back, which is precisely the failure this module exists to prevent.
    """
    current = _read_current()
    if current is None:
        # No file yet. Recording an empty snapshot keeps the history honest (this apply created
        # the file) and gives rollback something to compare against.
        current = ""
        missing = True
    else:
        missing = False

    stamp = _stamp()
    dest = snapshot_dir(stamp)
    # Two applies in the same second would otherwise share a directory and the second would
    # silently overwrite the first's backup.
    suffix = 1
    while dest.exists():
        dest = snapshot_dir(f"{stamp}-{suffix}")
        suffix += 1
        if suffix > 999:
            raise PresetsSnapshotError(f"cannot name a unique snapshot for {stamp}")

    try:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / SNAPSHOT_NAME).write_text(current)
        (dest / NOTE_NAME).write_text(
            f"presets.ini snapshot before apply\n"
            f"taken: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"reason: {reason}\n"
            f"personality: {personality or '(none)'}\n"
            f"source: {paths.PRESETS_INI}\n"
            f"existed before apply: {'no' if missing else 'yes'}\n"
            f"restore with: cp {dest / SNAPSHOT_NAME} {paths.PRESETS_INI}\n"
        )
    except OSError as exc:
        # Leave no half-written snapshot behind: a directory that looks like a backup but is
        # missing its file is worse than no directory at all.
        shutil.rmtree(dest, ignore_errors=True)
        raise PresetsSnapshotError(f"could not snapshot {paths.PRESETS_INI}: {exc}") from exc

    return {
        "stamp": dest.name[len(PREFIX):],
        "path": str(dest),
        "bytes": len(current.encode()),
        "existed": not missing,
        "reason": reason,
        "personality": personality,
    }


def restore(stamp: str) -> dict:
    """Write a snapshot's bytes back over `presets.ini`.

    Takes a stamp, not a rendered personality: rollback must work when the index, the
    personalities, and the code have all moved on. The file being replaced is snapshotted first
    too, so a rollback is itself undoable.
    """
    src = snapshot_dir(stamp) / SNAPSHOT_NAME
    if not src.is_file():
        raise PresetsSnapshotError(
            f"no snapshot {stamp!r} at {snapshot_dir(stamp)} — nothing to restore"
        )
    try:
        data = src.read_text()
    except OSError as exc:
        raise PresetsSnapshotError(f"snapshot {stamp} cannot be read: {exc}") from exc

    before = snapshot(reason=f"before rollback to {stamp}", personality="(rollback)")
    try:
        paths.PRESETS_INI.parent.mkdir(parents=True, exist_ok=True)
        paths.PRESETS_INI.write_text(data)
    except OSError as exc:
        raise PresetsSnapshotError(f"could not restore {paths.PRESETS_INI}: {exc}") from exc

    return {
        "restored_from": stamp,
        "path": str(paths.PRESETS_INI),
        "bytes": len(data.encode()),
        "undo_snapshot": before["stamp"],
    }


def diff_snapshot(stamp: str) -> list[str]:
    """Unified diff between a snapshot and the file as it is now.

    This is the question an operator actually asks after a bad apply: not "what was the old
    file" but "what exactly did that apply change".
    """
    src = snapshot_dir(stamp) / SNAPSHOT_NAME
    try:
        old = src.read_text()
    except OSError as exc:
        raise PresetsSnapshotError(f"snapshot {stamp} cannot be read: {exc}") from exc
    new = _read_current() or ""
    return list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"snapshot {stamp}", tofile="presets.ini (now)", lineterm=""))


def prune(keep: int = KEEP) -> list[str]:
    """Delete snapshots older than the newest `keep`. Returns the stamps removed.

    Runs after a successful apply, never before: pruning first would briefly leave the machine
    with no backup at the exact moment it needs one.
    """
    removed: list[str] = []
    if keep < 1:
        return removed
    entries = list_snapshots()[keep:]
    for entry in entries:
        try:
            shutil.rmtree(Path(entry["path"]))
            removed.append(entry["stamp"])
        except OSError as exc:
            # A snapshot we cannot delete is not a reason to fail an apply that already worked:
            # the apply's real job succeeded, and a leftover directory costs disk, not safety.
            # It is still logged, because a state directory that silently stops being writable
            # is the kind of problem that is discovered months later during a rollback.
            logsetup.get(__name__).warning(
                "could not prune presets snapshot %s: %s", entry["path"], exc)
            continue
    return removed
