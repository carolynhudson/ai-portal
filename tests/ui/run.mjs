// Drive one page of the AI Portal UI in jsdom and report what happened.
//
//   node run.mjs --base http://127.0.0.1:PORT --spec spec.json --page dashboard
//
// Prints one JSON object on stdout. Failures are assertions in the report, not
// exceptions from the driver: a broken page must still produce a readable
// report for pytest.

import { readFileSync } from "node:fs";
import { openPage, listInteractables, interact, describeElement } from "./lib.mjs";

function arg(name, fallback = null) {
  const i = process.argv.indexOf("--" + name);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const failures = [];
const warnings = [];
// Failures are tagged with the phase that produced them ("load", "sweep",
// "select-case", …). Without it, an assertion that only holds between page loads
// reads like a product bug when it was a driver pass that broke it.
let phase = "load";
const fail = (m) => failures.push(phase === "load" ? m : `${m} [${phase}]`);

const base = arg("base");
const pageName = arg("page");
const cookie = arg("cookie", "");
const allSpec = JSON.parse(readFileSync(arg("spec"), "utf8"));

// A page spec may `extends` another one. The dashboard's payload fixtures and
// tab group are what make that page deterministic, and a page that tests the
// global job bar *from* the dashboard needs exactly them; repeating them invites
// the two copies to drift apart, which is how a contract test stops testing
// anything. Scalars and lists in the child replace the parent's, unless the key
// is written "+key", which adds to the parent's list instead — the alternative is
// a child copying twenty inherited assertions to add one.
//
// Fixtures resolve by route, not by position: a child answering a route the
// parent also answers wins (route matching takes the first match, so leaving the
// parent ahead would let a child's "override" be silently ignored — the page
// would pass while testing the parent's payload). `"merge": true` patches the
// parent's body field-wise instead, which is how you change one field of a large
// payload without copying it and watching the copy drift.
function applyFixtures(base, overrides) {
  const list = [...(base || [])];
  for (const f of overrides || []) {
    const i = list.findIndex((x) => x && f && x.path === f.path);
    if (i < 0) { list.push(f); continue; }
    list[i] = f && f.merge
      ? { ...list[i], ...f, body: { ...list[i].body, ...f.body } }
      : { ...f };
  }
  return list;
}

function resolvePage(p, seen = new Set()) {
  if (!p || seen.has(p.name)) return p || {};
  seen.add(p.name);
  const parent = resolvePage(
    (allSpec.pages || []).find((x) => x.name === p.extends), seen);
  const merged = { ...parent, ...p };

  // "+key" in a child *adds* to the parent's list instead of replacing it. The
  // alternative is a child copying twenty-odd inherited assertions to add one —
  // and a copy is what stops a contract test being a contract.
  for (const k of Object.keys(merged)) {
    if (!k.startsWith("+")) continue;
    const base = k.slice(1);
    merged[base] = [...(parent[base] || []), ...(merged[k] || [])];
    delete merged[k];
  }

  merged.fixtures = applyFixtures(parent.fixtures, p.fixtures);
  return merged;
}

const spec = resolvePage((allSpec.pages || []).find((p) => p.name === pageName));

const report = {
  page: pageName,
  url: base + (spec ? spec.path : "?"),
  ok: false,
  failures,
  warnings,
  initial: {},
  stylesheets: [],
  tabs: [],
  controls: [],
  summary: {},
};

if (!spec) {
  fail(`no spec for page ${pageName}`);
  process.stdout.write(JSON.stringify(report), () => process.exit(0));
}

const url = base.replace(/\/$/, "") + spec.path;

// ---------------------------------------------------------------------------
// Tab geometry, measured rather than string-matched
//
// The complaint this guards: a tab strip whose baseline "floats" 10-20px above
// the panels it controls. jsdom performs no layout, but it does resolve the
// cascade for margin and radius — which is exactly where such a gap comes from
// — so the invariants are read off the computed style of the real elements, and
// the handful of properties jsdom cannot compute (border shorthands, var()
// colours) are read from the parsed rule instead.
//
// The visual contract, restated: the strip has no bottom margin; the panel is
// pulled up 1px onto the strip's baseline with no top border and square top
// corners; and the active tab is painted in the panel's own colour so tab and
// panel form one surface, with the baseline reading as one line broken only
// under the selected tab.
const realSheets = []; // stylesheets the page actually parsed and rendered with

function collectStylesheets() {
  // jsdom exposes a linked sheet on link.sheet even when document.styleSheets
  // omits it, and both are needed to see the whole cascade
  const collected = []
    .concat(Array.from(win.document.styleSheets))
    .concat(Array.from(win.document.querySelectorAll('link[rel="stylesheet"]')).map((l) => l.sheet));
  for (const sheet of collected) {
    if (!sheet || realSheets.includes(sheet)) continue;
    let rules = null;
    try {
      rules = sheet.cssRules; // throws for a cross-origin sheet
    } catch {
      continue;
    }
    if (!rules) continue;
    realSheets.push(sheet);
    report.stylesheets.push(`${sheet.href || "(inline)"}: ${rules.length} rules`);
  }
}

// The value of `prop` that wins for this element, walking every parsed rule
// whose selector the element actually matches, in source order, falling back to
// the listed shorthands. Needed because jsdom's getComputedStyle does not
// expand shorthands: a card that says `border-radius:10px` reports an empty
// border-top-left-radius, which is exactly how a rounded corner hid from the
// first version of this check.
function cascaded(el, prop, shorthands = []) {
  let value = null;
  for (const sheet of realSheets) {
    for (const rule of sheet.cssRules) {
      if (!rule.selectorText || !rule.style) continue;
      let hit = false;
      for (const sel of rule.selectorText.split(",").map((x) => x.trim())) {
        const clean = sel.replace(/:(?:hover|focus|active|visited|disabled|focus-visible)\b/g, "");
        if (!clean) continue;
        try {
          if (el.matches(clean)) {
            hit = true;
            break;
          }
        } catch {
          /* selector jsdom cannot evaluate against a single element */
        }
      }
      if (!hit) continue;
      for (const p of [prop, ...shorthands]) {
        const v = rule.style.getPropertyValue(p);
        if (v) {
          value = String(v).trim();
          break;
        }
      }
    }
  }
  return value;
}

// A length is "square/zero" if jsdom resolved it to 0 or the rule said so.
const isZero = (v) => v === null || v === "0" || v === "0px" || v === "none";

function checkTabGeometry() {
  for (const want of spec.expectStylesheets || []) {
    const link = Array.from(win.document.querySelectorAll('link[rel="stylesheet"]')).find(
      (l) => (l.getAttribute("href") || "").endsWith(want));
    if (!link) fail(`page does not link ${want}`);
    else if (!link.sheet || !link.sheet.cssRules || link.sheet.cssRules.length === 0)
      fail(`${want} is linked but never loaded and parsed (404? wrong content-type? syntax error?)`);
  }
  if (!realSheets.length) {
    fail("no stylesheet was parsed — tab geometry cannot be verified");
    return;
  }

  for (const g of spec.tabGroups || []) {
    const tabs = Array.from(win.document.querySelectorAll(g.tabs));
    const panels = Array.from(win.document.querySelectorAll(g.panels));
    if (!tabs.length || !panels.length) continue;
    const strip = tabs[0].parentElement;
    const label = `tab group ${g.tabs}`;

    // the rules must target this strip, or the checks below would pass against
    // a page whose tabs are styled by nothing at all
    const stripClasses = Array.from(strip.classList);
    if (stripClasses.length && !realSheets.some((sheet) =>
        Array.from(sheet.cssRules).some(
          (r) => r.selectorText && stripClasses.some((c) => r.selectorText.includes(`.${c}`)))))
      fail(`${label}: no stylesheet rule targets the tab strip (.${stripClasses.join(", .")}) — its geometry is unverifiable`);

    // 1. no floating gap between the strip and its panels
    const mb = win.getComputedStyle(strip).marginBottom || "";
    if (!mb) fail(`${label}: could not read the strip's computed margin-bottom`);
    else if (!isZero(mb))
      fail(`${label}: strip has margin-bottom ${mb} — the panels float below the tabs instead of attaching to them`);

    // the baseline the tabs stand on, spanning the full width
    const stripEdge = cascaded(strip, "border-bottom", ["border"]);
    if (!stripEdge || stripEdge === "none")
      fail(`${label}: the strip draws no bottom edge, so there is no baseline for the tabs to meet`);

    // 2. every tab carries its own bottom edge and is lowered onto the
    //    baseline; without the overlap the tab's frame stops 1px above the panel
    for (const tab of tabs) {
      const own = cascaded(tab, "border-bottom", ["border"]);
      if (!own || own === "none")
        fail(`${label}: ${JSON.stringify(tab.textContent.trim())} draws no bottom border — the frame edge cannot continue into the panel`);
      const overlap = win.getComputedStyle(tab).marginBottom || "";
      if (overlap && overlap !== "-1px")
        fail(`${label}: ${JSON.stringify(tab.textContent.trim())} has margin-bottom ${overlap}; tabs must sit 1px onto the baseline so their rails reach the panel`);
    }

    // 3. neither strip nor panel may be positioned: in normal flow the later
    //    elements paint in order, and positioning re-covers the baseline
    for (const [what, el] of [["strip", strip], ["panel", panels[0]]]) {
      const pos = cascaded(el, "position") || "";
      const z = cascaded(el, "z-index") || "";
      if (/(relative|absolute|fixed|sticky)/.test(pos) || z)
        fail(`${label}: ${what} is positioned (position:${pos} z-index:${z}) — that puts the baseline back on top of the active tab`);
    }

    for (const panel of panels) {
      const card = panel.querySelector(".card") || panel.firstElementChild;
      if (!card) continue;
      // the card must be flush: a negative margin would slide its opaque
      // background over the baseline and erase it under the inactive tabs
      const mt = win.getComputedStyle(card).marginTop || "";
      if (!isZero(mt))
        fail(`${label}: the card of #${panel.id} has margin-top ${mt}; the overlap belongs on the tab — a pulled-up card paints over the baseline`);

      // jsdom reports "" for corners set through border-radius, so read the
      // cascade by hand — and note the longhand is border-top-left-radius
      for (const corner of ["top-left", "top-right"]) {
        const radius = cascaded(card, `border-${corner}-radius`, ["border-radius"]);
        if (!isZero(radius))
          fail(`${label}: the card of #${panel.id} keeps a ${radius} ${corner} corner where it meets the square tab bottoms`);
      }
      const borderTop = cascaded(card, "border-top", ["border"]);
      if (borderTop && borderTop !== "none")
        fail(`${label}: the card of #${panel.id} has border-top:${borderTop} — the junction would draw twice`);
    }

    // the selected tab must open the baseline in the panel's own colour
    for (const tab of tabs) {
      const isActive = tab.classList.contains("active");
      const bg = cascaded(tab, "background", ["background-color"]) || "";
      const bb = cascaded(tab, "border-bottom", ["border"]) || "";
      const who = `${JSON.stringify(tab.textContent.trim())}${isActive ? " (active)" : ""}`;
      if (isActive) {
        if (!bg.includes("--panel"))
          fail(`${label}: active tab ${who} background is ${bg || "(none)"}; it must be var(--panel) — the colour of the panel it controls`);
        if (!bb.includes("--panel"))
          fail(`${label}: active tab ${who} border-bottom is ${bb || "(none)"}; it must open the baseline in var(--panel)`);
      } else if (bg.includes("--panel")) {
        fail(`${label}: inactive tab ${who} is painted in the panel colour — it claims a panel it does not control`);
      }
    }
  }
}

let ctx;
try {
  ctx = await openPage({
    url,
    cookieHeader: cookie,
    blockMutations: spec.blockMutations || allSpec.blockMutations || [".*"],
    allowHttpErrors: (allSpec.allowHttpErrors || []).concat(spec.allowHttpErrors || []),
    // page spec wins over the global fixtures, by route (see applyFixtures)
    fixtures: applyFixtures(allSpec.fixtures, spec.fixtures),
    maxWaitMs: spec.loadTimeoutMs || 20000,
  });
} catch (e) {
  fail(`page failed to load: ${e && e.message}`);
  process.stdout.write(JSON.stringify(report), () => process.exit(0));
}

const { win, trace } = ctx;

// ---------------------------------------------------------------- initial load
await ctx.idle({ quietMs: 200, maxMs: spec.idleTimeoutMs || 15000 });

report.initial = {
  title: win.document.title,
  errors: [...trace.errors],
  consoleErrors: [...trace.consoleErrors],
  httpErrors: [...trace.httpErrors],
  missedIds: [...new Set(trace.missedIds)],
  navigations: trace.navigations.length,
  unsupported: [...new Set(trace.unsupported)].slice(0, 5),
  requests: trace.requests.map(
    (r) => `${r.method} ${r.path}${r.query ? "?" + r.query : ""} -> ${r.status}${r.blocked ? " (blocked)" : ""}${r.fixture ? " (fixture)" : ""}`
  ),
};

for (const m of trace.errors) fail(`uncaught error while loading: ${m}`);
for (const m of trace.consoleErrors) fail(`console.error while loading: ${m}`);
for (const m of trace.httpErrors) fail(`endpoint error while loading: ${m}`);
if (report.initial.missedIds.length)
  fail(`page scripts looked up element ids that do not exist: ${report.initial.missedIds.join(", ")}`);

if (spec.expectTitle && !win.document.title.includes(spec.expectTitle))
  fail(`title is ${JSON.stringify(win.document.title)}, expected ${JSON.stringify(spec.expectTitle)}`);

// Every data fetch the page is supposed to make must really happen: a page
// that renders but never calls its endpoint just shows placeholders forever.
for (const want of spec.expectRequests || []) {
  const hit = trace.requests.some((r) => r.path === want || r.path.startsWith(want.replace(/\/$/, "") + "/"));
  if (!hit) fail(`expected request never made: ${want}`);
}
for (const want of spec.expectNoRequests || []) {
  const hit = trace.requests.find((r) => r.path === want);
  if (hit) fail(`page made a request it should not: ${want}`);
}

for (const sel of spec.expectVisible || [])
  if (!win.document.querySelector(sel)) fail(`expected element missing: ${sel}`);
for (const sel of spec.expectAbsent || [])
  if (win.document.querySelector(sel)) fail(`element should not exist: ${sel}`);

// A wide table on a narrow screen is only safe if something scrolls it. Without
// this check a page can grow a fifth, sixth, seventh column and every existing
// assertion still passes, because they all ask about elements and none asks about
// width — and the failure only appears on a phone. The models library table is the
// case that motivated it: arch, quant, size, trained ctx, layers, MoE and pairing
// in one row. jsdom performs no layout, so the check is structural — the named
// tables must sit directly inside an element the stylesheet makes scrollable, by
// inline overflow or by the project's `scroll` class convention.
for (const sel of spec.expectScrollContainers || []) {
  const els = [...win.document.querySelectorAll(sel)];
  if (!els.length) fail(`expected at least one scrollable table: ${sel}`);
  for (const el of els) {
    const wrap = el.parentElement;
    if (!wrap) { fail(`table has no container to scroll it: ${sel}`); continue; }
    const css = wrap.getAttribute("style") || "";
    const cls = wrap.className || "";
    const scrolls = /overflow(-x)?\s*:\s*(auto|scroll)/.test(css) || /\bscroll\b/.test(cls);
    if (!scrolls) {
        fail(`table is not inside a scroll container (would widen the page on a `
           + `narrow viewport): ${sel} → <${wrap.tagName.toLowerCase()} `
             + `class="${cls}">`);
    }
  }
}


// "Present but not showing" is a state of its own, and the one that matters for
// a panel that only appears when there is something to act on: a row that never
// hides is noise on every load, and a row that hides by never rendering takes
// its own explanation with it. expectVisible cannot tell the difference because
// it only asks whether the element is in the document.
for (const sel of spec.expectHidden || []) {
  const el = win.document.querySelector(sel);
  if (!el) fail(`expected element present and hidden, got nothing: ${sel}`);
  else if (!el.hidden && el.getAttribute("hidden") === null)
    fail(`element should be hidden but is showing: ${sel}`);
}

// The mirror, and the reason expectVisible is not enough for a panel that shows
// and hides: "in the document" is true whether or not anyone can see it. Without
// this half, a row that is permanently hidden passes every expectVisible on the
// page that exists to demonstrate it showing.
for (const sel of spec.expectShown || []) {
  const el = win.document.querySelector(sel);
  if (!el) fail(`expected element showing, got nothing: ${sel}`);
  else if (el.hidden || el.getAttribute("hidden") !== null)
    fail(`element should be showing but is hidden: ${sel}`);
}


// A live region must show *data*, not merely exist: a panel that renders "—"
// forever passes a visibility check and tells the user nothing. `contains` is
// matched against any element of the selector; `minCount` pins how many there
// are (a slot board with no rows is the classic failure).
// `after_select: true` moves an entry to the pass that runs once the select cases have
// driven the page (see checkText/checkValues below). A page that fills its fields from the
// record a control names has to be measured *after* that control was driven -- asserting the
// personality that is selected on load would only ever measure the first card.
function checkText(entries, when) {
  for (const t of entries || []) {
  if (!t.selector) {                        // annotation-only entry
    if (!t._comment) fail(`expectText entry has no selector: ${JSON.stringify(t)}`);
    continue;
  }
  const els = Array.from(win.document.querySelectorAll(t.selector));
  const seen = () => els.slice(0, 3)
    .map((el) => JSON.stringify((el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 90)));
  if (!els.length) { fail(`expected text region missing: ${t.selector}`); continue; }
  if (t.minCount != null && els.length < t.minCount)
    fail(`${t.selector}: ${els.length} element(s), expected at least ${t.minCount} (${when})`);
  if (t.contains != null && !els.some((el) => (el.textContent || "").includes(t.contains)))
    fail(`${t.selector}: nothing contains ${JSON.stringify(t.contains)} (${when}, saw ${seen().join(", ")})`);
  if (t.matches != null && !els.some((el) => new RegExp(t.matches).test(el.textContent || "")))
    fail(`${t.selector}: nothing matches /${t.matches}/ (${when}, saw ${seen().join(", ")})`);
  }
}

// A field's *value* is not text content, so expectText cannot reach it. The pages that seed a
// form from the record a control names (personalities, §6.14.1i) are defined by those values:
// an empty field with a correct label looks exactly like a correct field.
function checkValues(entries, when) {
  for (const t of entries || []) {
    const el = win.document.querySelector(t.selector);
    if (!el) { fail(`expected value control missing: ${t.selector} (${when})`); continue; }
    if (!("value" in el)) { fail(`${t.selector} has no value to assert (${when})`); continue; }
    const got = String(el.value ?? "");
    if (t.value != null && got !== String(t.value)) {
      fail(`${t.selector}: value is ${JSON.stringify(got)}, expected ${JSON.stringify(String(t.value))} (${when})`);
    }
    if (t.contains != null && !got.includes(t.contains)) {
      fail(`${t.selector}: value ${JSON.stringify(got)} does not contain ${JSON.stringify(t.contains)} (${when})`);
    }
  }
}

checkText((spec.expectText || []).filter((t) => !t.after_select), "load");
checkValues((spec.expectValues || []).filter((t) => !t.after_select), "load");

// A control the user is meant to use must not be stuck disabled.
report.disabledControls = [];
for (const el of listInteractables(ctx)) {
  if (el.disabled) report.disabledControls.push(describeElement(el, win).selector);
}
for (const sel of spec.expectEnabled || []) {
  const el = win.document.querySelector(sel);
  if (!el) fail(`expected enabled control missing: ${sel}`);
  else if (el.disabled) fail(`control should be usable but is disabled: ${sel}`);
}

// An attribute assertion on <body> that must hold at load **and** after the
// whole sweep. `data-jobbusy` is the machine-wide job lock; a bar that said
// "busy" on arrival and quietly forgot it while controls were being driven is
// invisible to a point-in-time check. The console line names the last
// /api/jobs/active poll so "when did it stop" is answerable from the report.
const expectBodyAttr = spec.expectBodyAttr || [];
const checkBodyAttr = (when) => {
  const bad = Object.entries(expectBodyAttr).filter(
    ([k, want]) => (win.document.body.getAttribute(k) || "") !== want);
  if (bad.length) {
    const last = trace.requests.filter((r) => r.path === "/api/jobs/active").slice(-1)[0];
    console.error("[ui-body] " + JSON.stringify({
      when, requests: trace.requests.length,
      lastJobsPollS: last && last.t ? Math.round((Date.now() - last.t) / 1000) : null,
      attrs: bad.map(([k, want]) => [k, want, win.document.body.getAttribute(k) || ""]),
    }));
  }
  for (const [k, want] of bad) {
    const got = win.document.body.getAttribute(k) || "";
    fail(`body[${k}] is ${JSON.stringify(got)}${when}, expected ${JSON.stringify(want)}`);
  }
};
checkBodyAttr("");

phase = "tabs";
// ------------------------------------------------------------------------ tabs
// A tab group must be a bijection: one panel per tab, every panel reachable,
// exactly one active and visible at a time, clicked tab marked active.
for (const g of spec.tabGroups || []) {
  const tabs = Array.from(win.document.querySelectorAll(g.tabs));
  const panels = Array.from(win.document.querySelectorAll(g.panels));
  const entry = { group: g.tabs, tabs: tabs.length, panels: panels.length, results: [] };
  report.tabs.push(entry);
  if (g.expectTabs != null && tabs.length !== g.expectTabs)
    fail(`tab group ${g.tabs}: ${tabs.length} tabs, expected ${g.expectTabs}`);
  if (g.expectPanels != null && panels.length !== g.expectPanels)
    fail(`tab group ${g.tabs}: ${panels.length} panels, expected ${g.expectTabs}`);
  if (tabs.length !== panels.length)
    fail(`tab group ${g.tabs}: ${tabs.length} tabs but ${panels.length} panels (one was added or renamed)`);

  const activated = new Map();
  for (let i = 0; i < tabs.length; i++) {
    const tab = tabs[i];
    const label = (tab.textContent || "").trim().slice(0, 30) || `#${i}`;
    const errBefore = trace.errors.length;
    tab.dispatchEvent(new win.MouseEvent("click", { bubbles: true, cancelable: true, view: win }));
    await ctx.idle({ quietMs: 40, maxMs: 4000 });
    const nowActive = panels.filter((p) => p.classList.contains("active"));
    const visible = panels.filter((p) => ctx.isVisible(p));
    entry.results.push({ tab: label, active: nowActive.map((p) => "#" + p.id), visible: visible.map((p) => "#" + p.id) });
    if (trace.errors.length > errBefore) fail(`clicking tab ${JSON.stringify(label)} threw: ${trace.errors[errBefore]}`);
    if (!tab.classList.contains("active")) fail(`tab ${JSON.stringify(label)} did not get .active`);
    if (nowActive.length !== 1) fail(`after clicking ${JSON.stringify(label)}: ${nowActive.length} panels active, expected 1`);
    if (visible.length !== 1) fail(`after clicking ${JSON.stringify(label)}: ${visible.length} panels visible, expected 1`);
    if (nowActive.length === 1 && visible.length === 1 && nowActive[0] !== visible[0])
      fail(`after clicking ${JSON.stringify(label)}: the .active panel is not the visible one`);
    if (nowActive.length === 1) {
      const key = "#" + nowActive[0].id;
      activated.set(key, (activated.get(key) || 0) + 1);
    }
  }
  for (const p of panels)
    if (!activated.has("#" + p.id)) fail(`panel #${p.id} is reachable from no tab in ${g.tabs}`);
  for (const [panel, n] of activated) if (n > 1) fail(`panel ${panel} is activated by ${n} tabs`);
  for (const m of trace.httpErrors) fail(`endpoint error while exercising tabs: ${m}`);
}

collectStylesheets();

// A pinned column is a CSS claim, and jsdom performs no layout, so this cannot prove
// the column sticks on screen. What it *can* prove is the two things that make pinning
// possible at all, and which a later edit can quietly break: the stylesheet declares a
// sticky position for the table's first two cell positions, and the table is inside a
// container that actually scrolls (a sticky cell in a non-scrolling box does nothing).
// Asserting the rule exists is not asserting the layout works — it is asserting that the
// layout has not been deleted while the markup still looks like it should work.
for (const sel of spec.expectStickyColumns || []) {
  const els = [...win.document.querySelectorAll(sel)];
  if (!els.length) fail(`expected a table with pinned columns: ${sel}`);
  // Read the parsed rules, the same way the tab-geometry checks above do. The page's
  // table CSS lives in an inline <style>, which jsdom exposes as a sheet in
  // document.styleSheets; reading `style` textContent instead would work only if the
  // element were still in the DOM and would silently miss a linked stylesheet.
  const sheets = realSheets.map((sheet) => {
    try {
      return Array.from(sheet.cssRules || []).map((r) => r.cssText).join("\n");
    } catch {
      return "";
    }
  }).join("\n");
  if (!sheets) { fail("no parsed CSS to verify pinned columns against"); continue; }
  for (const el of els) {
    const wrap = el.parentElement;
    const wrapCls = (wrap && wrap.className) || "";
    // The CSS scopes these rules by the CONTAINER class (`.grid-wrap th:first-child`),
    // not by the table's own id. Deriving the scope from the selector (`#library-table`
    // -> `library-table`) looked plausible and matched nothing, so every assertion failed
    // on correct CSS. Take the scope from the container's actual class list.
    const classes = wrapCls.split(/\s+/).filter(Boolean);
    if (!classes.length) { fail(`pinned-column table has no classed container: ${sel}`); continue; }
    const hasStickyFor = (pseudo) => classes.some((cls) => {
      // `[^{}]*` between the class and the pseudo-class: the real selectors are
      // `.grid-wrap th:first-child` and `.grid-wrap td:nth-child(2)`, so a tag name
      // sits between them. Joining the class straight to the pseudo-class matched
      // nothing and failed on correct CSS.
      const mid = pseudo
        ? `[^{}]*${pseudo.replace(/[(){}]/g, (m) => "\\" + m)}`
        : "";
      const re = new RegExp(`\\.${cls}${mid}[^{}]*\\{[\s\S]*?position:\\s*sticky`, "i");
      return re.test(sheets);
    });
    // Selector shapes in this stylesheet are `.grid-wrap th{...}`, `.grid-wrap
    // th:first-child,...{...}` and `.grid-wrap td:nth-child(2){...}`. An earlier version
    // of these patterns put `[^{]*` between the class and `:first-child`, which cannot
    // match `.grid-wrap th:first-child` because `th` is in the way — the assertion then
    // failed on correct CSS. `[^{}]*` stays inside one selector list, and [\s\S] crosses
    // the newlines in a multi-line rule body.
    if (!hasStickyFor("")) fail(`table has no sticky header rule: ${sel}`);
    if (!hasStickyFor(":first-child")) fail(`table's first column is not pinned: ${sel}`);
    if (!hasStickyFor(":nth-child(2)")) fail(`table's second column is not pinned: ${sel}`);
    if (!/\bscroll\b/.test(wrapCls)) {
      fail(`pinned columns need a scrolling container to be meaningful: ${sel}`);
    }
  }
}
checkTabGeometry();

// Stop the pollers before the sweep.
ctx.quiesce();
await ctx.idle({ quietMs: 80, maxMs: 2000 });

phase = "sweep";
// ------------------------------------------------------------- control sweep
const requireConfirm = spec.requireConfirmFor || [];
const requireConfirmUnless = spec.requireConfirmUnlessBodyContains || {};
const expectInert = (allSpec.expectInert || []).concat(spec.expectInert || []);

const covered = new Set();   // identities driven by any means, sweep or case
// An endpoint error is only a failure if somebody can attribute it to a click.
// A response that lands between two interactions used to slip through entirely:
// recorded in the trace, never inside any single interaction's window, so the
// page passed with a 400 in it. Anything not consumed by judgeControl fails here.
const httpAttributed = new Set(trace.httpErrors);   // load-time errors already failed
function attributeHttp(messages) {
  for (const m of messages) httpAttributed.add(m);
}
// A regression test for this file's own reason for existing: judgeControl used
// to lose these four assertions in a refactor and nothing failed, because the
// checks it kept (dead control, confirm gate) were loud enough to hide the ones
// it dropped. Mutation 8 in mutation_check.py is what catches that class of edit;
// this is the cheap version, and it costs nothing.
if (typeof judgeControl.toString === "function") {
  for (const needle of ["newErrors", "newConsoleErrors", "newMissedIds", "newHttpErrors",
                        "confirmsBefore", "dead control"]) {
    if (!judgeControl.toString().includes(needle)) {
      fail(`harness integrity: judgeControl no longer checks ${needle} — assertions were dropped in a refactor`);
    }
  }
}

phase = "select-case";
// ------------------------------------------------- selection-driven enablement
// A control that locks or unlocks another one has to be asserted, not assumed:
// "delete is disabled for the running build" is the whole safety story of that
// button, and a page can look perfectly fine with it permanently clickable.
for (const sel of spec.expectDisabled || []) {
  const el = win.document.querySelector(sel);
  if (!el) fail(`expected disabled control missing: ${sel}`);
  else if (!el.disabled) fail(`control should be locked on load but is enabled: ${sel}`);
}

// choose: {index} | {value} | {labelContains} | {labelNotContaining}
function pickOption(select, choose) {
  const opts = Array.from(select.querySelectorAll("option")).filter((o) => o.value !== "");
  if (!opts.length) return null;
  let hit = null;
  if (choose.index != null) hit = opts[choose.index];
  else if (choose.value != null) hit = opts.find((o) => o.value === choose.value) || null;
  else if (choose.labelContains != null)
    hit = opts.find((o) => (o.textContent || "").includes(choose.labelContains)) || null;
  else if (choose.labelNotContaining != null)
    hit = opts.find((o) => !(o.textContent || "").includes(choose.labelNotContaining)) || null;
  if (!hit) return null;
  select.value = hit.value;
  select.dispatchEvent(new win.Event("change", { bubbles: true }));
  return hit;
}

for (const c of spec.selectCases || []) {
  const select = win.document.querySelector(c.select);
  const what = `select case ${c.select} (${c.note || ""})`;
  if (!select) {
    fail(`${what}: select missing`);
    continue;
  }
  const errBefore = trace.errors.length;
  const picked = pickOption(select, c.choose || {});
  if (!picked) {
    if (c.optional) continue;
    fail(`${what}: no option matched ${JSON.stringify(c.choose)} — the page offers: ` +
         Array.from(select.querySelectorAll("option")).map((o) => o.textContent).join(" | "));
    continue;
  }
  await ctx.idle({ quietMs: 60, maxMs: 3000 });
  if (trace.errors.length > errBefore)
    fail(`${what}: changing the selection threw ${trace.errors[errBefore]}`);

  // A case may type into a text field before the page's enablement is judged. Client-side gating —
  // "Create unlocks when a usable name is typed", "Delete is locked for a built-in" — is a property
  // of the page, and a case that cannot type can only ever assert that a control is *locked*: the
  // enabled half of the rule would go unmeasured.
  for (const f of c.then_fill || []) {
    const el = win.document.getElementById(f.id) || win.document.querySelector(f.id);
    if (!el) { fail(`${what}: fill target ${f.id} missing`); continue; }
    if (el.disabled) {
      fail(`${what}: fill target ${f.id} is disabled with ${JSON.stringify(picked.textContent)} selected`);
      continue;
    }
    el.value = f.value;
    el.dispatchEvent(new win.Event("input", { bubbles: true }));
    el.dispatchEvent(new win.Event("change", { bubbles: true }));
    await ctx.idle({ quietMs: 25, maxMs: 2000 });
  }
  for (const sel of c.expectEnabled || []) {
    const el = win.document.querySelector(sel);
    if (!el) fail(`${what}: expected enabled control missing: ${sel}`);
    else if (el.disabled) fail(`${what}: ${sel} should be usable with ${JSON.stringify(picked.textContent)}, but it is disabled`);
  }
  for (const sel of c.expectDisabled || []) {
    const el = win.document.querySelector(sel);
    if (!el) fail(`${what}: expected disabled control missing: ${sel}`);
    else if (!el.disabled) fail(`${what}: ${sel} must stay locked with ${JSON.stringify(picked.textContent)} selected`);
  }

  // A case may also drive the control its selection just unlocked. Destructive
  // endpoints behind a selection-dependent button have to be exercised against
  // a known selection — the sweep reaches the button only in whatever state the
  // select happened to be left in.
  for (const target of c.then_click || []) {
    const btn = win.document.querySelector(target);
    if (!btn) {
      fail(`${what}: click target ${target} missing`);
      continue;
    }
    if (btn.disabled) {
      fail(`${what}: ${target} is disabled with ${JSON.stringify(picked.textContent)} selected — it cannot be exercised`);
      continue;
    }
    const rr = await interact(ctx, btn, { answerDialogs: true, noopCheck });
    const rinfo = describeElement(btn, win);
    report.controls.push({
      phase: "select-case", tab: c.note || "case", ...rinfo, effect: rr.effect,
      requests: rr.newRequests.map((q) => `${q.method} ${q.path}${q.blocked ? " (blocked)" : ""}`),
      dialogs: rr.newDialogs.map((d) => d.kind),
      navigations: rr.newNavigations.length,
    });
    covered.add(btn.__uiId || baseIdentity(btn));
    judgeControl(rinfo, rr, `${what}, clicked ${target}`, {});
  }
}

// The post-selection pass: what a page shows *because* a control was driven. A page that seeds
// its fields from the record the selection names can only be measured with that record selected,
// and the sweep runs after this point -- so a field the page never filled is caught here rather
// than looked over by the sweep on its way to judging the button next to it.
checkText((spec.expectText || []).filter((t) => t.after_select), "after select cases");
checkValues((spec.expectValues || []).filter((t) => t.after_select), "after select cases");


// Clicking the tab that is already showing changes nothing — in a browser
// too. This is safe to tolerate because the tab-group check above proves the
// clicked tab and the one visible panel correspond; a broken mapping fails
// there, not here.
function noopCheck(el) {
  for (const g of spec.tabGroups || []) {
    if (!el.matches || !el.matches(g.tabs)) continue;
    const active = Array.from(win.document.querySelectorAll(g.panels)).filter((p) =>
      p.classList.contains("active"));
    if (el.classList.contains("active") && active.length === 1)
      return `already showing #${active[0].id}`;
  }
  return null;
}

// Identity used for "already driven": what a control *is* (tag, id, name,
// text, handler, href, input type) plus an occurrence index among identical
// ones. Absolute position would break every time a save re-renders a table —
// which is exactly what the settings page does — while ignoring position
// entirely would let a duplicate control hide behind its twin.
function baseIdentity(el) {
  return [
    el.tagName.toLowerCase(),
    el.id || "",
    el.getAttribute("name") || "",
    (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 30),
    (el.getAttribute("onclick") || el.getAttribute("onchange") || el.getAttribute("onsubmit") || "").slice(0, 60),
    el.tagName === "A" ? el.getAttribute("href") || "" : "",
    el.tagName === "INPUT" ? el.getAttribute("type") || "" : "",
  ].join("|");
}

// annotate a freshly listed set with per-element identity
function withIdentities(list) {
  const counts = new Map();
  for (const el of list) counts.set(baseIdentity(el), (counts.get(baseIdentity(el)) || 0) + 1);
  const seen = new Map();
  for (const el of list) {
    const base = baseIdentity(el);
    const n = (seen.get(base) || 0) + 1;
    seen.set(base, n);
    el.__uiId = counts.get(base) > 1 ? `${base}#${n}` : base;
  }
  return list;
}

// One set of rules decides whether an interaction was acceptable. Both the
// sweep and the selection cases use it, so a click is judged identically no
// matter what drove it: no errors, no endpoint failures, destructive requests
// confirm-gated before they happen, and some visible effect.
function judgeControl(info, r, phase, { toleratedNoop = false } = {}) {
    for (const m of r.newErrors) fail(`${info.selector} threw: ${m}`);
    for (const m of r.newConsoleErrors) fail(`${info.selector} logged console.error: ${m}`);
    for (const m of r.newMissedIds) fail(`${info.selector} looked up missing id #${m}`);
    for (const m of r.newHttpErrors) fail(`${info.selector} hit an endpoint error: ${m}`);
    // claimed here so the end-of-page sweep can tell "judged" from "nobody saw
    // it"; without this line an error would be both failed and orphaned
    attributeHttp(r.newHttpErrors);

    // Destructive requests must be confirm-gated. Checked in both passes: in
    // the denied pass an ungated request is the bug itself (the app acted
    // without asking), and in the accepted pass it catches a handler that asks
    // but then sends something the endpoint needs anyway without it.
    const MUTATING = ["POST", "PUT", "PATCH", "DELETE"];
    for (const req of r.newRequests.filter((x) => MUTATING.includes(x.method))) {
      if (!requireConfirm.some((p) => new RegExp(p).test(req.path))) continue;
      // same endpoint, different verb: creating a router key is additive and
      // does not need a confirmation, rotating or revoking one does
      const exempt = Object.entries(requireConfirmUnless).find(
        ([p, needle]) => new RegExp(p).test(req.path) && (req.body || "").includes(needle));
      if (exempt) continue;
      // confirm() must have happened before the request — an alert() afterwards
      // ("rolled back") is not a confirmation
      if (req.confirmsBefore - r.confirmsAtMark <= 0)
        fail(`${info.selector} sent ${req.method} ${req.path} without asking for confirmation first (${phase} pass)`);
    }

    // expectInert is matched against the selector, which for a row the harness injected
    // via a fixture is positional (div[nth=16]) and changes whenever the fixture order
    // changes. A fixture row is data invented to test something else (escaping), not a
    // control the page offers, so its handler is the stable identity to exempt on.
    const inertHaystack = `${info.selector} ${info.handler || ""} ${info.text || ""}`;
    if (r.effect === "none" && !toleratedNoop
        && !expectInert.some((p) => new RegExp(p).test(inertHaystack))) {
      const what = [info.text && `text=${JSON.stringify(info.text)}`, info.handler && `handler=${JSON.stringify(info.handler)}`]
        .filter(Boolean)
        .join(" ");
      fail(`dead control: ${info.selector}${what ? " " + what : ""} — no request, no dialog, no navigation, no DOM change`);
    }

}

async function sweep(phase, answerDialogs, visited, context, budget) {
  // Controls are re-listed at every step: saving a row re-renders the table,
  // and clicking a detached node would look like a dead control. Identity is
  // positional (describeElement().selector), so a re-rendered copy is not
  // visited twice.
  for (let step = 0, guard = 0; guard < budget; guard++) {
    const list = withIdentities(listInteractables(ctx));
    if (step >= list.length) break;
    const el = list[step];
    const info = describeElement(el, win);
    const key = el.__uiId;
    if (process.env.UI_DEBUG === "2")
      console.error(`[sweep ${phase}/${context}] step=${step} n=${list.length} el=${String(key).slice(0, 60)}`);
    if (visited.has(key)) {
      step++;
      continue;
    }
    // A control that is disabled right now is *not* marked visited: a page can
    // enable it later (the API-key panel does exactly that when its state
    // arrives), and a sweep that wrote it off then reported it at coverage time
    // was a flake waiting for a response to be one tick slow.
    //
    // Walk past it rather than waiting on its spot. Staying put meant one
    // locked control (a job bar locking the dashboard for the length of a
    // build) stopped the pass from ever reaching the rest of the page, and the
    // coverage gate then reported unrelated controls as never driven. The gate
    // at the end re-lists everything and drives whatever is usable by then.
    if (el.disabled) { step++; continue; }
    visited.add(key);
    if (info.type === "file") continue;
    if (!el.isConnected) { step++; continue; } // re-rendered away before we got to it

    // Tabs are not clicked by the sweep: clicking one changes which controls
    // are visible, so a sweep that clicks tabs ends up skipping half the page.
    // The tab-group check above already exercises them fully (active class,
    // panel mapping, exactly one visible, no errors); the rotation below
    // decides which panel each pass covers.
    if (isTabElement(el)) {
      report.controls.push({ phase, tab: context, ...info, effect: "tab", drivenBy: "tab-group check" });
      step++;
      continue;
    }

    // Links: external ones are not the portal's to open (jsdom has no
    // window.open), and a link to the page you are already on is a no-op in a
    // browser too. Both are judged by their href instead of by clicking.
    let samePage = false;
    if (info.tag === "a") {
      const href = el.getAttribute("href") || "";
      const broken = !href || href.includes("{{") || /\bNone\b|undefined/.test(href);
      const external = el.getAttribute("target") === "_blank" || /^https?:\/\//.test(href);
      if (broken) fail(`link ${info.selector} has a broken href: ${JSON.stringify(href)}`);
      if (broken || external) {
        report.controls.push({ phase, tab: context, ...info, effect: broken ? "broken-link" : "external-link", href });
        continue;
      }
      samePage = new URL(href, url).pathname === new URL(url).pathname && !href.includes("#");
    }

    const r = await interact(ctx, el, { answerDialogs, noopCheck });
    report.controls.push({
      phase,
      tab: context,
      ...info,
      effect: r.effect,
      samePage,
      expectedNoop: r.expectedNoop,
      defaultPrevented: r.defaultPrevented,
      domChanged: r.domChanged,
      requests: r.newRequests.map((q) => `${q.method} ${q.path}${q.blocked ? " (blocked)" : ""}`),
      dialogs: r.newDialogs.map((d) => d.kind),
      navigations: r.newNavigations.length,
    });

    judgeControl(info, r, phase, { toleratedNoop: samePage || r.expectedNoop });

    // advance unless the list grew from under us (a control that rendered new
    // controls, e.g. search results), in which case rescan from here
    if (step + 150 < list.length) step++;
  }
}

// Sweep every tab, not just the one that happens to be active: a control
// inside an inactive panel is invisible to the driver, exactly as it is to the
// user, so "we clicked everything" without tab rotation silently skips most of
// the dashboard. Confirmations are refused on the first pass and accepted on
// the second, so destructive paths execute too — into the harness block list,
// never into the real system.
const tabContexts = [[]];
for (const g of spec.tabGroups || []) {
  tabContexts[0] = Array.from(win.document.querySelectorAll(g.tabs));
}
const tabSet = new Set(tabContexts[0]);
const isTabElement = (el) => tabSet.has(el);
const budget = 4 * (listInteractables(ctx).length + 40);
const t0 = Date.now();
const lap = (label) => {
  report.timings = report.timings || [];
  report.timings.push({ step: label, ms: Date.now() - t0 });
  if (process.env.UI_DEBUG) console.error(`[ui] ${label} @ ${Date.now() - t0}ms`);
};
// One sweep per (phase, tab). The visited set is per pass on purpose: a click
// can move the page to another tab (a search result jumps to the download
// form), which hides controls the pass has not reached yet — re-running under
// each tab guarantees every control is driven while it is actually visible.
for (const [phase, answerDialogs] of [["dialogs-denied", false], ["dialogs-accepted", true]]) {
  for (const tab of tabContexts[0].length ? tabContexts[0] : [null]) {
    if (Date.now() - t0 > (spec.maxSweepMs || 150000)) {
      report.warnings.push(`sweep stopped early after ${Date.now() - t0}ms`);
      break;
    }
    if (tab) {
      tab.dispatchEvent(new win.MouseEvent("click", { bubbles: true, cancelable: true, view: win }));
      await ctx.idle({ quietMs: 60, maxMs: 4000 });
    }
    const label = tab ? (tab.textContent || "").trim().slice(0, 24) : "default";
    const visited = new Set();
    await sweep(phase, answerDialogs, visited, label, budget);
    for (const k of visited) covered.add(k);
    lap(`${phase}/${label}`);
  }
}
report.coveredControls = covered.size;

phase = "coverage";
// ------------------------------------------------------------- coverage gate
// "Every interactable element" has to be a checked claim, not a promise: walk
// each tab's visible controls once more and require that each one was driven in
// some pass. Catches a control that only exists after some other interaction,
// a panel the rotation never reached, or a sweep that ran out of budget.
const missedCoverage = [];
for (const tab of tabContexts[0].length ? tabContexts[0] : [null]) {
  if (tab) {
    tab.dispatchEvent(new win.MouseEvent("click", { bubbles: true, cancelable: true, view: win }));
    await ctx.idle({ quietMs: 40, maxMs: 3000 });
  }
  for (const el of withIdentities(listInteractables(ctx))) {
    if (el.disabled) continue;
    if (covered.has(el.__uiId)) continue;
    // Driven late rather than reported: this control became usable after the
    // sweep passed its place in the DOM. Drive and judge it now — same rules —
    // so the gate means "unreachable", not "arrived at too early".
    const r = await interact(ctx, el, { answerDialogs: true, noopCheck });
    const info = describeElement(el, win);
    covered.add(el.__uiId);
    report.controls.push({ phase: "coverage", tab: (tab ? tab.textContent : "default") || "default",
      ...info, effect: r.effect,
      requests: r.newRequests.map((q) => `${q.method} ${q.path}${q.blocked ? " (blocked)" : ""}`),
      dialogs: r.newDialogs.map((d) => d.kind), navigations: r.newNavigations.length });
    judgeControl(info, r, "coverage", { toleratedNoop: r.expectedNoop });
  }
  for (const el of withIdentities(listInteractables(ctx))) {
    if (!el.disabled && !covered.has(el.__uiId))
      missedCoverage.push(`${tab ? (tab.textContent || "").trim().slice(0, 20) : "default"}: ${baseIdentity(el)}`);
  }
}
const uniqueMisses = [...new Set(missedCoverage)];
if (uniqueMisses.length)
  fail(`controls never driven: ${uniqueMisses.slice(0, 12).join("  |  ")}` +
       (uniqueMisses.length > 12 ? ` (+${uniqueMisses.length - 12} more)` : ""));

phase = "job-bar";
checkBodyAttr(" after every control was driven");

// Escaping is a property of the rendered DOM, not of the source you can eyeball, so a
// page that fetches JSON and builds markup with template literals needs this assertion.
// It runs here rather than at load time on purpose: on /models the hostile fixture is a
// /api/models/search response, and that request only happens when the search form is
// submitted. Asserting at load time measured an empty container and reported "no
// injection" for a page that had never rendered a search result at all.
phase = "markup";
for (const probe of spec.expectNoInjectedMarkup || []) {
  // Drive whatever the page needs to make the hostile fixture land. A probe that only
  // inspects is a probe on a page that has not done the thing being tested.
  if (probe.triggerForm) {
    const form = win.document.querySelector(probe.triggerForm);
    if (!form) { fail(`xss probe triggerForm matched nothing: ${probe.triggerForm}`); continue }
    // Fill the inputs first. The page's own guard is `if (!query) return`, so a
    // submitted-but-empty form silently does nothing and the fixture never lands.
    for (const fill of probe.triggerFill || []) {
      const el = win.document.querySelector(fill.select);
      if (!el) { fail(`xss probe triggerFill matched nothing: ${fill.select}`); continue }
      el.value = fill.value;
      try { el.dispatchEvent(new win.Event("input", { bubbles: true })); } catch {}
    }
    try { form.dispatchEvent(new win.Event("submit", { bubbles: true, cancelable: true })); }
    catch { /* jsdom version differences; the assertion below still reports the gap */ }
  }
}
await new Promise((r) => setTimeout(r, 400));   // let the triggered fetch settle
for (const probe of spec.expectNoInjectedMarkup || []) {
  const container = probe.selectors
    ? win.document.querySelector(probe.selectors)
    : win.document.body;
  if (!container) { fail(`xss probe found no container: ${probe.selectors}`); continue }
  const html = container.innerHTML || "";

  // Ask the DOM, not the string. A substring test on innerHTML cannot tell escaped text
  // from executed markup: esc() turns `<img onerror=...>` into
  // `&lt;img onerror=...&gt;`, and the characters `onerror=alert(1)` are still in there
  // as harmless displayed text. Asserting on the substring reports a vulnerability in a
  // page that is correctly escaping, which is worse than no assertion because it teaches
  // everyone to ignore the harness. The security property is what the parser built.
  for (const sel of probe.mustNotCreate || []) {
    const bad = container.querySelector(sel);
    if (bad) {
      fail(`rendered DOM contains an element ${JSON.stringify(sel)} built from a remote `
           + `response value in ${probe.selectors} — markup was interpolated without `
           + `escaping. innerHTML was: ${html.slice(0, 200)}`);
    }
  }
  // Attribute-level check too: escaping a value that lands inside an attribute is a
  // different failure from escaping one that lands in text, and the template has both.
  for (const rule of probe.mustNotHaveAttr || []) {
    for (const el of container.querySelectorAll(rule.select)) {
      const v = el.getAttribute(rule.attr) || "";
      if (rule.forbidden.some((f) => v.includes(f))) {
        fail(`attribute ${rule.attr} on ${rule.select} in ${probe.selectors} contains `
             + `${JSON.stringify(rule.forbidden.join("|"))} from a remote value: `
             + `${JSON.stringify(v.slice(0, 120))}`);
      }
    }
  }
  // Positive half, and the half that actually caught a broken harness here: if the
  // hostile fixture never reached the DOM, every mustNotContain above passes for the
  // wrong reason and the green means nothing.
  if (probe.mustContainEscaped) {
    for (const marker of probe.mustContainEscaped) {
      if (!html.includes(marker)) {
        fail(`xss fixture never reached the DOM (expected to see the escaped form `
             + `${JSON.stringify(marker)} in ${probe.selectors}), so "no injection" `
             + `proves nothing`);
      }
    }
  }
}

const orphanHttp = trace.httpErrors.filter((m) => !httpAttributed.has(m));
if (orphanHttp.length) {
  const detail = orphanHttp.slice(0, 5).join("  |  ");
  fail("endpoint errors that no control was held responsible for — they happened, "
       + "after every interaction window, and nothing failed for them: " + detail);
}

report.summary = {
  controls: report.controls.length,
  distinctControls: covered.size,
  dead: report.controls.filter((c) => c.effect === "none").length,
  requests: trace.requests.length,
  blockedMutations: trace.blocked.length,
  dialogs: trace.dialogs.length,
  navigations: trace.navigations.length,
  errors: trace.errors.length,
};
report.requestTrace = trace.requests;
report.blockedRequests = trace.blocked;
report.dialogs = trace.dialogs;
report.ok = failures.length === 0;
ctx.dispose();

emit(report);

// A page whose scripts keep the event loop alive (a poller nobody cleared)
// must fail with a message, not hang the suite.
function emit(obj) {
  const s = JSON.stringify(obj);
  process.stdout.write(s, () => process.exit(0));
}
setTimeout(() => {
  try {
    process.stdout.write(
      JSON.stringify({ ...report, ok: false, failures: failures.concat("harness watchdog: the page never went idle") }),
      () => process.exit(1)
    );
  } catch {
    process.exit(1);
  }
}, spec.watchdogMs || 120000).unref();
