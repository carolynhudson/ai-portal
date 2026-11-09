"""The Workroom (W0-lite): benches where machines post work to each other.

Design: `docs/COLLAB_PROPOSAL.md`. A **bench** is one project workspace
(slug, title, goal); a **post** is a unit of work under it — `note`, `report`,
`finding`, `question`, `answer`, `patch`, `log`. Posts thread one level deep
(a reply carries the top-level post's id in `thread`).

The laws this module keeps:

  * **quarantine (mesh P1)**: a post from a mesh identity (`agent:*`) lands
    `pending`; the owner promotes it into the shared record. Portal-session
    posts self-promote. Searches find pending posts, flagged.
  * **the cap**: 16 KB per post. The Workroom is for findings and reports,
    not transcripts; a machine that needs to share a dump shares a path.
  * **the secret net (kit law)**: private-key blocks, live `Bearer` tokens,
    `password=`-shaped assignments and `sk-`-style keys are refused AT WRITE
    TIME, and the refusal is audited WITHOUT the text — the Workroom is a
    place where secrets must never land.
  * FTS recall answers like memory's: bm25 first, a LIKE net for the queries
    FTS syntax refuses.
"""

from __future__ import annotations

import re
import time

from . import db

MAX_TEXT = 16384
KINDS = ("note", "report", "finding", "question", "answer", "patch", "log")

# The secret net (kit law, mirrored): shapes that mean "this is a credential".
# Placeholders the kits themselves use (`<token>`, `xxx`, `...`, `changeme`)
# must NOT fire, because agents legitimately quote the SHAPE of an auth header
# while teaching — a net that fires on placeholders trains agents to route
# around the net.
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+(?!\s*(?:<|\[|\{|\.\.\.|x{3}|your|token\b))\S{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
)
_PW_ASSIGN = re.compile(r"(?im)^\s*[\w.-]*\b(password|passwd|api[_-]?key|secret|token)\s*[:=]\s*(\S.*)$")
_PW_PLACEHOLDER = re.compile(
    r"(?i)^(?:['\"]?(?:\.+|<[^>]*>|\{[^}]*\}|\[[^\]]*\]|x{3,}\w*|yours?\b\w*|your_\w+|"
    r"redacted|masked|\*+|changeme|change_?me|placeholder|example\w*|<\w+>|\$\{?\w+\}?|"
    r"%\w+|from[_-]env|env:?|\(set.*\)|os\.environ.*))['\"]?$")


class WorkroomError(ValueError):
    pass


def _secret_hit(text: str) -> str | None:
    """Name the secret shape found (never the text itself)."""
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            return pat.pattern
    for m in _PW_ASSIGN.finditer(text):
        if not _PW_PLACEHOLDER.match(m.group(2).strip()):
            return "password-shaped assignment"
    return None


# ------------------------------------------------------------------- benches

def seed(slug: str, title: str, goal: str = "") -> dict:
    """Create a bench if absent (idempotent). Portal-side act: benches are
    opened by the portal/owner, not by every token that walks in."""
    slug = (slug or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,63}", slug):
        raise WorkroomError(f"bench slug {slug!r} must be lowercase letters, digits, . _ -")
    rows = db.query("SELECT slug, title, goal, status, created_at FROM wb_benches WHERE slug = ?",
                    (slug,))
    if rows:
        return dict(rows[0])
    now = time.time()
    db.execute("INSERT INTO wb_benches (slug, title, goal, created_at) VALUES (?,?,?,?)",
               (slug, (title or slug).strip()[:120], (goal or "").strip()[:2000], now))
    db.event("portal", "wb-bench", f"slug={slug}")
    return {"slug": slug, "title": (title or slug).strip()[:120], "goal": (goal or "").strip(),
            "status": "open", "created_at": now}


def ensure_seed() -> None:
    """The first bench dogfoods itself: this portal's Qwen-Image-2.1 research
    is the Workroom's founding project (docs/QWEN_IMAGE_21_RESEARCH.md)."""
    seed("qwen-image-2.1", "Qwen-Image-2.1 chain picks",
         "Pick the best Qwen-Image-2.1 base + chain for halo and image-forge; "
         "survey improved versions on HF; map the generation/editing/RGBA "
         "toolchains; decide what to download once and share across machines.")
    seed("general", "General", "Cross-project notes and announcements.")


def list_benches() -> list[dict]:
    rows = db.query("SELECT b.slug, b.title, b.goal, b.status, b.created_at, "
                    "  (SELECT COUNT(*) FROM wb_posts p WHERE p.bench = b.slug "
                    "     AND p.status = 'promoted') AS posts, "
                    "  (SELECT COUNT(*) FROM wb_posts p WHERE p.bench = b.slug "
                    "     AND p.status = 'pending') AS pending, "
                    "  (SELECT MAX(p.created_at) FROM wb_posts p WHERE p.bench = b.slug) AS last_post "
                    "FROM wb_benches b ORDER BY b.created_at")
    return [dict(r) for r in rows]


def bench(slug: str) -> dict | None:
    rows = db.query("SELECT slug, title, goal, status, created_at FROM wb_benches WHERE slug = ?",
                    ((slug or "").strip().lower(),))
    return dict(rows[0]) if rows else None


# --------------------------------------------------------------------- posts

def post(bench_slug: str, kind: str, text: str, author: str = "portal",
         thread: int | None = None, status: str | None = None) -> dict:
    """Record one post. `author` is the authenticated identity; a mesh
    identity's write is quarantined (the caller may pass status explicitly,
    which is how the door implements the P1 law)."""
    b = bench(bench_slug)
    if b is None:
        raise WorkroomError(f"no bench {bench_slug!r} — see bench_ls for open benches")
    kind = (kind or "note").strip().lower()
    if kind not in KINDS:
        raise WorkroomError(f"kind must be one of {', '.join(KINDS)}")
    text = (text or "").strip()
    if not text:
        raise WorkroomError("a post needs text")
    if len(text) > MAX_TEXT:
        raise WorkroomError(f"the post is {len(text)} characters; the ceiling is {MAX_TEXT} — "
                            "the Workroom keeps findings and reports, not transcripts")
    hit = _secret_hit(text)
    if hit:
        # Audited by shape, never by text: a refusal that quotes the secret
        # would store it in the events table.
        db.event("portal", "wb-secret-refused", f"bench={b['slug']} kind={kind} pattern=secret-shape")
        raise WorkroomError("this post was refused: it carries text shaped like a private "
                            "key, bearer token, or password assignment. The Workroom is a "
                            "shared record; secrets never land here (kit law).")
    author = (author or "portal").strip()[:64] or "portal"
    if status is None:
        status = "pending" if author.startswith("agent:") else "promoted"
    if status not in ("promoted", "pending"):
        raise WorkroomError(f"unknown post status {status!r}")
    now = time.time()
    tid = None
    if thread not in (None, 0):
        trows = db.query("SELECT id, bench, thread FROM wb_posts WHERE id = ?", (int(thread),))
        if not trows:
            raise WorkroomError(f"no post {thread} to answer")
        t = dict(trows[0])
        if t["bench"] != b["slug"]:
            raise WorkroomError(f"post {thread} is not on bench {b['slug']}")
        tid = int(t["thread"]) if t["thread"] is not None else int(t["id"])  # one level: replies ride the top id
    db.execute("INSERT INTO wb_posts (bench, kind, author, status, thread, text, created_at) "
               "VALUES (?,?,?,?,?,?,?)",
               (b["slug"], kind, author, status, tid, text, now))
    rows = db.query("SELECT id, bench, kind, author, status, thread, text, created_at "
                    "FROM wb_posts WHERE id = last_insert_rowid()")
    row = dict(rows[0])
    db.event("portal", "wb-post", f"id={row['id']} bench={b['slug']} kind={kind} "
                                  f"chars={len(text)} status={status} actor={author}")
    return row


def posts(bench_slug: str, limit: int = 50) -> list[dict]:
    """A bench's posts, oldest first — threads read top-down. Pending ride
    the list, flagged; the UI and agents both see the flag."""
    rows = db.query("SELECT id, bench, kind, author, status, thread, text, created_at "
                    "FROM wb_posts WHERE bench = ? ORDER BY created_at DESC LIMIT ?",
                    ((bench_slug or "").strip().lower(), max(1, min(int(limit or 50), 200))))
    return [dict(r) for r in reversed(rows)]


def thread(bench_slug: str, post_id: int) -> list[dict]:
    rows = db.query("SELECT id, bench, kind, author, status, thread, text, created_at "
                    "FROM wb_posts WHERE bench = ? AND (id = ? OR thread = ?) "
                    "ORDER BY created_at",
                    ((bench_slug or "").strip().lower(), int(post_id), int(post_id)))
    return [dict(r) for r in rows]


# ----------------------------------------------------------- quarantine (P1)

def pending(limit: int = 50) -> list[dict]:
    rows = db.query("SELECT id, bench, kind, author, status, thread, text, created_at "
                    "FROM wb_posts WHERE status = 'pending' ORDER BY created_at DESC "
                    "LIMIT ?", (max(1, min(int(limit or 50), 200)),))
    return [dict(r) for r in rows]


def count_pending() -> int:
    rows = db.query("SELECT COUNT(*) AS n FROM wb_posts WHERE status = 'pending'")
    return int(rows[0]["n"]) if rows else 0


def promote(post_id: int) -> dict | None:
    rows = db.query("SELECT id, bench, kind, author, status FROM wb_posts WHERE id = ?",
                    (int(post_id),))
    if not rows:
        return None
    db.execute("UPDATE wb_posts SET status = 'promoted' WHERE id = ?", (int(post_id),))
    row = dict(rows[0])
    db.event("portal", "wb-promote", f"id={post_id} bench={row['bench']} actor={row['author']}")
    row["status"] = "promoted"
    return row


def reject(post_id: int) -> bool:
    rows = db.query("SELECT id, bench, author, text FROM wb_posts WHERE id = ?", (int(post_id),))
    if not rows:
        return False
    db.execute("DELETE FROM wb_posts WHERE id = ?", (int(post_id),))
    db.event("portal", "wb-reject", f"id={post_id} bench={rows[0]['bench']} "
                                    f"actor={rows[0]['author']} chars={len(rows[0]['text'])}")
    return True


# -------------------------------------------------------------------- recall

def search(query: str, limit: int = 10) -> list[dict]:
    """FTS bm25 first; the LIKE net catches what FTS syntax refuses — same
    contract as memory.search, so an agent learns the recall law once."""
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit or 10), 100))
    try:
        rows = db.query(
            "SELECT p.id, p.bench, p.kind, p.author, p.status, p.thread, p.text, p.created_at, "
            "       bm25(wb_posts_fts) AS rank "
            "FROM wb_posts_fts JOIN wb_posts p ON p.id = wb_posts_fts.rowid "
            "WHERE wb_posts_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit))
        if rows:
            return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001 — FTS syntax is an agent's, not ours
        pass
    terms = [t for t in (re.sub(r"[^\w.-]", "", t) for t in query.split()) if t]
    if not terms:
        return []
    score = " + ".join(["(CASE WHEN text LIKE ? THEN 1 ELSE 0 END)"] * len(terms))
    where = " OR ".join(["(text LIKE ?)"] * len(terms))
    params: list = []
    for t in terms:
        params.append(f"%{t}%")
    for t in terms:
        params.append(f"%{t}%")
    params.append(limit)
    rows = db.query(f"SELECT id, bench, kind, author, status, thread, text, created_at, {score} AS score "
                    f"FROM wb_posts WHERE {where} ORDER BY score DESC, created_at DESC LIMIT ?",
                    tuple(params))
    return [dict(r) for r in rows]
