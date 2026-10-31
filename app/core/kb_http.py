"""The mesh's door: the portal's memory MCP, served over HTTP (phase P0).

``kb_mcp.py`` is stdio - a child of this box's harness, deliberately
dependency-free, and unreachable from another machine. The Windows box proved
the cost of that: it paid four install runs re-deriving PowerShell lessons
this KB recorded the day they were learned.

This module is transport plus policy, nothing else:

* **transport** — one JSON-RPC message per POST, answered by
  ``kb_mcp.handle`` (the same protocol handler the stdio server uses, so the
  two doors can never drift on semantics);
* **policy** — the scopes an identity holds (``app/core/agents.py``) decide
  which tools it can see and call. ``tools/list`` is filtered, so a read
  agent is not teased by a write tool it cannot use; and a scope refusal is
  a tool result that names the scope, not a transport error.

Two identities reach the door: an issued **agent token**
(``Authorization: Bearer <token>``, scoped) and a **portal session** (the
browser's own cookie, full scope, including the destructive tools no issued
token carries). Cookie POSTs resist cross-site forgery because the session
cookie is ``SameSite=lax``.

The audit records the identity, the tool, and the size of the arguments -
never their text. The KB is a place where secrets must not be written; the
audit is not a place where they get copied.
"""
from __future__ import annotations

import json

from fastapi.responses import JSONResponse

from . import agents, config as cfg, db, kb_mcp

# Scope -> tools. memory_remove and memory_reindex are portal-session only:
# a destructive tool on a token that can leak off-box is not a trade worth
# making, and reindexing is maintenance the portal schedules for itself.
READ_TOOLS = ("kb_search", "kb_read", "memory_search", "memory_recent")
WRITE_TOOLS = READ_TOOLS + ("memory_add",)
PORTAL_TOOLS = tuple(t["name"] for t in kb_mcp.TOOLS)

_settings_cache = None


def _settings():
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = cfg.load()
    return _settings_cache


def _identity(request):
    """(actor, allowed tools) for a request, or None when nothing authenticates."""
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        row = agents.verify(header[7:].strip())
        if row is None:
            return None
        allowed: list[str] = []
        if "read" in row["scopes"]:
            allowed += list(READ_TOOLS)
        if "write" in row["scopes"]:
            allowed += list(WRITE_TOOLS)
        return f"agent:{row['name']}", tuple(dict.fromkeys(allowed))
    token = request.cookies.get(cfg.COOKIE_NAME)
    if token and cfg.token_valid(_settings(), token):
        return "portal", PORTAL_TOOLS
    return None


def handle_request(request, raw: bytes) -> JSONResponse:
    """One HTTP request, one JSON answer. A plain function of (request, body
    bytes) so the route, the tests, and any future mount share one path."""
    if request.method == "GET":
        return JSONResponse({"error": "this is an MCP door: POST JSON-RPC to /mcp. "
                                      "Agent tokens are issued on the portal's "
                                      "Settings page."}, status_code=405)

    identity = _identity(request)
    if identity is None:
        # The portal is LAN-reachable by the owner's own setup, so the token
        # is the whole gate. The refusal names what is missing; it validates
        # nothing about the caller.
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32001,
                       "message": "unauthenticated: send Authorization: "
                                  "Bearer <agent token>, issued on the portal's "
                                  "Settings page"}},
            status_code=401)

    actor, allowed = identity
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        body = None
    if not isinstance(body, dict):
        db.event(actor, "mcp.reject", "body not one JSON-RPC object")
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32600,
                                       "message": "expected one JSON-RPC object per POST"}})

    method = body.get("method")
    params = body.get("params") or {}
    args = params.get("arguments") or {}

    if method == "tools/list":
        out = kb_mcp.handle(body)
        out["result"]["tools"] = [t for t in out["result"]["tools"] if t["name"] in allowed]
        db.event(actor, "mcp.tools", f"visible={len(out['result']['tools'])}")
        return JSONResponse(out)

    if method == "tools/call":
        tool = str(params.get("name") or "")
        if tool not in allowed:
            db.event(actor, "mcp.denied", f"tool={tool} bytes={len(json.dumps(args))}")
            return JSONResponse(
                {"jsonrpc": "2.0", "id": body.get("id"),
                 "result": {"isError": True, "content": [{
                     "type": "text",
                     "text": f"{tool!r} is outside this identity's scope. "
                             f"Allowed here: {', '.join(allowed)}. The portal's own "
                             f"session holds the tools no token carries."}]}},
                headers={"X-Mesh-Scope": ",".join(allowed)})
        out = kb_mcp.handle(body)
        ok = 1 if not (out.get("result") or {}).get("isError") else 0
        db.event(actor, "mcp.call", f"tool={tool} ok={ok} bytes={len(json.dumps(args))}")
        return JSONResponse(out)

    out = kb_mcp.handle(body)
    if out is None:  # notifications/initialized and friends
        return JSONResponse({"ok": True}, status_code=202)
    db.event(actor, f"mcp.{method}", "")
    return JSONResponse(out)
