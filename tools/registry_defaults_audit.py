#!/usr/bin/env python3
"""Ask a llama.cpp build what it says its own defaults are, and compare them to the registry.

`personality.render()` omits any preset key whose value equals `SERVER_DEFAULTS`. That
omission is the whole reason a generated presets.ini contains 6 lines and not 30, and it is
only correct while the portal's copy of each default matches the build's. A default that
drifts is not a cosmetic bug:

    registry default too low   ->  the portal writes a line the server would have applied
                                   anyway, and the file stops being readable (§4.10.0)
    registry default too high  ->  the portal omits a line the server needs, so the worker
                                   runs a value nobody chose, and nothing reports it

The flag audit (`tools/registry_flag_audit.py`) answers *which keys a build takes*. This
answers the other half: *what the build says each key defaults to*. Both are run after an
upgrade; §6.12.26 records the measured comparison, §6.12.21 and §6.12.25 the flag audit.

    .venv/bin/python tools/registry_defaults_audit.py
    .venv/bin/python tools/registry_defaults_audit.py --help-text /tmp/help_b11429.txt
    .venv/bin/python tools/registry_defaults_audit.py --json /tmp/defaults.json

Verdicts:
    match                 the registry's default equals what the build states
    spelling              same value, different spelling (`0.8` vs `0.80`, `True` vs `enabled`)
    documented            the registry's default differs from the build's, and the key says so
                          via `server_default` -- deliberate, with a reason in its help text
    DRIFT                 the registry's default differs from the build's and nothing explains
                          it: fix the registry, or record it as a documented deviation
    no default stated     the build's help gives no default for this key. For the three
                          preset-only keys (`load-on-startup`, `stop-timeout`,
                          `dedup-cache-models`) that is expected: they have no CLI form, so
                          they are not in --help at all. For anything else, check the spelling.
    not in --help         the key is not in the build's help and is not marked preset-only

Exit 0 when nothing drifts. Exit 1 when something does. A run that cannot read a server
binary exits 2 and says why, because an instrument that reports "clean" after failing to
measure is the failure mode this repo has been bitten by three times (§6.12.17, §6.13).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.core import personality as P                                # noqa: E402
from app.core import paths                                          # noqa: E402

SERVER = "llama-server"
SUMMARY = "Compare the settings registry's defaults against what a llama.cpp build states."

# A declaration line starts at column 0; its continuation lines are indented. That is
# the whole parser: split blocks on indentation, then read a block for the flag it names.
# A declaration line starts at column 0 with a dash. Short forms are not single letters
# (`-fa`, `-ngl`, `-ctk`), so the pattern cannot enumerate them -- and a section header is
# also all dashes (`----- common params -----`), which is why the header is excluded by
# being made only of dashes and spaces.
_DECL_START = re.compile(r"^-{1,2}[A-Za-z0-9]")
_ALL_DASHES = re.compile(r"^[-\s]+$")
_DEFAULT = re.compile(r"default:\s*(same as [^)]+|\"[^\"]*\"|'[^']*'|[A-Za-z0-9_.\-]+)")
# "default: unused" and "default: same as --threads" state no value, so they are not a
# default an estimator can compare against. Kept as a list so a new one is a data edit.
_NOT_A_VALUE = ("unused", "unset", "n/a")


# ---------------------------------------------------------------- reading the build
def server_binary(explicit: str | None = None) -> pathlib.Path | None:
    """The server binary to ask, resolved the way the portal launches it.

    `~/ai/bin/llama-server` is the symlink farm's current release, so "the build in use"
    is a path the portal already owns. Passing --build asks a different build without
    touching that link.
    """
    cand = pathlib.Path(explicit) if explicit else paths.AI_BIN / SERVER
    try:
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    except OSError:                          # an unreadable path is "not available", not a crash
        return None
    return None


def help_text(binary: pathlib.Path) -> str:
    """`<binary> --help`, with the release's own libraries on the loader path.

    A staged release keeps its .so files beside the binary; running it from a plain shell
    fails with a loader error that looks exactly like a broken build.
    """
    env = dict(os.environ, LD_LIBRARY_PATH=str(binary.resolve().parent))
    out = subprocess.run([str(binary), "--help"], capture_output=True, text=True,
                         env=env, timeout=60)
    return out.stdout + out.stderr


def decl_flags(decl: str) -> list[str]:
    """The long forms a declaration line *declares*, scanning its leading flag tokens.

    Keying a block by every `--flag` in its text is wrong and was measured wrong: the
    `--fit-ctx` line reads "minimum ctx size that can be set by --fit option, default:
    4096", which made it declare `fit` and handed the fit audit a default of 4096 when
    the build says 'on'. A block declares the flags in front of its description, so the
    scan stops at the first token that is not a flag.
    """
    # llama.cpp writes `-c, --ctx-size N  size of the prompt context`: a short form, the long
    # form, a value placeholder, then the description, which starts lowercase. So the scan
    # collects `--names`, skips short forms, walks over placeholders (`N`, `<0|1>`, `MiB0,...`)
    # and stops at the first lowercase word. Stopping at the placeholder was a bug: `-np N,
    # --parallel N` lost parallel, and 11 keys came back "not in --help".
    names: list[str] = []
    for tok in decl.split():
        if tok.startswith("--"):
            name = tok[2:].strip(",[]{} ")
            if name:
                names.append(name)
            continue
        if tok.startswith("-"):
            continue                                  # short form (-c, -ngl): not a preset key
        if tok[:1].islower():
            break                                     # the description starts here
        continue                                      # a value placeholder: N, TYPE, <0|1>, 0|1
    return names


def parse_help(text: str) -> dict[str, dict]:
    """Every flag the build's help declares, with the default it states.

    One flag line names several spellings (`-c, --ctx-size N`), so a block is keyed by
    every long form it declares and keeps the block text for the report.
    """
    blocks: list[list[str]] = []
    cur: list[str] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith((" ", "\t")):
            if cur is not None:
                cur.append(line.strip())
            continue
        if _DECL_START.match(line) and not _ALL_DASHES.match(line):
            cur = [line.strip()]
            blocks.append(cur)
        else:
            cur = None                                     # section header

    out: dict[str, dict] = {}
    for block in blocks:
        decl, body = block[0], " ".join(block[1:])
        names = decl_flags(decl)
        if not names:
            continue
        stated = _DEFAULT.search(decl + " " + body)
        entry = {"decl": decl, "stated": stated.group(1).strip() if stated else None,
                 "names": names, "text": (decl + " " + body).strip()}
        for n in names:
            out[n] = entry
    return out


# ---------------------------------------------------------------- comparing
def _norm(value) -> str:
    """Normalise a default for comparison: bools are spellings, numbers are numbers."""
    s = str(value).strip().strip("'\"").lower()
    return {"true": "on", "enabled": "on", "false": "off", "disabled": "off"}.get(s, s)


def _same(a: str, b: str) -> bool:
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except ValueError:
        return False


def compare(registry: dict, server_defaults: dict, help_map: dict[str, dict]) -> list[dict]:
    """One row per registry key: what the portal omits, what the build states, the verdict.

    `preset_only` keys are compared against nothing -- they are not in the CLI help, so
    the build states no default here. The row says so instead of inventing a pass.
    """
    rows: list[dict] = []
    for key in sorted(registry):
        spec = registry[key]
        ours = spec.get("default")
        omission = server_defaults.get(key)
        entry = help_map.get(key)
        stated = entry.get("stated") if entry else None
        row = {"key": key, "kind": spec.get("kind"), "section": spec.get("section"),
               "registry_default": ours, "omitted_when_equal_to": omission,
               "build_default": stated, "decl": (entry or {}).get("decl")}
        if omission is None:
            # Checked first, on purpose: a key with no entry can never be recognised as the
            # default, so every value the operator sets is written -- including the one that
            # changes nothing, which is the artifact §4.10.0 exists to prevent. A preset-only
            # key is not an excuse for this: it has a default behaviour, it just has no CLI
            # line to read it from.
            row["verdict"] = "DRIFT: key has no SERVER_DEFAULTS entry, so it is never omitted"
        elif entry is None:
            row["verdict"] = "no CLI form (preset-only)" if spec.get("preset_only") \
                else "not in --help"
        elif stated is None or stated.strip().lower() in _NOT_A_VALUE:
            row["verdict"] = "no default stated"
        elif _same(_norm(omission), _norm(stated)):
            row["verdict"] = "match" if str(omission).strip() == stated else "spelling"
        elif _norm(spec.get("server_default")) == _norm(stated):
            row["verdict"] = "documented"          # registry says the build says otherwise
        else:
            row["verdict"] = (f"DRIFT: registry omits {omission!r}, build defaults to "
                              f"{stated!r}")
        rows.append(row)
    for key in sorted(set(server_defaults) - set(registry)):
        rows.append({"key": key, "kind": None, "section": None,
                     "registry_default": None,
                     "omitted_when_equal_to": server_defaults[key],
                     "build_default": (help_map.get(key) or {}).get("stated"),
                     "decl": (help_map.get(key) or {}).get("decl"),
                     "verdict": "server default with no registry definition (informational)"})
    return rows


def drift(rows: list[dict]) -> list[dict]:
    return [r for r in rows if str(r["verdict"]).startswith("DRIFT")]


def main() -> int:
    ap = argparse.ArgumentParser(description=SUMMARY)
    ap.add_argument("--build", help=f"server binary to ask (default: {paths.AI_BIN}/{SERVER})")
    ap.add_argument("--help-text", help="read this captured --help output instead of running one")
    ap.add_argument("--json", dest="json_out", help="write the full row list here")
    args = ap.parse_args()

    if args.help_text:
        text = pathlib.Path(args.help_text).read_text(encoding="utf-8", errors="replace")
        source = f"captured help: {args.help_text}"
    else:
        binary = server_binary(args.build)
        if binary is None:
            print(f"no server binary to ask (looked at {args.build or paths.AI_BIN / SERVER}) "
                  f"-- reporting nothing, not reporting clean", file=sys.stderr)
            return 2
        text = help_text(binary)
        source = f"{binary}"

    help_map = parse_help(text)
    rows = compare(P.SETTINGS, P.SERVER_DEFAULTS, help_map)
    bad = drift(rows)

    print(f"{source}: {len(help_map)} flags, {len(rows)} keys compared")
    print(f"{'key':24} {'omitted when':>13} {'build says':>12}  verdict")
    print("-" * 96)
    for r in rows:
        print(f"{r['key']:24} {str(r['omitted_when_equal_to']):>13} "
              f"{str(r['build_default']):>12}  {r['verdict']}")
    print()
    counts: dict[str, int] = {}
    for r in rows:
        head = str(r["verdict"]).split(":")[0]
        counts[head] = counts.get(head, 0) + 1
    print("counts:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if bad:
        print(f"\n{len(bad)} drift(s):")
        for r in bad:
            print(f"  {r['key']}: {r['verdict']}")
    if args.json_out:
        pathlib.Path(args.json_out).write_text(json.dumps(
            {"source": source, "flags": len(help_map), "rows": rows}, indent=2),
            encoding="utf-8")
        print(f"wrote {args.json_out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
