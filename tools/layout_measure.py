#!/usr/bin/env python3
"""Measure the portal's layout as a browser renders it.

The UI harness drives the real pages in jsdom, which proves controls, requests, dialogs, states and
text. jsdom has no layout engine: it cannot tell you that the personalities editor is six viewports
tall, that a card is 57 px of chrome around one line, or how much of the status page is above the
fold on a laptop. So every statement about spacing and hierarchy in `docs/PORTAL_UI.md` was, until
this tool existed, prose that nothing measured.

This is the instrument behind `docs/PORTAL_LAYOUT.md` L6's layout criterion. It opens pages in
Chromium, logs in the way the operator does, and reports geometry:

  scroll height, blocks per page, how many are above the fold, the tallest block, the vertical gaps
  between blocks in the same column, boxes that intersect, and the 1280x800 answer to "can I read
  this page without scrolling".

It is read-only: it navigates, it fills the login field, and it evaluates geometry. It never clicks
a verb, never posts a mutating request, and never touches the router.

    uv pip install playwright && uv run python -m playwright install chromium
    uv run python tools/layout_measure.py                      # the live portal, both viewports
    uv run python tools/layout_measure.py --base http://127.0.0.1:8090 --pages / /models
"""
from __future__ import annotations

import argparse
import pathlib
import sys

VIEWPORTS = {"laptop": (1280, 800), "phone": (390, 844)}
PAGES = ["/", "/models", "/personalities", "/health", "/settings", "/help"]

# Top-level blocks only. `.card` is the portal's block; pages that build their own layout
# (personalities, help) use `main` children, so the selector falls back. Nested cards are excluded:
# a card inside a card is one visual block, and counting it as a neighbour is how a measurement
# reports "overlap" where there is only structure.
GEOMETRY = """(viewport) => {
  const vis = (el) => { const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2 && getComputedStyle(el).visibility !== 'hidden'; };
  const hasCards = document.querySelectorAll('.card').length > 0;
  const sel = hasCards ? '.card' : 'main > section, main > div, main > table, main > form, main > pre';
  const blocks = [...document.querySelectorAll(sel)].filter(vis).filter((el) =>
    !el.closest(hasCards ? '.card' : 'main').parentElement?.closest('.card'));
  const box = (el) => { const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y + window.scrollY),
            w: Math.round(r.width), h: Math.round(r.height),
            title: ((el.querySelector('h2,h3') || {}).textContent || el.tagName).trim().slice(0, 44)}; };
  const items = blocks.map(box).sort((a, b) => (a.y - b.y) || (a.x - b.x));
  const fold = viewport.h;
  // Vertical gaps between blocks in the same column: the cramping measure.
  const gaps = [];
  for (let i = 0; i < items.length; i++) {
    for (let j = i + 1; j < items.length; j++) {
      const a = items[i], b = items[j];
      const sameColumn = a.x < b.x + b.w && b.x < a.x + a.w;
      if (sameColumn && b.y >= a.y + a.h) { gaps.push(b.y - (a.y + a.h)); break; }
    }
  }
  let intersect = 0;
  for (let i = 0; i < items.length; i++) {
    for (let j = i + 1; j < items.length; j++) {
      const a = items[i], b = items[j];
      if (a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h) intersect++;
    }
  }
  const sizes = {};
  for (const el of [...document.querySelectorAll('body *')].filter(vis)) {
    const px = getComputedStyle(el).fontSize;
    sizes[px] = (sizes[px] || 0) + 1;
  }
  return {
    scrollHeight: document.documentElement.scrollHeight,
    blocks: items,
    aboveFold: items.filter((b) => b.y < fold).length,
    tallest: items.length ? items.reduce((m, b) => (b.h > m.h ? b : m)) : null,
    gaps: gaps.sort((a, b) => a - b),
    intersectingPairs: intersect,
    fontSizes: Object.entries(sizes).sort((a, b) => b[1] - a[1]).slice(0, 6),
  };
}"""


def measure(base: str, pages: list[str], viewport_name: str, password: str) -> int:
    from playwright.sync_api import sync_playwright

    w, h = VIEWPORTS[viewport_name]
    failures = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": w, "height": h})
        page.goto(f"{base}/login", wait_until="load")
        page.fill("input[type=password]", password)
        page.press("input[type=password]", "Enter")
        page.wait_for_url("**/", timeout=20000)
        print(f"# {base} at {w}x{h} ({viewport_name})")
        for path in pages:
            page.goto(base + path, wait_until="load")
            page.wait_for_selector("main", timeout=10000)
            d = page.evaluate(GEOMETRY, {"w": w, "h": h})
            tallest = d["tallest"]
            print(f"\n## {path}  —  {d['scrollHeight']} px scroll, {len(d['blocks'])} blocks, "
                  f"{d['aboveFold']} above the fold, {d['intersectingPairs']} intersecting pairs")
            print(f"   tallest block: {tallest['h']} px  ({tallest['title']})" if tallest else "   no blocks")
            print(f"   blocks: {[b['h'] for b in d['blocks']]}")
            print(f"   column gaps: {d['gaps']}")
            print(f"   font sizes in use: {dict(d['fontSizes'])}")
            if tallest and tallest["h"] > 3 * h:
                print(f"   MORE THAN THREE VIEWPORTS in one block: {tallest['title']}")
                failures += 1
            if d["intersectingPairs"]:
                print(f"   boxes intersect: {d['intersectingPairs']} pair(s)")
                failures += 1
        browser.close()
    return failures


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Measure the portal's layout in a real browser: geometry, fold coverage, gaps.")
    ap.add_argument("--base", default="http://127.0.0.1:8090")
    ap.add_argument("--pages", nargs="*", default=PAGES)
    ap.add_argument("--viewport", default="laptop", choices=sorted(VIEWPORTS))
    ap.add_argument("--json", action="store_true", help="one JSON object per page")
    args = ap.parse_args(argv)
    pw_file = pathlib.Path.home() / "ai/state/portal/PORTAL_PASSWORD.txt"
    if not pw_file.is_file():
        print(f"no portal password file at {pw_file}; the portal is auth-gated", file=sys.stderr)
        return 2
    password = pw_file.read_text().strip()
    return 0 if measure(args.base, args.pages, args.viewport, password) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
