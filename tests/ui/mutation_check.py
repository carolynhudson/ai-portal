"""Prove the UI tests can fail.

A test suite that has never failed on a real bug proves nothing, and a harness
that drives 96 controls could in principle be clicking into the void. This
script introduces one small, realistic regression at a time into a *copy* of the
templates, runs the UI suite against it, and requires a failure that names the
right thing. If you change the harness and this still passes, the harness got
weaker.

    uv run python -m tests.ui.mutation_check          # ~2 min
    uv run python -m tests.ui.mutation_check --keep   # keep the mutated tree for inspection
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.ui_sandbox import Sandbox  # noqa: E402

TEMPLATES = ROOT / "web" / "templates"

# (name, file, old text, new text, page, substring the failure must contain)
MUTATIONS = [
    (
        "element id renamed (rollback select)",
        "dashboard.html",
        'id="rollback-select"',
        'id="rollback-chooser"',
        "dashboard",
        "rollback-chooser",
    ),
    (
        "tab handler deleted",
        "dashboard.html",
        "function switchUpdateTab(tab, el){",
        "function _switchUpdateTab_renamed(tab, el){",
        "dashboard",
        "switchUpdateTab is not defined",
    ),
    (
        "a whole script block made unparseable",
        "dashboard.html",
        "function renderUpdRemote(rem){",
        "function renderUpdRemote(rem){{",
        "dashboard",
        "uncaught error while loading",
    ),
    (
        "endpoint renamed in a page fetch",
        "dashboard.html",
        "fetch('/api/gpu'",
        "fetch('/api/gpu-x'",
        "dashboard",
        "/api/gpu-x",
    ),
    (
        "tab panel removed",
        "dashboard.html",
        '<div id="update-python" class="update-tab-content">',
        '<div id="update-pythonX" class="update-tab-content">',
        "dashboard",
        "update-python",
    ),
    (
        "destructive button no longer confirms",
        "dashboard.html",
        "if (!confirm('Roll back to ' + name",
        "if (false && !confirm('Roll back to ' + name",
        "dashboard",
        "without asking for confirmation",
    ),
    (
        "settings save posts the wrong field",
        "settings.html",
        "body: JSON.stringify({bind, address})});",
        "body: JSON.stringify({bindMode: bind, address})});",
        "settings",
        "missing",
    ),
    (
        "dead button: handler emptied",
        "settings.html",
        '<button onclick="saveSharedEnv()">Save</button>',
        "<button>Save</button>",
        "settings",
        "dead control",
    ),
    # (a template gaining an hx-* attribute is caught by
    # test_ui_contract.test_templates_carry_no_htmx_attributes, not by the DOM
    # harness, so it is not a mutation for this tool)
]


def _clean_templates() -> bool:
    """This tool edits the real templates temporarily (the app resolves its web
    tree from its own location), so refuse to start from a dirty tree: if it
    ever died mid-run, `git checkout web/templates` recovers without eating
    anyone's work."""
    import subprocess

    out = subprocess.run(["git", "status", "--porcelain", "--", "web/templates"],
                         cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if out:
        print("refusing to run: web/templates is not committed\n" + out)
        print("commit or stash first — this tool temporarily edits those files")
        return False
    return True


def apply_and_test(name, filename, old, new, page, expect, keep=False) -> tuple[bool, str]:
    """Mutate the template, drive the page, require a matching failure."""
    target = TEMPLATES / filename
    original = target.read_text()
    if old not in original:
        return False, f"mutation target not found in {filename}: {old[:50]!r}"
    mutated = original.replace(old, new, 1)
    try:
        target.write_text(mutated)
        with Sandbox() as sbx:
            report = sbx.page(page)
        failures = report.get("failures", [])
        blob = "\n".join(failures)
        if report.get("ok"):
            return False, "suite PASSED despite the injected bug — the harness is blind to it"
        if expect and expect not in blob:
            return False, f"failed, but not with a message naming {expect!r}:\n{blob[:600]}"
        return True, next(ln for ln in failures if not expect or expect in ln)
    finally:
        target.write_text(original)
        restored = target.read_text() == original
        if not restored:
            print(f"   !! {filename} was NOT restored — run: git checkout {target}")


def main(argv: list[str]) -> int:
    keep = "--keep" in argv
    names = [a for a in argv if not a.startswith("-")]
    selected = [m for m in MUTATIONS if not names or m[0] in names]
    if not _clean_templates():
        return 2
    results = []
    for mut in selected:
        name, filename, old, new, page, expect = mut
        print(f"* {name}", flush=True)
        ok, detail = apply_and_test(name, filename, old, new, page, expect, keep)
        print(f"    {'caught' if ok else 'MISSED'}: {str(detail)[:160]}")
        results.append((name, ok))
    missed = [n for n, ok in results if not ok]
    print()
    print(f"{len(results) - len(missed)}/{len(results)} injected regressions caught")
    if missed:
        print("the UI suite does NOT detect:")
        for n in missed:
            print(f"  - {n}")
        return 1
    print("the UI suite detects every injected regression")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
