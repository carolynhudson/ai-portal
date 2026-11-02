#!/usr/bin/env python3
"""Measure what breaks when the portal is opened on a phone. Read-only.

`tools/layout_measure.py` answers "is the page well-composed" at laptop and
phone widths. This tool answers the harder question the owner's partner
raised: **what is broken** at a phone's width, in portrait. It is deliberately
not a pass/fail gate at first: the owner's rule is to measure and enumerate
before changing the design, so the probe reports and only the invariants that
already pass are pinned in `tests/test_viewport.py`.

What counts as breakage (each is a measurement, not an opinion):

  overflow     the document scrolls sideways (scrollWidth > viewport) — the
               classic portrait failure; plus the widest offenders (the
               elements whose right edge passes the viewport's right edge),
               because the page's diagnosis is "which element, not that".
  tables       a table whose nearest scrolling ancestor does not scroll:
               wide tables must live in an overflow-x:auto shell (the
               portal's `scroll` convention), and a table that does not is
               a page you must pan sideways to read.
  tap targets  buttons/links/inputs measured against the 44 CSS px Apple
               HIG minimum and a 24 px floor. "Dense on desktop" becomes
               "untappable on a phone" in exactly this column.
  text sizes   computed font sizes below 12 px, counted and sampled.
  ios zoom     inputs/selects/textareas with font-size < 16 px: on iPhone,
               focusing one ZOOMS the page (Safari's documented behaviour),
               which feels like the site is broken. This is invisible from
               the desktop and only shows on a phone.
  clipping     <pre>/<code>/<span class=mono> boxes whose content is wider
               than the box (model names, unit names, URLs) - the long
               unbreakable strings a phone cannot rewrap.

    .venv/bin/python tools/viewport_probe.py --base http://127.0.0.1:8090 \
        --cookie "aipp=TOKEN" --out ~/ai/logs/viewport-20261102

It navigates and measures. It never clicks a verb, never posts a mutating
request, never touches the router — the same discipline as layout_measure.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

# The phones, in CSS px. iPhone 15/16 portrait is 390x844; 320 is the
# smallest width a modern phone reports (and the old iPhone SE); 844x390 is
# a current iPhone in landscape, where "too narrow" turns into "too short".
VIEWPORTS = {"phone-portrait": (390, 844), "phone-narrow": (320, 568),
             "phone-landscape": (844, 390)}

# Pages come from tests/ui/spec.json so the probe cannot drift from the census.

MEASURE = """(vw) => {
  const abs = (x) => Math.round(x);
  const path = (el) => {
    let p = el.tagName.toLowerCase();
    if (el.id) return p + '#' + el.id;
    if (el.className && typeof el.className === 'string') {
      const c = el.className.trim().split(/\\s+/).slice(0, 2).join('.');
      if (c) p += '.' + c;
    }
    return p;
  };
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2 && getComputedStyle(el).visibility !== 'hidden';
  };

  // 1. horizontal overflow of the document, and who sticks out.
  const doc = document.documentElement;
  const overflowX = Math.max(doc.scrollWidth, document.body ? document.body.scrollWidth : 0) - vw;
  const offenders = [];
  for (const el of document.querySelectorAll('body *')) {
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.right > vw + 1 && r.width > 4) {
      offenders.push({sel: path(el), right: abs(r.right), w: abs(r.width)});
    }
  }
  offenders.sort((a, b) => b.right - a.right);

  // 2. tables and their scroll shells.
  const tables = [];
  for (const t of document.querySelectorAll('table')) {
    if (!vis(t)) continue;
    let node = t.parentElement, shell = null;
    while (node) {
      const ox = getComputedStyle(node).overflowX;
      if (ox === 'auto' || ox === 'scroll') { shell = node; break; }
      node = node.parentElement;
    }
    const canScroll = shell && shell.scrollWidth > shell.clientWidth + 1;
    tables.push({id: t.id || path(t), w: abs(t.getBoundingClientRect().width),
                 shell: shell ? (shell.id || shell.className || shell.tagName) : null,
                 scrolls: !!canScroll, needsScroll: t.scrollWidth > vw});
  }

  // 3. tap targets, against 44 (HIG) and 24 (floor).
  const tap = {under24: [], under44: 0, total: 0};
  for (const el of document.querySelectorAll('a, button, input[type=submit], input[type=button], select')) {
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 6 && r.height < 6) continue;   // decorative 0-size links
    tap.total += 1;
    const m = Math.min(r.width, r.height);
    if (m < 44) tap.under44 += 1;
    if (m < 24) tap.under24.push({sel: path(el), w: abs(r.width), h: abs(r.height)});
  }

  // 4. small text (computed, not source).
  const sizes = {};
  for (const el of document.querySelectorAll('body *')) {
    if (!vis(el)) continue;
    if (!el.textContent.trim()) continue;
    const fs = parseFloat(getComputedStyle(el).fontSize);
    const key = abs(fs);
    sizes[key] = (sizes[key] || 0) + 1;
  }

  // 5. iOS zoom: form fields under 16px.
  const zoom = [];
  for (const el of document.querySelectorAll('input, select, textarea')) {
    if (!vis(el)) continue;
    const fs = parseFloat(getComputedStyle(el).fontSize);
    if (fs < 16) zoom.push({sel: path(el), size: abs(fs)});
  }

  // 6. clipped monospace/long-token boxes (content wider than the box).
  const clipped = [];
  for (const el of document.querySelectorAll('pre, code, .mono')) {
    if (!vis(el)) continue;
    if (el.scrollWidth > el.clientWidth + 2)
      clipped.push({sel: path(el), text: (el.textContent || '').trim().slice(0, 60),
                    content: el.scrollWidth, box: el.clientWidth});
  }
  clipped.sort((a, b) => (b.content - b.box) - (a.content - a.box));

  return {viewport: vw, overflowX, offenders: offenders.slice(0, 12),
          tables, tap, sizes, zoom: zoom.slice(0, 12), zoomCount: zoom.length,
          clipped: clipped.slice(0, 10),
          hasViewportMeta: !!document.querySelector('meta[name=viewport]')};
}"""


def probe(base: str, cookie: str, pages: list[str], out_dir: pathlib.Path) -> dict:
    from playwright.sync_api import sync_playwright

    report: dict = {"viewports": {k: list(v) for k, v in VIEWPORTS.items()},
                    "pages": {}}
    out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 390, "height": 844},
                            device_scale_factor=2)
        if cookie:
            ctx.add_cookies([{"name": kv[0], "value": kv[1], "url": base}
                             for kv in [c.split("=", 1) for c in cookie.split(";") if "=" in c]])
        page = ctx.new_page()
        for path in pages:
            page.goto(f"{base}{path}", wait_until="networkidle")
            page.wait_for_timeout(700)
            per = {}
            for name, (w, h) in VIEWPORTS.items():
                page.set_viewport_size({"width": w, "height": h})
                page.wait_for_timeout(350)
                per[name] = page.evaluate(MEASURE, w)
                shot = out_dir / f"{path.strip('/').replace('/', '-') or 'root'}-{name}.png"
                page.screenshot(path=str(shot), full_page=False)
            report["pages"][path] = per
        b.close()
    (out_dir / "report.json").write_text(json.dumps(report, indent=1))
    (out_dir / "report.md").write_text(markdown(report))
    return report


def markdown(rep: dict) -> str:
    lines = ["# Viewport probe", "",
             f"Viewports: {', '.join(f'{k} {v[0]}x{v[1]}' for k, v in rep['viewports'].items())}", ""]
    for path, per in rep["pages"].items():
        lines.append(f"## {path}")
        for name, d in per.items():
            lines.append(f"### {name} ({d['viewport']} px)")
            lines.append(f"- meta viewport tag: {'yes' if d['hasViewportMeta'] else 'MISSING'}")
            lines.append(f"- horizontal overflow: **{d['overflowX']} px**"
                         + (f" — offenders: " + "; ".join(
                            f"{o['sel']} (right {o['right']}, w {o['w']})"
                            for o in d["offenders"][:5]) if d["offenders"] else ""))
            wide = [t for t in d["tables"] if t["needsScroll"] and not t["scrolls"]]
            lines.append(f"- tables needing scroll without a scroll shell: "
                         + (", ".join(t["id"] for t in wide) if wide else "none"))
            lines.append(f"- tap targets: {d['tap']['under44']}/{d['tap']['total']} under 44px "
                         f"(HIG), {len(d['tap']['under24'])} under 24px"
                         + (f" — worst: " + "; ".join(f"{o['sel']} {o['w']}x{o['h']}"
                            for o in d["tap"]["under24"][:5]) if d["tap"]["under24"] else ""))
            small = {k: v for k, v in d["sizes"].items() if int(k) < 12}
            lines.append(f"- text under 12px: " + (json.dumps(small) if small else "none"))
            lines.append(f"- iOS focus-zoom fields (<16px): {d['zoomCount']}"
                         + (f" — e.g. " + "; ".join(f"{z['sel']} {z['size']}px"
                            for z in d["zoom"][:5]) if d["zoom"] else ""))
            lines.append(f"- clipped long-token boxes: "
                         + (f"{len(d['clipped'])} — e.g. " + "; ".join(
                            f"\"{c['text'][:40]}\" {c['content']}>{c['box']}"
                            for c in d["clipped"][:4]) if d["clipped"] else "none"))
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8090")
    ap.add_argument("--cookie", default="")
    ap.add_argument("--pages", nargs="*", default=None)
    ap.add_argument("--out", default=str(pathlib.Path.home() / "ai/logs/viewport-probe"))
    a = ap.parse_args()
    pages = a.pages
    if not pages:
        spec = json.loads((pathlib.Path(__file__).resolve().parent.parent
                           / "tests" / "ui" / "spec.json").read_text())
        pages = [p["path"] for p in spec["pages"] if p.get("auth", True)]
    rep = probe(a.base, a.cookie, pages, pathlib.Path(a.out).expanduser())
    print(markdown(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
