#!/usr/bin/env python3
"""The portal's memory MCP server — stdio, stdlib, no dependencies.

    python app/core/kb_mcp.py          # speaks MCP over stdin/stdout

The owner's ask, verbatim: "RAG-style memory system for opencode, via plugin/MCP if
no other option." This is the MCP half, and it is deliberately the smallest thing
that remembers:

  * `kb_search` / `kb_read` — the portal's own documentation base (app/core/kb.py),
    lexical, no model, answers while the router runs anything. This is the RAG
    reading side: a session asks, the portal's docs answer with section refs.
  * `memory_add` / `memory_search` / `memory_recent` / `memory_remove` — the durable
    store in the portal's sqlite (app/core/memory.py), FTS5 recall, audited in the
    portal's own event log. This is what a session leaves for the next one.

Why stdio and stdlib: the harness launches it as `{type: "local", command: [...]}`
(a child of the opencode host, at the session's location), and that child must not
drag in a dependency tree — the harness restarts often, and a memory server that
fails to start is a quiet amnesia. One file, standard library, JSON-RPC 2.0 over
newline-delimited JSON (the MCP stdio framing).

The server writes nothing to stdout except protocol messages; diagnostics go to
stderr, where the host's own log keeps them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Run as a plain script (the harness command is `python <this file>`), so the portal
# package must be importable from here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import db, embeddings, kb, memory  # noqa: E402

PROTOCOL_FALLBACK = "2025-03-26"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

TOOLS = [
    {
        "name": "kb_search",
        "description": "Search the portal's knowledge base (its curated docs) by words. "
                       "Returns section refs, titles, and excerpts — cite the ref, do not paraphrase.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "the words to search for"},
            "limit": {"type": "integer", "description": "max sections (default 8, ceiling 30)"}},
            "required": ["query"]},
    },
    {
        "name": "kb_read",
        "description": "Read one knowledge-base section in full by its ref (from kb_search results).",
        "inputSchema": {"type": "object", "properties": {
            "ref": {"type": "string", "description": "the section ref, e.g. 'guide.md#some-section'"}},
            "required": ["ref"]},
    },
    {
        "name": "memory_add",
        "description": "Record one durable fact a later session must know (decisions, constraints, "
                       "measured values). Not a transcript: one fact, under 8000 characters.",
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "the fact, stated so a stranger can use it"},
            "tags": {"type": "string", "description": "space-separated tags, e.g. 'router presets'"},
            "source": {"type": "string", "description": "where it came from (session, doc, measurement)"}},
            "required": ["text"]},
    },
    {
        "name": "memory_search",
        "description": "Recall recorded memories by words (hybrid: FTS bm25 + embedding cosine when "
                       "the embedding sidecar is live; FTS-only when it is not). FTS syntax works; plain words work.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "description": "max memories (default 10)"}},
            "required": ["query"]},
    },
    {
        "name": "memory_reindex",
        "description": "Vectorize memories that have no embedding yet (queued when the embedding "
                       "sidecar was down). Needs the sidecar; a no-op answering 0 when it is not live.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "memory_recent",
        "description": "The most recently recorded memories, newest first.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "max memories (default 20)"}},
            "required": []},
    },
    {
        "name": "memory_remove",
        "description": "Forget one memory by id, when it is proven wrong. Say why in the audit trail: "
                       "give the reason as the id's argument context in your reply, not here.",
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "integer"}}, "required": ["id"]},
    },
]


def _result(payload) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


def _embed_row(row: dict) -> None:
    """Attach a vector to a stored row. Failure is quiet on purpose: a sidecar that
    died between the probe and the call must not fail the memory write — the row
    simply stays in the memory_reindex queue."""
    try:
        vecs = embeddings.embed_documents([row["text"]])
        memory.set_embedding(row["id"], vecs[0])
    except Exception:  # noqa: BLE001 — recall degrades to FTS, writes never degrade
        pass


def _hybrid_search(query: str, limit: int) -> list[dict]:
    """FTS bm25 and embedding cosine, one ranked list. Two signals, one honest blend:
    0.6·cosine + 0.4·(text position score) — cosine is the stronger signal, the text
    rank keeps exact tokens (ids, flags, hyphenated names) from falling out when the
    vector likes a near-synonym better. The whole vector half is wrapped: an embedder
    hiccup answers with the FTS list, never with an error."""
    text_hits = memory.search(query, limit=limit * 2)
    if not embeddings.live():
        return text_hits[:limit]
    try:
        qv = embeddings.embed_query(query)
        scored = sorted(((embeddings.cosine(qv, r["vector"]), r)
                         for r in memory.embedded_rows()),
                        key=lambda t: t[0], reverse=True)
    except Exception:  # noqa: BLE001 — FTS-only is a state, not a failure
        return text_hits[:limit]
    v_scores = {r["id"]: cos for cos, r in scored[:limit * 2]}
    t_scores = {r["id"]: 1.0 / (pos + 2) for pos, r in enumerate(text_hits)}
    rows_by_id = {r["id"]: r for _, r in scored}
    rows_by_id.update({r["id"]: r for r in text_hits})
    merged = sorted(rows_by_id,
                    key=lambda mid: 0.6 * v_scores.get(mid, 0.0) + 0.4 * t_scores.get(mid, 0.0),
                    reverse=True)
    out = []
    for mid in merged[:limit]:
        r = dict(rows_by_id[mid])
        r.pop("vector", None)
        out.append(r)
    return out


def call_tool(name: str, args: dict) -> dict:
    """One tool call, portal-side. Raises on unknown names; tool refusals return isError."""
    if name == "kb_search":
        rows = kb.search(str(args.get("query") or ""), limit=int(args.get("limit") or 8))
        # The address a section is cited by is its `id` (kb.find accepts it). The
        # row has no "ref" key: mapping one here used to hand every agent a
        # citation of null while the tool text said "cite the ref" - found by
        # the mesh door's acceptance test, 2026-10-30.
        return _result([{"ref": r.get("id"), "title": r.get("title"),
                         "doc": r.get("doc"), "excerpt": r.get("excerpt")} for r in rows])
    if name == "kb_read":
        sec = kb.find(str(args.get("ref") or ""))
        if sec is None:
            return _result({"error": f"no knowledge-base section matches {args.get('ref')!r}; "
                                     "use kb_search first"})
        return _result({"ref": sec.get("ref"), "title": sec.get("title"),
                        "body": sec.get("body", "")})
    if name == "memory_add":
        try:
            row = memory.add(str(args.get("text") or ""), tags=str(args.get("tags") or ""),
                             source=str(args.get("source") or "mcp"))
        except memory.MemoryError as e:
            return _result({"error": str(e)})
        if embeddings.live():
            _embed_row(row)
        return _result(row)
    if name == "memory_search":
        return _result(_hybrid_search(str(args.get("query") or ""),
                                      int(args.get("limit") or 10)))
    if name == "memory_reindex":
        rows = memory.missing_embedding()
        if not rows:
            return _result({"embedded": 0, "pending": 0})
        if not embeddings.live():
            return _result({"embedded": 0, "pending": len(rows),
                            "note": "the embedding sidecar is not live; start it and rerun"})
        vecs = embeddings.embed_documents([r["text"] for r in rows])
        for r, v in zip(rows, vecs):
            memory.set_embedding(r["id"], v)
        return _result({"embedded": len(rows), "pending": 0})
    if name == "memory_recent":
        return _result(memory.recent(int(args.get("limit") or 20)))
    if name == "memory_remove":
        return _result({"removed": memory.remove(int(args.get("id") or 0))})
    raise LookupError(f"unknown tool: {name}")


def handle(msg: dict) -> dict | None:
    """One JSON-RPC message in, one response out (None for notifications)."""
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        want = str(params.get("protocolVersion") or "")
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": want if want in SUPPORTED_PROTOCOLS else PROTOCOL_FALLBACK,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "portal-memory", "version": "1"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        try:
            return {"jsonrpc": "2.0", "id": mid,
                    "result": call_tool(str(params.get("name") or ""),
                                        params.get("arguments") or {})}
        except LookupError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": str(e)}}
        except Exception as e:  # noqa: BLE001 — a tool failure is a tool result, not a death
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"isError": True, "content": [
                        {"type": "text", "text": f"{type(e).__name__}: {e}"}]}}
    if mid is None:  # notifications/initialized and friends: no response
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> int:
    db.init()          # schema, idempotent — the server and the portal share one base
    print("portal-memory MCP serving on stdio", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"dropped non-JSON line: {e}", file=sys.stderr, flush=True)
            continue
        try:
            out = handle(msg)
        except Exception as e:  # noqa: BLE001 — the loop survives everything
            print(f"handler error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            continue
        if out is not None:
            sys.stdout.write(json.dumps(out, default=str) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
