// jsdom driver for the AI Portal UI.
//
// Why a real DOM and not string matching: the bugs this harness exists to
// catch are "the JS on this page no longer works" — a template edit that
// breaks a <script> block, a renamed element id, a button wired to a function
// that no longer exists, a handler that posts to a route that was renamed.
// None of those show up in the HTML; all of them show up the moment the page's
// own scripts run.
//
// The page is loaded from a live (sandboxed) portal over HTTP, its scripts
// execute, and every interactable element is then driven for real: clicks,
// change events, typing, submits. Each interaction is scored on
//   - did it throw, or log console.error
//   - did it look up a DOM id that does not exist
//   - did it hit an endpoint that answered 4xx/5xx
//   - did it do *anything* (request / dialog / navigation / DOM or value change)
//     — a control that does nothing is what "the button stopped working" looks
//     like from the user's side
//
// Safety: mutating requests (POST/PUT/DELETE) are matched against a block
// list. A blocked request is NOT sent — the harness answers it with a synthetic
// success so the page still takes its success path (and the reload that
// follows is recorded as an effect) — and the request itself is reported so the
// Python side can assert the contract: url, method, body. That is what keeps a
// UI test from rebuilding llama.cpp or restarting the router a live agent
// session is riding on.

import { JSDOM, VirtualConsole, ResourceLoader, CookieJar } from "jsdom";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Answer for a blocked mutating request: one shape covering every caller in
// these pages ({ok} for service/settings handlers, {job}/{id} for the
// job-starting ones).
const SYNTHETIC_OK = {
  ok: true,
  job: 4242,
  id: 4242,
  status: "running",
  log: "[harness] accepted, not executed",
  target: "harness",
  detail: "blocked-by-ui-harness",
};

// jsdom cannot navigate: a reload, a link click or a form POST ends up as a
// "not implemented" error. For a test driver that is the *successful outcome*
// of these controls, so it is counted as a navigation, never as a failure.
const NAVIGATION = [
  /Not implemented: navigation/i,
  /Not implemented: HTMLFormElement\.prototype\.submit/i,
  /Not implemented: window\.open/i,
];
// ...and these are environment gaps that a stub below closes (canvas) or that
// no page can avoid (jsdom's older CSS parser). Counted, never failed on.
const UNSUPPORTED = [
  /Not implemented: HTMLCanvasElement\.prototype\.getContext/i,
  /Could not parse CSS/i,
];

function looksLikeMutation(method) {
  return !["GET", "HEAD", "OPTIONS"].includes((method || "GET").toUpperCase());
}

function matchesAny(patterns, value) {
  return (patterns || []).some((p) => new RegExp(p).test(value));
}

// jsdom performs no layout, so "can the user reach this" is derived from the
// mechanisms the app itself uses to hide things: display:none, an inactive tab
// panel, a closed <details>.
function isVisible(el) {
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    if (n.hasAttribute("hidden")) return false;
    if (/display\s*:\s*none/i.test(n.getAttribute("style") || "")) return false;
    if (n.matches && n.matches(".update-tab-content:not(.active), .model-tab-content:not(.active)")) return false;
    if (n.tagName === "NOSCRIPT") return false;
  }
  return true;
}

export const INTERACTABLE = [
  "a[href]",
  "button",
  "input",
  "select",
  "textarea",
  "summary",
  "[onclick]",
  "[onchange]",
  "[onsubmit]",
  "[role='tab']",
  ".update-tab",
  ".model-tab",
  ".search-result-item",
].join(",");

// Stable, human-readable handle for a control, used as its identity in the
// report and as the "already visited" key across DOM re-renders.
export function describeElement(el, win) {
  const all = Array.from(win.document.querySelectorAll(INTERACTABLE));
  const nth = all.indexOf(el);
  const kind =
    el.tagName.toLowerCase() +
    (el.getAttribute("type") ? `[type=${el.getAttribute("type")}]` : "");
  const text = (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 40);
  const handler =
    el.getAttribute("onclick") || el.getAttribute("onchange") || el.getAttribute("onsubmit") || "";
  const form = el.form
    ? { action: el.form.getAttribute("action") || null, method: (el.form.getAttribute("method") || "get").toLowerCase() }
    : null;
  return {
    selector: `${kind}${el.id ? "#" + el.id : ""}[nth=${nth}]`,
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute("type") || null,
    id: el.id || null,
    name: el.getAttribute("name") || null,
    text,
    handler: handler.slice(0, 90),
    href: el.tagName === "A" ? el.getAttribute("href") : null,
    form,
    disabled: !!el.disabled,
  };
}

// A 2D context that accepts anything. Real browsers hand one back; jsdom
// returns null unless the optional `canvas` package is installed, and without
// it every chart-drawing line would "fail" for the wrong reason.
function installCanvasStub(win) {
  const contexts = new WeakMap();
  const stub = () => {
    const target = { canvas: null };
    return new Proxy(target, {
      get(t, prop) {
        if (prop === "canvas") return t.canvas;
        if (prop in t) return t[prop];
        return (...args) => undefined;
      },
      set(t, prop, v) {
        t[prop] = v;
        return true;
      },
    });
  };
  try {
    win.HTMLCanvasElement.prototype.getContext = function () {
      if (!contexts.has(this)) {
        const c = stub();
        c.canvas = this;
        contexts.set(this, c);
      }
      return contexts.get(this);
    };
  } catch {
    /* jsdom changed shape; charts then report as unsupported */
  }
}

export async function openPage({
  url,
  cookieHeader = "",
  blockMutations = [".*"],
  allowHttpErrors = [],
  fixtures = [],
  maxWaitMs = 20000,
}) {
  const base = new URL(url);
  const trace = {
    requests: [],
    httpErrors: [],
    dialogs: [],
    missedIds: [],
    errors: [],
    navigations: [],
    consoleErrors: [],
    blocked: [],
    unsupported: [],
  };

  const jar = new CookieJar();
  if (cookieHeader) {
    // host-only cookie inferred from the URL: tough-cookie rejects an explicit
    // `domain=127.0.0.1` as a public suffix
    for (const pair of cookieHeader.split(/;\s*/)) {
      if (pair.includes("=")) await jar.setCookie(pair, url);
    }
  }

  // <script src="/static/htmx.min.js"> must load, or the page under test is
  // not the page a user gets.
  class AuthLoader extends ResourceLoader {
    fetch(resourceUrl, opts) {
      const headers = Object.assign({}, opts && opts.headers, cookieHeader ? { cookie: cookieHeader } : {});
      return super.fetch(resourceUrl, Object.assign({}, opts, { headers, accept: "*/*" }));
    }
  }

  const vc = new VirtualConsole();
  vc.on("jsdomError", (e) => {
    const msg = (e && e.message ? e.message : String(e)) + (e && e.detail ? ` ${e.detail}` : "");
    const first = msg.split("\n")[0];
    if (NAVIGATION.some((re) => re.test(msg))) {
      trace.navigations.push(first);
      return;
    }
    if (UNSUPPORTED.some((re) => re.test(msg))) {
      trace.unsupported.push(first);
      return;
    }
    trace.errors.push("jsdomError: " + msg);
  });
  vc.on("error", (...args) => {
    const line = args.map((a) => (a && a.stack ? a.stack : String(a))).join(" ").trim();
    if (!UNSUPPORTED.some((re) => re.test(line))) trace.consoleErrors.push(line.slice(0, 400));
  });
  vc.on("warn", () => {}); // jsdom warns about CSS/features we cannot avoid

  const intervals = new Set();
  const timeouts = new Set();
  let inFlight = 0;
  let numberSeed = 0;

  const dom = await JSDOM.fromURL(url, {
    runScripts: "dangerously",
    resources: new AuthLoader({ userAgent: "ai-portal-ui-test", cookieJar: jar }),
    pretendToBeVisual: true,
    virtualConsole: vc,
    cookieJar: jar,
    beforeParse(win) {
      installCanvasStub(win);

      // htmx's only XPath use is collecting elements that carry an hx-on*/hx-*
      // attribute, and it calls document.evaluate(expr, ctx) with no result
      // type — legal in browsers (ANY_TYPE), rejected by jsdom. These templates
      // carry no hx-* attributes (asserted by test_ui_contract), so the honest
      // answer is an empty set; pretending otherwise would make the harness
      // depend on which jsdom is installed.
      const emptyResult = {
        resultType: 5,
        iterateNext: () => null,
        snapshotLength: 0,
        snapshotItem: () => null,
        singleNodeValue: null,
      };
      const nativeEvaluate = win.Document && win.Document.prototype.evaluate;
      if (nativeEvaluate) {
        win.Document.prototype.evaluate = function (expr, ctx, resolver, type, result) {
          if (type === undefined || type === null || type === 0) {
            trace.unsupported.push(`document.evaluate(${String(expr).slice(0, 40)}…) defaulted to ANY_TYPE`);
            return emptyResult;
          }
          return nativeEvaluate.call(this, expr, ctx, resolver, type, result);
        };
      }

      // --- a lookup that misses is a renamed/removed element
      const getById = win.Document.prototype.getElementById;
      win.Document.prototype.getElementById = function (id) {
        const el = getById.call(this, id);
        if (!el) trace.missedIds.push(String(id));
        return el;
      };

      // --- fetch: same-origin, session cookie attached, every call recorded
      const nativeFetch = globalThis.fetch;
      win.fetch = async function (input, opts = {}) {
        const href = typeof input === "string" ? input : input && input.url;
        const abs = new URL(href, url);
        const method = (opts.method || (input && input.method) || "GET").toUpperCase();
        const rawBody = opts.body ?? null;
        const rec = {
          method,
          url: abs.href,
          path: abs.pathname,
          query: abs.search.replace(/^\?/, ""),
          body: typeof rawBody === "string" ? rawBody : rawBody === null ? null : String(rawBody),
          status: null,
          blocked: false,
          fixture: false,
        };
        trace.requests.push(rec);

        const answer = (status, obj, isFixture) => {
          rec.status = status;
          rec.fixture = !!isFixture;
          return new Response(JSON.stringify(obj), {
            status,
            headers: { "content-type": "application/json" },
          });
        };

        // fixtures answer GETs by default: a canned /api/updates/x must not
        // swallow the POST that test-deploys it, which has to fall through to
        // the block list so its request shape is recorded
        const fix = (fixtures || []).find(
          (f) => new RegExp(f.path).test(abs.pathname) &&
            (f.methods || ["GET"]).includes(method));
        if (fix) return answer(fix.status || 200, fix.body, true);

        if (abs.origin !== base.origin) {
          rec.status = "cross-origin-refused";
          trace.errors.push(`page JS tried a cross-origin request: ${abs.href}`);
          throw new Error(`harness: cross-origin fetch to ${abs.origin}`);
        }
        if (looksLikeMutation(method) && matchesAny(blockMutations, abs.pathname)) {
          rec.blocked = true;
          trace.blocked.push(rec);
          return answer(200, SYNTHETIC_OK);
        }

        const headers = Object.assign({}, opts.headers);
        if (cookieHeader && !headers.cookie && !headers.Cookie) headers.cookie = cookieHeader;
        inFlight++;
        try {
          const res = await nativeFetch(abs.href, { method, headers, body: rawBody, redirect: "manual" });
          const text = await res.text();
          rec.status = res.status;
          if (res.status >= 400 && !matchesAny(allowHttpErrors, `${method} ${abs.pathname}`))
            trace.httpErrors.push(`${method} ${abs.pathname} -> ${res.status}`);
          return new Response(text, {
            status: res.status,
            statusText: res.statusText,
            headers: Object.fromEntries(res.headers.entries()),
          });
        } catch (e) {
          rec.status = "network-error";
          if (!matchesAny(allowHttpErrors, `${method} ${abs.pathname}`))
            trace.httpErrors.push(`${method} ${abs.pathname} -> ${e.message}`);
          throw e;
        } finally {
          inFlight--;
        }
      };

      // --- dialogs: recorded; the answer is a switch the driver flips
      win.__dialogAnswer = false;
      for (const kind of ["confirm", "alert", "prompt"]) {
        win[kind] = (msg) => {
          trace.dialogs.push({ kind, message: String(msg).slice(0, 200) });
          return kind === "confirm" ? win.__dialogAnswer : undefined;
        };
      }

      // --- uncaught errors, including SyntaxError in an inline <script>
      win.addEventListener("error", (e) => {
        trace.errors.push(`uncaught: ${e.error && e.error.message ? e.error.message : e.message}`);
      });
      win.addEventListener("unhandledrejection", (e) => {
        trace.errors.push(`unhandled rejection: ${e.reason && e.reason.message ? e.reason.message : e.reason}`);
      });

      // --- own the timers: the driver must stop the pollers before sweeping
      // (so an effect belongs to the control that caused it) and before exiting
      // (or a page's 3s job-poller keeps the node process alive forever)
      const si = win.setInterval.bind(win);
      const ci = win.clearInterval.bind(win);
      const st = win.setTimeout.bind(win);
      const ct = win.clearTimeout.bind(win);
      win.setInterval = (fn, ms, ...a) => {
        const h = si(fn, ms, ...a);
        intervals.add(h);
        return h;
      };
      win.clearInterval = (h) => {
        intervals.delete(h);
        return ci(h);
      };
      win.setTimeout = (fn, ms, ...a) => {
        const h = st(fn, ms, ...a);
        timeouts.add(h);
        // a completed timer is no longer pending
        st(() => timeouts.delete(h), ms + 50);
        return h;
      };
      win.clearTimeout = (h) => {
        timeouts.delete(h);
        return ct(h);
      };

      // Unique values per interaction: a second sweep must actually change
      // something, or a text field would be reported as a dead control because
      // it already contains the value the driver typed.
      win.__nextPort = () => String(20100 + (++numberSeed % 400));
      win.__nextText = () => `ui-test-value-${++numberSeed}`;
    },
  });

  const win = dom.window;

  async function idle({ quietMs = 120, maxMs = maxWaitMs } = {}) {
    const t0 = Date.now();
    for (;;) {
      if (inFlight === 0) {
        await sleep(quietMs);
        if (inFlight === 0) return true;
      } else {
        await sleep(25);
      }
      if (Date.now() - t0 > maxMs) return false;
    }
  }

  // Stop background polling: an interaction's effects must be attributable to
  // the control that caused them, and the driver must be able to exit.
  function quiesce() {
    for (const h of Array.from(intervals)) {
      try {
        win.clearInterval(h);
      } catch {
        /* already cleared */
      }
    }
    intervals.clear();
    for (const h of Array.from(timeouts)) {
      try {
        win.clearTimeout(h);
      } catch {
        /* already cleared */
      }
    }
    timeouts.clear();
  }

  // Release jsdom's own handles so the node process can exit.
  function dispose() {
    quiesce();
    try {
      win.close();
    } catch {
      /* already closed */
    }
  }

  return { dom, win, trace, idle, quiesce, dispose, isVisible, INTERACTABLE };
}

// ---------------------------------------------------------------------------
// interaction

export function listInteractables(ctx) {
  const out = [];
  for (const el of ctx.win.document.querySelectorAll(ctx.INTERACTABLE)) {
    if (el.tagName === "FORM") continue; // driven through its submit button
    if (el.tagName === "A" && /^(javascript|mailto):/i.test(el.getAttribute("href") || "")) continue;
    if (!ctx.isVisible(el)) continue;
    out.push(el);
  }
  return out;
}

// Drive one control the way a user would, and report what changed because of
// it. `answerDialogs: true` accepts confirm() so destructive paths run (into
// the harness block list, never into the real system).
// `noopCheck(el)` may return a string explaining why this element is expected
// to change nothing (e.g. clicking the tab that is already showing) — see
// run.mjs. Without it, legitimate no-ops would be reported as dead controls.
export async function interact(ctx, el, { answerDialogs = false, noopCheck = null } = {}) {
  const { win, trace } = ctx;
  const mark = {
    errors: trace.errors.length,
    consoleErrors: trace.consoleErrors.length,
    httpErrors: trace.httpErrors.length,
    requests: trace.requests.length,
    dialogs: trace.dialogs.length,
    navigations: trace.navigations.length,
    missedIds: trace.missedIds.length,
  };
  const htmlBefore = win.document.body ? win.document.body.innerHTML : "";
  const valueBefore = "value" in el ? el.value : null;
  const checkedBefore = "checked" in el ? el.checked : null;

  const attempted = [];
  let defaultPrevented = false;
  let expectedNoop = null;
  const fire = (type) => {
    attempted.push(type);
    el.dispatchEvent(new win.Event(type, { bubbles: true, cancelable: true }));
  };

  win.__dialogAnswer = answerDialogs;
  const tag = el.tagName;
  const inputType = (el.getAttribute("type") || "").toLowerCase();
  if (noopCheck) expectedNoop = noopCheck(el) || null;

  if (el.disabled) {
    // nothing to do; the caller reports disabled controls separately
  } else if (tag === "SELECT") {
    const opts = Array.from(el.options || []);
    if (opts.length > 1) {
      const next = opts.find((o) => o.value !== el.value) || opts[0];
      el.value = next.value;
      fire("change");
    } else {
      expectedNoop = "select offers one option";
    }
  } else if (tag === "INPUT" && (inputType === "checkbox" || inputType === "radio")) {
    if (inputType === "radio" && el.checked) {
      // clicking the selected radio of a group changes nothing — in a browser too
      expectedNoop = "radio already selected";
    } else {
      if (inputType === "checkbox") el.checked = !el.checked;
      else el.checked = true;
      fire("input");
      fire("change");
    }
  } else if (tag === "INPUT" && ["text", "search", "url", "password", "number", "tel", ""].includes(inputType)) {
    el.value = inputType === "number" ? win.__nextPort() : win.__nextText();
    fire("input");
    fire("change");
  } else if (tag === "TEXTAREA") {
    el.value = win.__nextText();
    fire("input");
    fire("change");
  } else if (tag === "BUTTON" && (el.getAttribute("type") || "submit") === "submit" && el.form) {
    attempted.push("submit");
    // Fire the submit event (that is what the inline onsubmit handlers hook).
    // If nothing in the page handled it, the browser would now navigate to the
    // form's action — say so by asking jsdom to actually submit, which it
    // reports as "not implemented" and the driver counts as a navigation.
    const ev = new win.Event("submit", { bubbles: true, cancelable: true });
    el.form.dispatchEvent(ev);
    defaultPrevented = ev.defaultPrevented;
    if (!ev.defaultPrevented && typeof el.form.submit === "function") {
      attempted.push("form.submit");
      try {
        el.form.submit();
      } catch {
        /* jsdom: not implemented — already recorded as a navigation */
      }
    }
  } else {
    attempted.push("click");
    el.dispatchEvent(new win.MouseEvent("click", { bubbles: true, cancelable: true, view: win }));
  }

  await ctx.idle({ quietMs: 60, maxMs: 8000 });

  const newRequests = trace.requests.slice(mark.requests);
  const newDialogs = trace.dialogs.slice(mark.dialogs);
  const newNavigations = trace.navigations.slice(mark.navigations);
  const htmlAfter = win.document.body ? win.document.body.innerHTML : "";
  const domChanged =
    htmlAfter !== htmlBefore ||
    ("value" in el && el.value !== valueBefore) ||
    ("checked" in el && el.checked !== checkedBefore);

  return {
    attempted,
    defaultPrevented,
    expectedNoop,
    newRequests,
    newDialogs,
    newNavigations,
    newErrors: trace.errors.slice(mark.errors),
    newConsoleErrors: trace.consoleErrors.slice(mark.consoleErrors),
    newHttpErrors: trace.httpErrors.slice(mark.httpErrors),
    newMissedIds: trace.missedIds.slice(mark.missedIds),
    domChanged,
    effect:
      newRequests.length + newDialogs.length + newNavigations.length > 0 || domChanged ? "yes" : "none",
  };
}
