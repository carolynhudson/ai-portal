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

WEB = ROOT / "web"   # template/static mutations are relative to here
APP = ROOT           # backend mutations are written "app/..." and relative to here

# (name, file, old text, new text, page, substring the failure must contain)
MUTATIONS = [
    (
        # The bar is the only thing standing between "I started a build" and "I
        # navigated away and never heard back". It has to name the job.
        "job bar stops naming the running job (jobbar-names)",
        "templates/base.html",
        "txt.innerHTML = '<b>' + esc(run.label) + '</b> ' + esc(run.target || '') +",
        "txt.innerHTML = '<b>working</b> ' + esc(run.target || '') +",
        "jobbar",
        "ComfyUI update",
    ),
    (
        "job bar loses the current step",
        "templates/base.html",
        "(run.step ? ' <span class=\"jobbar-step\">— ' + esc(run.step) + '</span>' : '')",
        "''",
        "jobbar",
        "holding 4 machine-specific package(s)",
    ),
    (
        # One line does both halves of the lock: it marks <body> busy for CSS and
        # for every other script, and it is the flag the whole page reads. Losing
        # it un-styled the bar's controls *and* left the machine unlocked while the
        # bar itself kept animating — which is the insidious part, and why the
        # spec asserts the attribute, not just a disabled button.
        "job bar stops locking the machine (jobbar-unlocks)",
        "templates/base.html",
        "document.body.setAttribute('data-jobbusy', run ? '1' : '');",
        "document.body.setAttribute('data-jobbusy', '');",
        "jobbar",
        "body[data-jobbusy]",
    ),
    (
        "finished job is reported as a success whatever happened",
        "templates/base.html",
        "(last.status === 'success' ? 'done' : 'failed');",
        "(last.status === 'success' ? 'done' : 'done');",
        "jobbar-finished",
        "#jobbar.failed",
    ),
    (
        # The server refuses a second disruptive job whatever the browser does;
        # the bar locking the controls is the *courtesy*, and the spec claims it
        # too, so it has to be tested as well.
        "a running job no longer locks the update controls",
        "templates/base.html",
        "      el.disabled = true;",
        "      el.disabled = el.disabled;",
        "jobbar",
        "#comfy-upd-btn",
    ),
    (
        "element id renamed (build dropdown)",
        "templates/dashboard.html",
        'id="build-select"',
        'id="build-chooser"',
        "dashboard",
        "build-select",   # expectVisible/expectDisabled still look for the old id
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
        "switch build no longer confirms before it happens",
        "templates/dashboard.html",
        "  if (!confirm(`Switch to this build?",
        "  if (false && !confirm(`Switch to this build?",
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
        # the delete button's whole safety story: the running build stays locked
        "delete unlocks for the build the router is running",
        "templates/dashboard.html",
        "  del.disabled = !e || !!e.active;",
        "  del.disabled = !e;",
        "dashboard",
        "must stay locked",
    ),
    (
        "build deletion no longer confirms before it happens",
        "templates/dashboard.html",
        "  if (!confirm(`DELETE THIS BUILD?${buildFacts(e)}This deletes it from disk. It cannot be undone — rebuilding takes 10–20 minutes.`)) return;",
        "  // confirm removed by mutation",
        "dashboard",
        "without asking for confirmation",
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
    # --- the router pulse pane: lanes, throughput, attention -----------------
    (
        # the board renders once and then stops tracking the router: exactly the
        # complaint the pane exists to fix, in new form
        "slot board stops reporting what each slot is doing",
        "templates/dashboard.html",
        "    k[1].textContent = PHASE[s.state] || s.state;",
        "    k[1].textContent = 'idle';",
        "dashboard",
        "generating",
    ),
    (
        "throughput readout never fills in",
        "templates/dashboard.html",
        "  setText('tps-now', latest != null ? Number(latest).toFixed(1) : '—');",
        "  setText('tps-now', '—');",
        "dashboard",
        "#tps-now",
    ),
    (
        # the Jinja lane loop is first paint only — the script is the renderer —
        # so what has to break for the board to be gone is the node it draws into
        "lane board container renamed, the script has nothing to draw into",
        "templates/dashboard.html",
        '<div class="lanes" id="pulse-lanes" aria-label="router slots">',
        '<div class="lanes" id="pulse-rows" aria-label="router slots">',
        "dashboard",
        "#pulse-lanes",
    ),
    (
        "attention toggle stops switching the pane",
        "templates/dashboard.html",
        "function toggleFeedView(){",
        "function toggleFeedView(){ return;",
        "dashboard",
        "dead control",
    ),
    (
        # llama.cpp changes a log spelling, the parser stops matching, and every
        # slot silently looks idle
        "generation lines no longer recognised (parser rot)",
        "app/core/rlog.py",
        'GEN_RE = re.compile(r"\\bn_gen\\s*=\\s*(\\d+),',
        'GEN_RE = re.compile(r"\\bn_gne\\s*=\\s*(\\d+),',
        "dashboard",
        "#pulse-lanes .lane.s-generate",
    ),
    (
        "a truncated answer stops being called out",
        "app/core/rlog.py",
        '                slot.truncated = bool(rm and rm.group(2) != "0")',
        '                slot.truncated = False',
        "dashboard",
        "context limit",
    ),
    (
        "attention items lose their text",
        "app/core/rlog.py",
        'attention.append({"level": level, "text": _clip(text),',
        'attention.append({"level": level, "text": "",',
        "dashboard",
        "prompt cache pressure",
    ),
    # (a template gaining an hx-* attribute is caught by
    # test_ui_contract.test_templates_carry_no_htmx_attributes, not by the DOM
    # harness, so it is not a mutation for this tool)
    (
        # the health pane exists so something gets acted on; an empty list is the
        # failure mode that matters most, and it looks like a quiet machine
        "health pane stops listing what needs attention",
        "templates/health.html",
        "{% for a in advisories %}",
        "{% for a in [] %}",
        "health",
        "amdrocm-base9.9",
    ),
    (
        "health pane loses the model-store card",
        "templates/health.html",
        '<div class="card" id="model-store">',
        '<div class="card" id="store-model">',
        "health",
        "#model-store",
    ),
    (
        "disk sizes arrive as raw byte counts",
        "templates/health.html",
        "{{ t.bytes|sz }}",
        "{{ t.bytes }}",
        "health",
        "#disk table td.mono",
    ),
    (
        # the ROCm coupling table is the reason the pane exists: without the
        # installed version every advisory about version drift is guesswork
        "health pane loses the installed ROCm version",
        "templates/health.html",
        '<b>{{ rocm.installed or "—" }}</b>',
        '<b>"—"</b>',
        "health",
        "9.9.0",
    ),
    (
        # The whole safety story of the purge control is which selection unlocks
        # it. The server refuses whatever the browser shows, but a button that
        # invites the click for a build the router is configured to run is a
        # button that teaches people to ignore warnings.
        "an in-use build becomes purgeable",
        "templates/dashboard.html",
        "  btn.disabled = !e || !e.purgeable;",
        "  btn.disabled = !e;",
        "builds-strays",
        "#purge-stray-btn",
    ),
    (
        "the unregistered-build row never hides",
        "templates/dashboard.html",
        "  row.hidden = list.length === 0;",
        "  row.hidden = false;",
        "dashboard",
        "#stray-row",
    ),
    (
        "the unregistered-build row never shows",
        "templates/dashboard.html",
        "  row.hidden = list.length === 0;",
        "  row.hidden = true;",
        "builds-strays",
        "#stray-row",
    ),
    (
        # "798 MB" is the part that makes it worth acting on: a list of directory
        # names with no weight attached is not a reason to delete anything
        "the weight of what is unregistered is dropped",
        "templates/dashboard.html",
        "'Unregistered (' + humanSize(d.stray_bytes || 0) + '):'",
        "'Unregistered (' + '' + '):'",
        "builds-strays",
        "798 MB",
    ),
    (
        # The editor's fields are seeded from the personality's own settings, carried on the
        # option by personality.settings_view() (plan §6.14.1i). Dropping the seed leaves a
        # correct-looking page with empty fields -- and an empty field next to a correct label is
        # indistinguishable from a filled one, which is why the spec asserts values.
        "the editor stops showing the personality's current settings",
        "templates/personalities.html",
        "      if (control) control.value = st.global[f.dataset.key] || '';",
        "      if (control) control.value = '';",
        "personalities",
        "#ed-g-threads-val: value is",
    ),
    (
        # "no changes" has to name the configuration it compared against. A generic sentence is
        # true of an editor that lost the comparison, which is the whole claim.
        "the summary stops naming the settings it compared",
        "templates/personalities.html",
        "      el.textContent = `no changes — these are ${name}'s current settings: ${Object.keys(st.global).length}`",
        "      el.textContent = 'no edits proposed'",
        "personalities",
        "UI-Test-Work's current settings",
    ),
    (
        # The start button is the verb the process census exists for. A `llama-server` started by
        # hand on the router's port makes `systemctl start` succeed while every client is talking to
        # the other process; losing the gate puts a machine-mutating POST back on a bare click.
        "starting the router stops consulting the process census (census-start-ungated)",
        "templates/dashboard.html",
        "  if (key === 'llamacpp' && action === 'start'){",
        "  if (false){",
        "dashboard",
        "/api/services/llamacpp/start",
    ),
    (
        # The census's most load-bearing sentence is which llama-server processes the portal cannot
        # touch. Softening it to "none" is exactly the failure the panel was built to prevent.
        "the census stops naming the servers it does not control (census-blanket-ok)",
        "templates/dashboard.html",
        "      {{ c.foreign | length }} llama-server process{{ 'es' if c.foreign | length != 1 }} the portal does",
        "      no server processes outside the unit: every llama-server on the box is",
        "dashboard",
        "llama-server",
    ),

]


def target_of(filename: str) -> Path:
    """Mutation paths are relative to web/, except backend ones written app/..."""
    return (ROOT / filename) if filename.startswith("app/") else WEB / filename


def _clean(targets) -> bool:
    """This tool edits files in place, so refuse to touch one that already
    differs from HEAD: if a run ever died midway, `git checkout -- <file>`
    recovers it without eating anyone's work."""
    import subprocess

    ok = True
    for target in sorted(set(targets), key=str):
        rel = target.relative_to(ROOT)
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", str(rel)],
                                 cwd=ROOT, capture_output=True, text=True)
        if tracked.returncode != 0:
            print(f"refusing to run: {rel} is not tracked by git")
            ok = False
            continue
        dirty = subprocess.run(["git", "status", "--porcelain", "--", str(rel)],
                               cwd=ROOT, capture_output=True, text=True).stdout.strip()
        if dirty:
            print(f"refusing to run: {rel} has uncommitted changes\n{dirty}")
            ok = False
    if not ok:
        print("commit or stash first — this tool temporarily edits these files")
    return ok


def apply_and_test(filename, old, new, page, expect, keep_on_miss=False):
    """Mutate a real file, drive the page, require a matching failure.

    Restores the file unless the suite missed the bug and keep_on_miss is set,
    in which case the mutated tree is left behind to be inspected."""
    target = target_of(filename)
    original = target.read_text()
    if old not in original:
        return False, f"mutation target not found in {filename}: {old[:60]!r}"
    target.write_text(original.replace(old, new, 1))
    # Restore in `finally`. Restoring from the error path instead left the bug
    # in the tree whenever the sandbox died (running this file with plain
    # `python` picks the venv-less interpreter, and one missed mutation then
    # poisoned every run after it — and the guard that refuses to run on a
    # dirty tree reported it as "refusing to run", three times, as if the tool
    # were refusing rather than having just trashed a file).
    try:
        with Sandbox() as sbx:
            report = sbx.page(page)
    except Exception as exc:                       # noqa: BLE001 — report, don't crash
        # The sandbox raises with its log tail; without it a sandbox-side
        # failure is just "harness blew up: <first line>" and says nothing.
        import traceback
        detail = "\n".join(traceback.format_exception(exc)[-3:])
        print(f"    harness error:\n      " + detail.strip().replace("\n", "\n      ")[:900])
        return False, f"harness blew up: {str(exc).splitlines()[0][:160]}"
    finally:
        if not keep_on_miss:
            target.write_text(original)
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
    if not _clean([target_of(f) for _, f, *_ in selected]):
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
            print("\nthe last mutated file was left in place; reset it with git checkout")
        return 1
    print("the UI suite detects every injected regression; the tree is back at HEAD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
