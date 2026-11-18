"""mesh.place -- the read-only service-card aggregation (NODE_CONTRACT v0).

The forge's meshd 0.3.0 emits service cards (the unit the mesh places
work against); the portal is the consumer. This module asks each known
meshd door (MCP, ONE-token bearer, hub-first) for node_caps, and applies
the OWNER PLACEMENT POLICY (memory 210): the forge is a MINOR ASSISTANT
node until the GPU upgrade -- cuda-heavy cards on it are deprioritized
for heavy jobs; CPU/image-contract lanes stay neutral. The page shows
the policy as a named marker, never as a hidden bias.

Read-only by construction: no verbs, no leases granted here. Every door
degrades with its reason; the page renders with zero meshd present.
"""
from __future__ import annotations

import json
import os
import time

import httpx

from . import meshhub, meshstatus

_TTL = 60.0
_cache: tuple[float, dict] = (0.0, {})

# Owner policy (memory 210): keep as data, name it on the page.
_MINOR_ASSISTANT = {"rainbowblock", "forge"}


def _doors() -> list[str]:
    """Known meshd doors: the forge (port ledger derivation) plus any in
    AIPP_MESH_DOORS (comma list) -- the lane for future nodes."""
    out: list[str] = []
    extra = (os.environ.get("AIPP_MESH_DOORS") or "").strip()
    out += [u.strip().rstrip("/") for u in extra.split(",") if u.strip()]
    f = meshstatus._meshd_url()
    if f and f not in out:
        out.append(f)
    return out


def mcp_tool(url: str, tool: str, args: dict, tok: str) -> dict:
    """One MCP tools/call over StreamableHTTP (the flow proven against
    meshd): initialize -> initialized -> call; SSE-tolerant parse."""
    h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    with httpx.Client(base_url=url, timeout=12.0) as c:
        r = c.post("/mcp", headers=h, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "aipp-place", "version": "1"}}})
        r.raise_for_status()
        sid = r.headers.get("mcp-session-id")
        hh = dict(h)
        if sid:
            hh["mcp-session-id"] = sid
        c.post("/mcp", headers=hh,
               json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = c.post("/mcp", headers=hh, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": args}})
        r.raise_for_status()
        d = _parse(r)
        txt = ((d or {}).get("result") or {}).get("content", [{}])[0].get("text", "{}")
        return json.loads(txt)


def _parse(r: httpx.Response) -> dict:
    if "event-stream" in r.headers.get("content-type", ""):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
        return {}
    return r.json() if r.text.strip() else {}


def _pref(card: dict) -> str:
    """Placement preference under the owner policy -- visible, not hidden."""
    s = 2 if (card.get("state") == "idle") else 1
    req = card.get("requires") or {}
    if req.get("backend") == "cuda" and (card.get("node") or "") in _MINOR_ASSISTANT:
        s -= 1
    return "high" if s >= 2 else "med" if s == 1 else "low"


def _node(url: str, tok: str) -> dict:
    try:
        caps = mcp_tool(url, "node_caps", {}, tok)
        cards = caps.get("cards") or []
        for c in cards:
            c["pref"] = _pref(c)
        leases = {}
        try:  # a status failure must not kill the cards -- lanes degrade alone
            leases = (mcp_tool(url, "mesh_status", {}, tok) or {}).get("leases") or {}
        except Exception as e:  # noqa: BLE001 -- the lane names its own truth
            leases = {"error": f"{type(e).__name__}: {e}"[:120]}
        return {"ok": True, "url": url, "node": caps.get("node") or url,
                "router_url": caps.get("router_url") or "",
                "cards": cards, "leases": leases,
                "policy": "minor-assistant (owner, mem 210)"
                          if (caps.get("node") in _MINOR_ASSISTANT) else ""}
    except Exception as e:  # noqa: BLE001 -- doors degrade with reasons
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}"[:160]}


def bundle(force: bool = False) -> dict:
    global _cache
    if not force and time.time() - _cache[0] < _TTL and _cache[1]:
        return _cache[1]
    tok, src = meshstatus._token()
    nodes = [_node(u, tok) for u in _doors()] if tok else [
        {"ok": False, "url": "-", "error": "no ONE-token (hub and file both empty)"}]
    n_cards = sum(len(n.get("cards", [])) for n in nodes)
    leased = sum((n.get("leases") or {}).get("count", 0) for n in nodes if n.get("ok"))
    committed = sum((n.get("leases") or {}).get("committed_vram_mib", 0)
                    for n in nodes if n.get("ok"))
    out = {"at": time.time(), "token_src": src, "nodes": nodes,
           "cards": n_cards, "leased": leased, "committed_vram_mib": committed,
           "policy": "minor-assistant nodes: "
           + ", ".join(sorted(_MINOR_ASSISTANT))}
    _cache = (time.time(), out)
    return out
