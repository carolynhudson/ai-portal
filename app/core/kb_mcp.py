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

from app.core import db, kb, memory  # noqa: E402

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
        "description": "Recall recorded memories by words (ranked). FTS syntax works; plain words work.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "description": "max memories (default 10)"}},
            "required": ["query"]},
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


def call_tool(name: str, args: dict) -> dict:
    """One tool call, portal-side. Raises on unknown names; tool refusals return isError."""
    if name == "kb_search":
        rows = kb.search(str(args.get("query") or ""), limit=int(args.get("limit") or 8))
        return _result([{"ref": r.get("ref"), "title": r.get("title"),
                         "excerpt": r.get("excerpt")} for r in rows])
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
        return _result(row)
    if name == "memory_search":
        return _result(memory.search(str(args.get("query") or ""),
                                     limit=int(args.get("limit") or 10)))
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
