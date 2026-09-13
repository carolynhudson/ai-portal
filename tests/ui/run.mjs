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
const fail = (m) => failures.push(m);

const base = arg("base");
const pageName = arg("page");
const cookie = arg("cookie", "");
const allSpec = JSON.parse(readFileSync(arg("spec"), "utf8"));
const spec = (allSpec.pages || []).find((p) => p.name === pageName);

const report = {
  page: pageName,
  url: base + (spec ? spec.path : "?"),
  ok: false,
  failures,
  warnings,
  initial: {},
  tabs: [],
  controls: [],
  summary: {},
};

if (!spec) {
  fail(`no spec for page ${pageName}`);
  process.stdout.write(JSON.stringify(report), () => process.exit(0));
}

const url = base.replace(/\/$/, "") + spec.path;

let ctx;
try {
  ctx = await openPage({
    url,
    cookieHeader: cookie,
    blockMutations: spec.blockMutations || allSpec.blockMutations || [".*"],
    allowHttpErrors: (allSpec.allowHttpErrors || []).concat(spec.allowHttpErrors || []),
    fixtures: (allSpec.fixtures || []).concat(spec.fixtures || []),
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

// Stop the pollers before the sweep.
ctx.quiesce();
await ctx.idle({ quietMs: 80, maxMs: 2000 });

// ------------------------------------------------------------- control sweep
const requireConfirm = spec.requireConfirmFor || [];
const requireConfirmUnless = spec.requireConfirmUnlessBodyContains || {};
const expectInert = (allSpec.expectInert || []).concat(spec.expectInert || []);

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
    visited.add(key);
    if (el.disabled) continue;
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

    for (const m of r.newErrors) fail(`${info.selector} threw: ${m}`);
    for (const m of r.newConsoleErrors) fail(`${info.selector} logged console.error: ${m}`);
    for (const m of r.newMissedIds) fail(`${info.selector} looked up missing id #${m}`);
    for (const m of r.newHttpErrors) fail(`${info.selector} hit an endpoint error: ${m}`);

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

    const toleratedNoop = samePage || r.expectedNoop;
    if (r.effect === "none" && !toleratedNoop && !expectInert.some((p) => new RegExp(p).test(info.selector))) {
      const what = [info.text && `text=${JSON.stringify(info.text)}`, info.handler && `handler=${JSON.stringify(info.handler)}`]
        .filter(Boolean)
        .join(" ");
      fail(`dead control: ${info.selector}${what ? " " + what : ""} — no request, no dialog, no navigation, no DOM change`);
    }

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
const covered = new Set();
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
    if (!covered.has(el.__uiId)) missedCoverage.push(`${tab ? (tab.textContent || "").trim().slice(0, 20) : "default"}: ${baseIdentity(el)}`);
  }
}
const uniqueMisses = [...new Set(missedCoverage)];
if (uniqueMisses.length)
  fail(`controls never driven: ${uniqueMisses.slice(0, 12).join("  |  ")}` +
       (uniqueMisses.length > 12 ? ` (+${uniqueMisses.length - 12} more)` : ""));

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
