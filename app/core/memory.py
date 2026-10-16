"""Agent memory — what harness sessions record for the sessions that follow them.

The owner's standing ask: opencode sessions should remember, across sessions, without
a second product. The portal already keeps one durable, audited store every component
trusts — its sqlite base — so memory lives there, and the harness reaches it through
one small MCP server (`kb_mcp.py`), never through the browser.

What this module refuses, and why:

  * empty and absurdly long text: a memory store fills up fastest when agents use it
    as a transcript. 8000 characters is a generous note and a refusal of a dump;
  * an FTS query that is not valid FTS syntax answers as a LIKE search, not as an
    error — the caller is an agent mid-task, and "your quotes are unbalanced" is a
    worse answer than a short list.

The recall is FTS5 (measured available on this box's python 3.14 sqlite, 2026-10-16);
embedding-based recall is the next step, and needs `ggml-org/embeddinggemma-300M-GGUF`
loaded — which is a router decision this module deliberately does not make.
"""

from __future__ import annotations

import re
import time

from . import db

MAX_TEXT = 8000


class MemoryError(ValueError):
    pass


def add(text: str, tags: str = "", source: str = "") -> dict:
    """Record one memory. Returns the stored row."""
    text = (text or "").strip()
    if not text:
        raise MemoryError("a memory needs text")
    if len(text) > MAX_TEXT:
        raise MemoryError(f"the memory is {len(text)} characters; the ceiling is {MAX_TEXT} — "
                          "this store is for what a later session must know, not for transcripts")
    now = time.time()
    db.execute("INSERT INTO memory (text, tags, source, created_at) VALUES (?,?,?,?)",
               (text, (tags or "").strip(), (source or "").strip(), now))
    rows = db.query("SELECT id, text, tags, source, created_at FROM memory "
                    "WHERE id = last_insert_rowid()")
    row = dict(rows[0]) if rows else {"text": text, "tags": tags, "source": source,
                                      "created_at": now}
    db.event("portal", "memory-add", f"id={row.get('id')} tags={row.get('tags') or '-'} "
                                     f"chars={len(text)} source={row.get('source') or '-'}")
    return row


def search(query: str, limit: int = 10) -> list[dict]:
    """Recall by words. FTS5 bm25 first; the LIKE net catches what FTS syntax refuses
    *and what it silently answers with nothing* — `min-p` parses as `min NOT p`, a
    term list an agent writes every day, and an empty answer is indistinguishable
    from an absent memory. The caller is mid-task; both failures are the same one."""
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit or 10), 100))
    try:
        rows = db.query(
            "SELECT m.id, m.text, m.tags, m.source, m.created_at, "
            "       bm25(memory_fts) AS rank "
            "FROM memory_fts JOIN memory m ON m.id = memory_fts.rowid "
            "WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit))
        if rows:
            return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001 — FTS syntax is an agent's, not ours
        pass
    # The LIKE net: plain words, quote noise stripped — but the hyphen stays, because
    # the words agents write are full of it: `models-max`, `min-p`, `IQ3_S`.
    terms = [t for t in (re.sub(r"[^\w-]", "", t) for t in query.split()) if t]
    if not terms:
        return []
    # Recall, not a conjunctive query: rows matching ANY term qualify, ranked by how
    # many they match. AND-only answers the question "is this exactly right"; an agent
    # mid-task is asking "does anything here help me", and FTS bm25 already ranked the
    # rows that parsed — this net catches the rest.
    score = " + ".join(["(CASE WHEN text LIKE ? OR tags LIKE ? THEN 1 ELSE 0 END)"] * len(terms))
    where = " OR ".join(["(text LIKE ? OR tags LIKE ?)"] * len(terms))
    params: list = []
    for t in terms:
        params += [f"%{t}%", f"%{t}%"]
    for t in terms:
        params += [f"%{t}%", f"%{t}%"]
    params.append(limit)
    rows = db.query(f"SELECT id, text, tags, source, created_at, {score} AS score "
                    f"FROM memory WHERE {where} "
                    f"ORDER BY score DESC, created_at DESC LIMIT ?", tuple(params))
    return [dict(r) for r in rows]


def recent(limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit or 20), 100))
    rows = db.query("SELECT id, text, tags, source, created_at FROM memory "
                    "ORDER BY created_at DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def remove(mem_id: int) -> bool:
    """Forget one memory. The FTS row goes with it (the delete trigger)."""
    rows = db.query("SELECT id, text FROM memory WHERE id = ?", (int(mem_id),))
    if not rows:
        return False
    db.execute("DELETE FROM memory WHERE id = ?", (int(mem_id),))
    db.event("portal", "memory-remove", f"id={mem_id} chars={len(rows[0]['text'])}")
    return True


def count() -> int:
    rows = db.query("SELECT COUNT(*) AS n FROM memory")
    return int(rows[0]["n"]) if rows else 0
