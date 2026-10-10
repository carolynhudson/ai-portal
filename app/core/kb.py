"""The portal's knowledge base: the project's own docs, linkable and searchable.

Why this exists
---------------
The settings registry explains a key in one or two sentences and cites the measurement that
earned it -- "§6.12.6", "§4.10.1". Those pointers are good: they keep a setting's detail brief.
They were also dead text. The document they point into is a 215 KB file in the repo that nobody
opens from the page they are reading, so a citation read as decoration.

This module makes the citation mean something: it parses the docs into sections, gives every
numbered section the id the code cites it by, and renders them at /help with search. A setting's
help can then link to exactly the section it named.

Two rules this module lives by
------------------------------
* **Section ids come from the heading text, not from a table maintained here.** A hand-written
  id map is a second source of truth, and the day a section is inserted the map is wrong while
  every page still renders. The heading `#### 6.12.10 — why the MTP run...` *is* the id.
* **A citation that resolves to nothing is a defect, and it is tested as one.** See
  `test_every_documented_reference_in_the_registry_resolves` in tests/test_kb.py: the registry
  may not point at a section that does not exist.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

# Docs worth reading from the portal, in reading order. PLAN.md is the bootstrap-era plan and is
# deliberately absent: it is superseded by PORTAL_PLAN.md, and a search page that returns both
# invites reading the obsolete one.
DOC_ORDER = [
    ("PORTAL_PLAN.md", "Portal plan — subsystems, decisions, and measured findings"),
    ("MODEL_SIGNALS.md", "Model signals — how to know what a GGUF can do before loading it"),
    ("ENVIRONMENT.md", "This box — hardware, units, paths, and the machine's limits"),
    ("DEBUGGING.md", "Debugging — instrument failures and how to verify a probe"),
    ("UPDATES.md", "Update pipeline — builds, releases, and the gated jobs"),
    ("PORTAL_SETTINGS.md", "Settings reference — every key a personality can set, from the registry"),
    ("PORTAL_UI.md", "Interface baseline — tokens, scale, components, page ownership, measured gaps"),
    ("PORTAL_LAYOUT.md", "Layout spec — hero, ribbon, owner pages, sub-pages, acceptance criteria"),
]

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# A heading that leads with a dotted number owns that number: "## 4. Subsystems",
# "### 4.10 ...", "#### 6.12.10 — why ...". Everything else gets a slug.
_LEAD_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)\.?\s*(?:—|-|:|\s)\s*(.*)$")


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:60] or "section"


def anchor_of(title: str) -> str:
    """The section id a heading with this title gets — the thing a `/help#…` link points at.

    Public because the anchor is a claim shared between a document and the pages that link into it:
    the settings reference headings a key by its own name, and the settings rows have to build the
    same slug `_split_sections` will assign to it. Duplicating the slug rule in the page is how a
    link to a section that does not exist gets printed, which is the failure `/help` had once.
    """
    return _slug(title)


def _doc_root(docs_dir: Path | None = None) -> Path:
    """The docs directory: a sibling of the package, not inside it.

    Resolved from this file, the same way `app.main` resolves `web/`. The portal is deployed in
    place, so the checkout the docs live in is the checkout that serves them.
    """
    root = Path(docs_dir) if docs_dir else Path(__file__).resolve().parents[2] / "docs"
    if not root.is_dir():
        raise FileNotFoundError(f"docs directory not found: {root}")
    return root


def _split_sections(text: str, doc: str) -> list[dict]:
    """Split one markdown file into sections at every heading.

    A section's body is the text under its heading up to the next heading at any level, so
    "6.12.6" contains its own prose and nothing from 6.12.7. Subsections are listed so a reader
    can jump, not to nest the body.
    """
    lines = text.splitlines()
    out: list[dict] = []
    cur: dict | None = None
    preamble: list[str] = []
    for i, ln in enumerate(lines):
        m = _HEADING.match(ln)
        if not m:
            (cur["lines"] if cur else preamble).append(ln)
            continue
        level = len(m.group(1))
        title = m.group(2).strip()
        num = _LEAD_NUMBER.match(title)
        sid = num.group(1) if num else None
        if cur:
            cur["body"] = "\n".join(cur.pop("lines")).strip()
        cur = {
            "doc": doc,
            "level": level,
            "title": num.group(2) if num else title,
            "number": sid,
            "id": sid or _slug(title),
            "line": i + 1,
            "lines": [],
        }
        out.append(cur)
    if cur:
        cur["body"] = "\n".join(cur.pop("lines")).strip()
    head = "\n".join(preamble).strip()
    if head:
        out.insert(0, {"doc": doc, "level": 0, "title": "(preamble)", "number": None,
                       "id": "preamble", "line": 1, "body": head})
    return out


_cache: tuple[tuple, list[dict]] = ((), [])


def _fingerprint(root: Path) -> tuple:
    stamps = []
    for name, _ in DOC_ORDER:
        p = root / name
        if p.is_file():
            st = p.stat()
            stamps.append((name, int(st.st_mtime), st.st_size))
    return tuple(stamps)


def sections(docs_dir: Path | None = None) -> list[dict]:
    """Every section of every documented file, cached and re-parsed when a file changes."""
    global _cache
    root = _doc_root(docs_dir)
    fp = _fingerprint(root)
    if _cache[0] == fp and _cache[1]:
        return _cache[1]
    rows: list[dict] = []
    for name, _title in DOC_ORDER:
        p = root / name
        if not p.is_file():
            continue
        rows.extend(_split_sections(p.read_text(encoding="utf-8", errors="replace"), name))
    _cache = (fp, rows)
    return rows


def normalize_ref(ref: str) -> str:
    """`§6.12.6`, `6.12.6`, `help#6.12.6` -> `6.12.6`."""
    s = str(ref).strip().lstrip("#").lstrip("§").strip()
    m = re.search(r"\d+(?:\.\d+)*", s)
    return m.group(0) if m else s


def find(ref: str, docs_dir: Path | None = None) -> dict | None:
    """The section a citation names, or None. None is a defect in the citation, not an answer.

    Two shapes arrive here. A number (`6.12.6`, `§6.12.6`, `help#6.12.6`) and a slug
    (`measured-catalogue-2026-10-02-b11146-this-box`) -- the page links slugs for headings that
    carry no number. Normalizing a slug as a number is what broke it: the digits inside that slug
    were read as `2026.10.02`, which names nothing, so a link the page itself printed returned
    404. So the number form is applied only when the reference contains no letters.
    """
    raw = str(ref).strip().lstrip("\u00a7").strip()
    if "#" in raw:  # `help#6.12.6`, `#6.12.6`: the anchor is what follows the hash
        raw = raw.rsplit("#", 1)[1].strip()
    if not raw:
        return None
    rows = sections(docs_dir)
    for s in rows:
        if s["id"] == raw:
            return s
    if not re.search(r"[A-Za-z]", raw):
        want = normalize_ref(raw)
        for s in rows:
            if s["id"] == want:
                return s
    lowered = raw.lower()
    for s in rows:
        if s["id"].lower() == lowered:
            return s
    return None


def refs_in(text: str) -> list[str]:
    """The section numbers a passage cites, in order, de-duplicated."""
    out: list[str] = []
    for m in re.finditer(r"§\s*(\d+(?:\.\d+)*)", str(text)):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def search(query: str, limit: int = 30, docs_dir: Path | None = None) -> list[dict]:
    """Rank sections by how much of the query they contain.

    Title hits count for more than body hits, because a reader searching "kv unified" wants the
    section *named* for it, not every section that mentions it in passing. A section that
    mentions the query 40 times is not 40 times more relevant, so body matches are capped.
    """
    terms = [t for t in re.split(r"[^a-z0-9]+", str(query).lower()) if len(t) >= 2]
    if not terms:
        return []
    scored = []
    for s in sections(docs_dir):
        title = s["title"].lower()
        body = s["body"].lower()
        score = 0
        for t in terms:
            if t in title:
                score += 10
            n = body.count(t)
            if n:
                score += 1 + min(n, 5)
        if score:
            d = dict(s)
            d["score"] = score
            d["excerpt"] = _excerpt(s["body"], terms)
            scored.append(d)
    scored.sort(key=lambda r: (-r["score"], r["doc"], r["id"]))
    return scored[:limit]


def _excerpt(body: str, terms: list[str], width: int = 220) -> str:
    low = body.lower()
    pos = min((low.find(t) for t in terms if t in low), default=-1)
    if pos < 0:
        return body[:width].strip()
    start = max(0, pos - 60)
    return ("…" if start else "") + body[start:start + width].strip() + "…"


def docs_available(docs_dir: Path | None = None) -> list[tuple[str, str]]:
    root = _doc_root(docs_dir)
    return [(n, t) for n, t in DOC_ORDER if (root / n).is_file()]


# ------------------------------------------------------------------ rendering
#
# A markdown renderer, deliberately small. The docs use headings, paragraphs, bullet lists,
# fenced blocks, pipe tables, inline code and bold -- nothing else load-bearing -- and adding a
# dependency for that is a runtime package the deploy syncs. Everything is escaped first and
# decorated second: text that looks like a tag never becomes one, and the only links emitted are
# http(s), mailto and in-page anchors.

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_SAFE_SCHEME = re.compile(r"^(https?://|mailto:|#|/)", re.I)


def _inline(text: str) -> str:
    out = html.escape(text)
    # Links are rewritten on escaped text, so the URL is already entity-free of `<`/`>` and a
    # `javascript:` URL is simply not a scheme we render.
    def link(m: re.Match) -> str:
        label, url = m.group(1), html.unescape(m.group(2))
        if not _SAFE_SCHEME.match(url):
            return html.escape(m.group(1))
        return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'

    out = _LINK.sub(link, out)
    out = _INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", out)
    # §citations become links into /help, which is the whole point of this module.
    out = re.sub(r"§(\d+(?:\.\d+)*)",
                 lambda m: f'<a class="kbref" href="/help#{m.group(1)}">§{m.group(1)}</a>', out)
    return out


def _table(rows: list[str]) -> str:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    header, body = cells[0], [c for c in cells[1:] if not all(re.fullmatch(r":?-{2,}:?", x or "-")
                                                              for x in c)]
    head = "".join(f"<th>{_inline(c)}</th>" for c in header)
    body_html = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
                        for row in body)
    return f"<table class='kb'><thead><tr>{head}</tr></thead><tbody>{body_html}</tbody></table>"


def render_markdown(text: str) -> str:
    """Render the markdown subset these docs use. Escaped first; no HTML passes through."""
    out: list[str] = []
    lines = (text or "").splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre class='kb'>" + html.escape("\n".join(buf)) + "</pre>")
            continue
        if ln.strip().startswith("|") and i + 1 < len(lines) and re.search(r"\|\s*:?-", lines[i + 1]):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
            continue
        m = _HEADING.match(ln)
        if m:
            level = len(m.group(1))
            out.append(f"<h{min(level + 1, 6)} class='kb'>{_inline(m.group(2))}</h{min(level + 1, 6)}>")
            i += 1
            continue
        if ln.strip().startswith(("- ", "* ")):
            items = []
            while i < len(lines) and lines[i].strip().startswith(("- ", "* ")):
                items.append(_inline(lines[i].strip()[2:]))
                i += 1
            out.append("<ul class='kb'>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
            continue
        if not ln.strip():
            i += 1
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not lines[i].startswith(("```", "#")) \
                and not lines[i].strip().startswith(("|", "- ", "* ")):
            para.append(lines[i].strip())
            i += 1
        out.append("<p class='kb'>" + _inline(" ".join(para)) + "</p>")
    return "\n".join(out)


def section_html(ref: str, docs_dir: Path | None = None) -> str:
    s = find(ref, docs_dir)
    if not s:
        return ""
    return render_markdown(s["body"])


def toc(docs_dir: Path | None = None) -> list[dict]:
    """The sections worth listing: numbered plan sections plus every named heading."""
    return [{"doc": s["doc"], "id": s["id"], "level": s["level"], "title": s["title"]}
            for s in sections(docs_dir) if s["level"] >= 2]


def warm() -> int:
    """Parse the docs once (at startup) so the first /help request does not pay for it."""
    n = len(sections())
    return n


def ref_of(cite: str | None) -> str | None:
    """The section id inside a citation string, for building a `/help#id` link.

    Citations in the registry read `docs/PORTAL_PLAN.md sec 6.12.20, server.cpp:161-166`:
    prose plus source lines. A link needs only the section number, and taking the first
    `sec N.N` match is what keeps a multi-part citation from turning `161-166` into a
    section request that 404s. Returns None when the citation names no section, so the
    caller renders text without a link instead of a link to nothing.
    """
    if not cite:
        return None
    m = re.search(r"\bsec(?:tion)?\s+(\d+(?:\.\d+)*)", str(cite))
    return m.group(1) if m else None
