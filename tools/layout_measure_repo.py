#!/usr/bin/env python3
"""Measure the layout of the code in THIS checkout, in a browser.

`layout_measure.py` measures a *server*. The live portal serves the code it started with, so a page
measured against it reports that server's layout, not the repo's -- POLISH §7 records a 420 px
removal that did not move the measured number for exactly this reason. This is the other half of the
instrument: it starts the portal from this working tree, in the sandbox home the UI harness uses
(models, presets, a project registry, `HOME` moved so nothing can reach the operator's real
`~/ai` or `~/ai-projects`), runs the same Chromium geometry pass, and prints the number with the
commit it belongs to.

Read-only, like the instrument it drives: it navigates, logs in, and evaluates geometry. It never
clicks a verb and never posts a mutating request.

    .venv/bin/python tools/layout_measure_repo.py                 # the repo's code, both viewports
    .venv/bin/python tools/layout_measure_repo.py --pages /personalities --viewport phone

The sandbox is seeded by the UI integration suite's fixture, imported here unchanged: a page measured
in an unseeded home renders empty states, and an empty state measured is a number about the fixture.
"""
from __future__ import annotations

import argparse
import contextlib
import pathlib
import subprocess
import sys
from collections.abc import Iterator

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# The integration suite imports its GGUF writer from `tests/` as a top-level module, the way pytest
# imports it. A plain script has to make that importable too.
sys.path.insert(0, str(ROOT / "tests"))

# Importing the integration suite's module is deliberate. Its session fixture is the sandbox --
# seeding, env, the uvicorn subprocess, the strays it owns and kills. Reimplementing it here would
# be a second sandbox that drifts from the one the UI gate runs, and a layout number measured in a
# sandbox nobody else uses is a number nobody can reproduce.
import tests.test_ui_integration as ui  # noqa: E402
from tools.layout_measure import VIEWPORTS, measure  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Measure THIS checkout's pages in Chromium, in the UI harness's sandbox home.")
    ap.add_argument("--pages", nargs="*", default=["/personalities", "/projects", "/"])
    ap.add_argument("--viewports", nargs="*", default=sorted(VIEWPORTS),
                    help="viewport names to report (default: all)")
    ap.add_argument("--keep", action="store_true",
                    help="print the sandbox address and wait, so a browser can be aimed at it")
    args = ap.parse_args(argv)

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
                            capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT),
                           capture_output=True, text=True).stdout.strip()
    print(f"# repo code: {commit}{' +uncommitted' if dirty else ''}")

    with ui_server() as server:
        print(f"# sandbox: {server['base']}  home: {server['home']}")
        if args.keep:
            print(f"# --keep: aim a browser at {server['base']} (password {ui.PASSWORD}); Ctrl-C to stop")
            import time
            try:
                time.sleep(3600)
            except KeyboardInterrupt:
                return 0
        failures = 0
        for vp in args.viewports:
            failures += measure(str(server["base"]), args.pages, vp, ui.PASSWORD)
        return 1 if failures else 0


@contextlib.contextmanager
def ui_server() -> Iterator[dict]:
    """Drive the session-scoped `ui_server` fixture without a pytest invocation.

    A session fixture is a generator with one `yield`; the decorated function is reached through
    pytest's own accessor, not a dunder. Doing it here keeps the sandbox's ownership rules (stray
    process killed, uvicorn terminated, log closed) exactly where the suite defines them.
    """
    gen = ui.ui_server._fixture_function()
    try:
        yield next(gen)
    finally:
        with contextlib.suppress(StopIteration):
            next(gen, None)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
