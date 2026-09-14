"""Prove the UI tests can fail.

A test suite that has never failed on a real bug proves nothing, and a harness
that drives 96 controls could in principle be clicking into the void. This
script introduces one small, realistic regression at a time into the real templates
and stylesheet, runs the UI suite against it, and requires a failure that names the
right thing. If you change the harness and this still passes, the harness got
weaker.

    ./scripts/check-ui.sh --mutations           # every mutation, ~10 min
    python tests/ui/mutation_check.py --help    # list them
    python tests/ui/mutation_check.py "tab strip floats above its panels"   # one
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.ui_sandbox import Sandbox  # noqa: E402

WEB = ROOT / "web"   # mutations are paths under web/

# (name, file, old text, new text, page, substring the failure must contain)
MUTATIONS = [
    (
        "element id renamed (rollback select)",
        "templates/dashboard.html",
        'id="rollback-select"',
        'id="rollback-chooser"',
        "dashboard",
        "rollback-select",   # the page still looks for the old id
    ),
    (
        "tab handler deleted",
        "templates/dashboard.html",
        "function switchUpdateTab(tab, el){",
        "function _switchUpdateTab_renamed(tab, el){",
        "dashboard",
        "switchUpdateTab is not defined",
    ),
    (
        # the bug that actually shipped: an apostrophe inside a single-quoted
        # JS string. Jinja-safe, so it is a pure JavaScript syntax error
        "a string literal broken, killing its script block",
        "templates/dashboard.html",
        "alert('update refused: '",
        "alert('update refused don't: '",
        "dashboard",
        "uncaught error while loading",
    ),
    (
        "endpoint renamed in a page fetch",
        "templates/dashboard.html",
        "fetch('/api/gpu'",
        "fetch('/api/gpu-x'",
        "dashboard",
        "/api/gpu-x",
    ),
    (
        "tab panel removed",
        "templates/dashboard.html",
        '<div id="update-python" class="update-tab-content">',
        '<div id="update-pythonX" class="update-tab-content">',
        "dashboard",
        "update-python",
    ),
    (
        "destructive button no longer confirms",
        "templates/dashboard.html",
        "if (!confirm('Roll back to ' + name",
        "if (false && !confirm('Roll back to ' + name",
        "dashboard",
        "without asking for confirmation",
    ),
    (
        "service stop button skips its confirm",
        "templates/dashboard.html",
        "  if (action !== 'start' &&\n      !confirm(meta.confirm",
        "  if (false &&\n      !confirm(meta.confirm",
        "dashboard",
        "without asking for confirmation",
    ),
    (
        "settings save posts the wrong field",
        "templates/settings.html",
        "body: JSON.stringify({bind, address})});",
        "body: JSON.stringify({bindMode: bind, address})});",
        "settings",
        "/api/settings/services/llamacpp/bind",   # the endpoint refuses it
    ),
    (
        "dead button: handler emptied",
        "templates/settings.html",
        '<button onclick="saveSharedEnv()">Save</button>',
        "<button>Save</button>",
        "settings",
        "dead control",
    ),
    (
        # the reported bug itself: the strip's bottom margin floats the panels
        # 16px below the baseline the tabs sit on
        "tab strip floats above its panels",
        "static/tabs.css",
        "  gap:0;\n  margin:0;",
        "  gap:0;\n  margin:0 0 16px 0;",
        "dashboard",
        "margin-bottom 16px",
    ),
    (
        # the fix for that bug taken too far: overlapping with the card instead
        # of the tab paints the opaque card over the baseline
        "panel card pulled up over the baseline",
        "static/tabs.css",
        "  margin-top:0;\n  border-top:none;",
        "  margin-top:-1px;\n  border-top:none;",
        "dashboard",
        "paints over the baseline",
    ),
    (
        "tabs no longer reach the baseline they stand on",
        "static/tabs.css",
        "  margin-bottom:-1px;",
        "  margin-bottom:0;",
        "dashboard",
        "1px onto the baseline",
    ),
    (
        "active tab no longer painted in the panel's colour",
        "static/tabs.css",
        "  background:var(--panel);\n  color:var(--fg);",
        "  background:var(--bg);\n  color:var(--fg);",
        "dashboard",
        "must be var(--panel)",
    ),
    (
        "active tab no longer opens the baseline",
        "static/tabs.css",
        "  border-bottom:1px solid var(--panel);",
        "  border-bottom:1px solid var(--line);",
        "dashboard",
        "must open the baseline",
    ),
    (
        "panel card keeps a round top corner against the square tabs",
        "static/tabs.css",
        "  border-top-left-radius:0;\n  border-top-right-radius:0;\n}",
        "}",
        "dashboard",
        "top-left corner",
    ),
    # (a template gaining an hx-* attribute is caught by
    # test_ui_contract.test_templates_carry_no_htmx_attributes, not by the DOM
    # harness, so it is not a mutation for this tool)
]


def _clean_web() -> bool:
    """This tool edits the real templates and stylesheet in place (the app
    resolves its web tree from its own location), so refuse to start from a
    dirty tree: if it ever died mid-run, `git checkout -- web` recovers without
    eating anyone's work."""
    import subprocess

    out = subprocess.run(["git", "status", "--porcelain", "--", "web"],
                         cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if out:
        print("refusing to run: web/ has uncommitted changes\n" + out)
        print("commit or stash first — this tool temporarily edits those files")
        return False
    return True


def apply_and_test(filename, old, new, page, expect, keep_on_miss=False):
    """Mutate a real file, drive the page, require a matching failure.

    Restores the file unless the suite missed the bug and keep_on_miss is set,
    in which case the mutated tree is left behind to be inspected."""
    target = WEB / filename
    original = target.read_text()
    if old not in original:
        return False, f"mutation target not found in {filename}: {old[:60]!r}"
    try:
        target.write_text(original.replace(old, new, 1))
        with Sandbox() as sbx:
            report = sbx.page(page)
    except Exception as exc:                       # noqa: BLE001 — report, don't crash
        return False, f"harness blew up: {str(exc).splitlines()[0][:160]}"
    failures = report.get("failures", [])
    blob = "\n".join(failures)
    if report.get("ok"):
        caught, detail = False, "suite PASSED despite the injected bug — the harness is blind to it"
    elif expect and expect not in blob:
        caught, detail = False, f"failed, but not with a message naming {expect!r}:\n{blob[:600]}"
    else:
        caught, detail = True, next((ln for ln in failures if not expect or expect in ln), "failed")
    if caught or not keep_on_miss:
        target.write_text(original)
        if target.read_text() != original:
            print(f"   !! {filename} was NOT restored — run: git checkout -- {target}")
    return caught, detail


def main(argv: list[str]) -> int:
    if {"-h", "--help"} & set(argv):
        print(__doc__.rstrip() + "\n\navailable mutations:")
        for m in MUTATIONS:
            print(f"  {m[0]}  [{m[1]} -> /{m[4]}]")
        return 0
    keep_on_miss = "--keep-on-miss" in argv
    names = [a for a in argv if not a.startswith("-")]
    selected = [m for m in MUTATIONS if not names or m[0] in names]
    if not selected:
        print("no such mutation; available:")
        for m in MUTATIONS:
            print(f"  {m[0]}")
        return 2
    if not _clean_web():
        return 2

    results = []
    for name, filename, old, new, page, expect in selected:
        print(f"* {name}", flush=True)
        caught, detail = apply_and_test(filename, old, new, page, expect, keep_on_miss)
        print(f"    {'caught' if caught else 'MISSED'}: {str(detail)[:170]}")
        results.append((name, caught))

    missed = [n for n, ok in results if not ok]
    print()
    print(f"{len(results) - len(missed)}/{len(results)} injected regressions caught")
    if missed:
        print("the UI suite does NOT detect:")
        for n in missed:
            print(f"  - {n}")
        if keep_on_miss:
            print("\nthe last mutated file was left in place; reset with: git checkout -- web")
        return 1
    print("the UI suite detects every injected regression; the tree is back at HEAD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
