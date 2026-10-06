// Markup insertion for the portal, with the checks the plain `innerHTML =` form skips.
//
// Every panel on the dashboard and the status pages is built from strings assembled out
// of server data (model names, job ids, log lines). `esc()` quotes text; it does not stop
// a value that *is* markup, and it does nothing to an attribute. This file is the single
// place markup is inserted: a fixed tag allow-list, a fixed attribute allow-list, links
// checked for an internal path, and event-handler attributes dropped. A template string
// that carries a script, an `on*` handler, or a `javascript:` URL renders as text.
//
// docs/PORTAL_PLAN.md sec 6.13 — why the panels are built this way.
(function () {
  'use strict';

  var TAGS = new Set(['SPAN', 'DIV', 'B', 'I', 'EM', 'STRONG', 'BR', 'P', 'SMALL',
                      'CODE', 'PRE', 'TABLE', 'THEAD', 'TBODY', 'TR', 'TH', 'TD',
                      'UL', 'OL', 'LI', 'H4', 'H5', 'H6', 'A', 'BUTTON', 'LABEL']);
  // `style` stays: the panels set widths and orders with it, and a stripped style turns
  // a working layout into a broken one with no message. It carries no script in modern
  // browsers (expression() is gone), so the trade is layout over a theoretical vector.
  var ATTRS = new Set(['class', 'title', 'colspan', 'rowspan', 'style']);

  // The validator, in one place: returns the path when it is a portal path, and null
  // when it is not. Nothing assigns a location from a value that skipped this.
  function internalHref(url) {
    var u = String(url == null ? '' : url);
    if (u.charAt(0) !== '/' || u.slice(0, 2) === '//') { return null; }  // absolute, //host
    if (u.indexOf('\\') !== -1 || /\s/.test(u)) { return null; }
    // Portal paths are plain: letters, digits, and these. No query, no fragment, no
    // scheme — a link with a query is a link built elsewhere and validated there.
    return /^\/[A-Za-z0-9\/._-]*$/.test(u) ? u : null;
  }

  // gotoInternal(path): the only redirect this project performs from a value.
  window.gotoInternal = function gotoInternal(path) {
    var safe = internalHref(path);
    if (!safe) { return; }
    // Second gate, and the one that matters: resolve against this page's own origin
    // and refuse anything that leaves it. A portal path is a path on this host, so
    // the comparison is always true for the links this project emits.
    var target = new window.URL(safe, window.location.origin);
    if (target.origin !== window.location.origin) { return; }
    window.location.assign(target.pathname);   // same-origin, proven by the check above
  };

  function clean(node) {
    var out = document.createDocumentFragment();
    Array.prototype.slice.call(node.childNodes).forEach(function (child) {
      if (child.nodeType === 3) { out.appendChild(document.createTextNode(child.nodeValue)); return; }
      if (child.nodeType !== 1) { return; }                    // comments, doctype
      if (!TAGS.has(child.tagName)) {
        // Keep the text a disallowed element carried; drop the element itself.
        out.appendChild(clean(child));
        return;
      }
      var el = document.createElement(child.tagName);
      Array.prototype.slice.call(child.attributes).forEach(function (a) {
        if (a.name.indexOf('on') === 0) { return; }            // event handlers
        if (!ATTRS.has(a.name)) { return; }
        if (a.name === 'style' && /url\s*\(|expression/i.test(a.value)) { return; }
        el.setAttribute(a.name, a.value);
      });
      if (child.tagName === 'A') {
        var href = internalHref(child.getAttribute('href'));
        if (!href) { out.appendChild(document.createTextNode(child.textContent)); return; }
        el.setAttribute('href', href);
        el.setAttribute('rel', 'noopener');
      }
      el.appendChild(clean(child));
      out.appendChild(el);
    });
    return out;
  }

  // safeSet(el, html): replace el's children with sanitized markup.
  window.safeSet = function safeSet(el, html) {
    if (!el) { return; }
    var doc = new DOMParser().parseFromString('<body>' + String(html) + '</body>',
                                             'text/html');
    el.textContent = '';
    el.appendChild(clean(doc.body));
  };
})();
