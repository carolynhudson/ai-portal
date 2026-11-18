"""meshllm: the /llm gateway -- a READ-PATH placement proxy (P2, 2026-11-18).

A client (opencode, scripts) posts OpenAI-style requests here; the portal
resolves the model over the live mesh inventory (meshplace.bundle IS the
synchronized master list), ranks with the owner's rubric (meshmatch),
and forwards to the best router. Failover is honest: a peer is tried
ONLY before the first response byte reaches the client -- a half-stream
is never silently replayed (a replayed answer would lie).

LAWS:
  - READ PATH ONLY: chat requests, model lists. The gateway NEVER loads,
    unloads, or reconfigures a router -- that stays the owner's lane and
    the confirm-gated meshd verbs. Pointing opencode at the gateway
    creates no new mutation surface.
  - VISIBLE POLICY: X-AIPP-Place names who answered and why (the same
    law as /place's pref column); attempts list every peer tried.
  - Loopback doors never proxy: the portal's own meshd door is loopback
    by law (the actor lane's rule), and a LOOPBACK router_url on a REMOTE
    door is rewritten to the door's host (meshd reports its local view).
  - The gateway token is minted once at state/portal/llm_gateway_token.txt
    (0600), never logged, revocable by deleting the file. Sessions
    (the portal cookie) ride the normal auth lane.
  - The suite injects: bundle and sender are parameters; no test touches
    live doors.
"""
from __future__ import annotations

import json
import os
import secrets
from urllib.parse import urlparse, urlunparse

import httpx

from . import meshmatch, meshplace
from . import paths

TOK_FILE = "llm_gateway_token.txt"
_DEFAULT_CTX = 8192


def gateway_token() -> str:
    env = (os.environ.get("AIPP_LLM_GATEWAY_TOKEN") or "").strip()
    if env:
        return env
    p = str(paths.PORTAL_STATE / TOK_FILE)
    try:
        with open(p, "r", encoding="utf-8") as f:
            t = f.read().strip()
            if t:
                return t
    except FileNotFoundError:
        pass
    t = secrets.token_urlsafe(32)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(t + "\n")
    return t


def _lan_router(router_url: str, door_url: str) -> str:
    """meshd reports its LOCAL router view (loopback). From the portal's
    seat, a loopback router on a REMOTE door means that router on the
    door's host. Same-host loopback stays loopback (halo IS the portal)."""
    r, d = urlparse(router_url), urlparse(door_url)
    if r.hostname not in ("127.0.0.1", "localhost", "0.0.0.0", "::1"):
        return router_url
    if d.hostname in ("127.0.0.1", "localhost", "0.0.0.0", "::1"):
        return router_url
    return urlunparse((r.scheme, f"{d.hostname}:{r.port}", r.path, "", "", ""))


def resolve(body: dict, bundle: dict) -> tuple[dict, list, list]:
    """Rank every advertised router card for this request. Returns
    (request_used, ranked, filtered) with ranked rows carrying the
    proxy target: router_url (LAN-rewritten), node, service, why.
    model="auto" ranks by task/ctx/tags; an exact model name finds its
    copies on every node and prefers the loaded copy (stickiness)."""
    req: dict = {
        "model": body.get("model") or "",
        "ctx": int(body.get("ctx") or body.get("max_ctx") or _DEFAULT_CTX),
        "task": body.get("task") or "",
        "tags": body.get("tags") or [],
        "prefer_loaded": True,
    }
    # the failover/cliff/latency knobs ride through when the caller
    # (or failover_request) sets them - the rubric owns their meaning
    for k in ("min_params_ratio", "max_active_b", "params_b", "weights_bytes"):
        if body.get(k) is not None:
            req[k] = body[k]
    if req["model"] in ("", "auto", "mesh"):
        req["model"] = ""
    if body.get("params_b"):
        req["params_b"] = float(body["params_b"])
    cands = []
    for n in bundle.get("nodes") or []:
        if not n.get("ok"):
            continue
        ru = n.get("router_url")
        if not ru:
            continue
        llm_cards = [c for c in (n.get("cards") or []) if c.get("kind") == "llm-router"]
        for c in meshmatch.candidates_from_cards(llm_cards):
            c["router_url"] = _lan_router(ru, n.get("url") or "")
            cands.append(c)
    ranked, filtered = meshmatch.rank(req, cands)
    return req, ranked, filtered


def _send(router_url: str, service: str, body: dict, tok: str, timeout: float = 300.0):
    """One upstream call; returns the httpx Response (stream=True caller
    closes). The ONE-token rides along: guarded routers need it, open
    routers accept it."""
    b = dict(body)
    b["model"] = service
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    with httpx.Client(timeout=timeout) as c:
        r = c.post(f"{router_url}/v1/chat/completions", json=b, headers=h)
    return r


def chat(body: dict, bundle: dict, tok: str, send=None, max_tries: int = 3) -> dict:
    """Proxy one chat completion with pre-first-token failover. Returns
    {'ok', 'status', 'body', 'placed': {node, service, why}, 'attempts':
    [{node, service, why, result}]}."""
    send = send or _send
    _req, ranked, _filtered = resolve(body, bundle)
    attempts, last = [], None
    for r in ranked[:max_tries]:
        try:
            resp = send(r["router_url"], r["service"], body, tok)
        except Exception as e:  # noqa: BLE001 -- a down door is DATA
            attempts.append({"node": r["node"], "service": r["service"],
                             "why": r["why"], "result": f"{type(e).__name__}"})
            last = {"status": 0, "body": {"error": f"{type(e).__name__}"}}
            continue
        if resp.status_code < 400:
            attempts.append({"node": r["node"], "service": r["service"],
                             "why": r["why"], "result": resp.status_code})
            return {"ok": True, "status": resp.status_code, "body": resp.json(),
                    "placed": {"node": r["node"], "service": r["service"],
                               "why": r["why"]}, "attempts": attempts}
        attempts.append({"node": r["node"], "service": r["service"],
                         "why": r["why"], "result": resp.status_code})
        last = {"status": resp.status_code, "body": {"error": resp.text[:200]}}
    return {"ok": False, "status": (last or {}).get("status", 503),
            "body": (last or {}).get("body", {"error": "no candidates"}),
            "placed": None, "attempts": attempts}


def stream_chat(body: dict, bundle: dict, tok: str, max_tries: int = 3) -> tuple:
    """Streaming proxy: returns (status, generator, placed, attempts).
    Failover stays honest: a peer is abandoned only up to and including
    the first chunk; once bytes flow, the stream is committed (a
    half-stream is never silently replayed -- that would lie)."""
    _req, ranked, _filtered = resolve(body, bundle)
    attempts, last_status = [], 503
    for r in ranked[:max_tries]:
        b = dict(body)
        b["model"] = r["service"]
        b["stream"] = True
        h = {"Content-Type": "application/json"}
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        client = httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0))
        try:
            upstream = client.build_request("POST", f"{r['router_url']}/v1/chat/completions",
                                            json=b, headers=h)
            resp = client.send(upstream, stream=True)
        except Exception as e:  # noqa: BLE001 -- a down door is DATA
            client.close()
            attempts.append({"node": r["node"], "service": r["service"],
                             "why": r["why"], "result": type(e).__name__})
            last_status = 0
            continue
        if resp.status_code >= 400:
            attempts.append({"node": r["node"], "service": r["service"],
                             "why": r["why"], "result": resp.status_code})
            resp.close()
            client.close()
            last_status = resp.status_code
            continue
        it = resp.iter_bytes()
        try:
            first = next(it, b"")
        except Exception as e:  # noqa: BLE001
            attempts.append({"node": r["node"], "service": r["service"],
                             "why": r["why"], "result": f"stream {type(e).__name__}"})
            resp.close()
            client.close()
            last_status = 0
            continue

        def gen(first=first, it=it, resp=resp, client=client):
            yield first
            try:
                for chunk in it:
                    yield chunk
            finally:
                resp.close()
                client.close()

        attempts.append({"node": r["node"], "service": r["service"],
                         "why": r["why"], "result": "streaming"})
        return 200, gen, {"node": r["node"], "service": r["service"],
                          "why": r["why"]}, attempts
    return last_status, None, None, attempts


def failover_request(bundle: dict, model_id: str, **over) -> dict | None:
    """Build the failover request FROM the source card: its ctx IS the
    floor (transfer can fail outright mid-context), its params seed the
    size proxy with the cliff ratio (a far-smaller peer is a cliff, not
    a close fit), its tags carry the persona needs. Stated, derived -
    never guessed. Returns None when the mesh has no card for it."""
    g = meshmatch._norm(model_id)
    for n in bundle.get("nodes") or []:
        for c in n.get("cards") or []:
            if c.get("kind") != "llm-router" or meshmatch._norm(c.get("service") or "") != g:
                continue
            cands = meshmatch.candidates_from_cards([c])
            p = cands[0]["params"]
            eff = (p.get("active_b") if "moe" in cands[0]["tags"]
                   else p.get("params_b")) if p else None
            req = {"model": c.get("service"),
                   "ctx": cands[0].get("ctx") or 0,
                   "weights_bytes": cands[0]["weights_bytes"],
                   "tags": sorted(cands[0]["tags"]),
                   "prefer_loaded": True,
                   "min_params_ratio": 0.75}
            if eff:
                req["params_b"] = eff
            req.update(over)
            return req
    return None


def models(bundle: dict) -> list[dict]:
    """The mesh model list: every advertised service, deduped across
    nodes, with where it lives. 'auto' is always available."""
    out: dict[str, dict] = {"auto": {"id": "auto", "owned_by": "mesh-gateway",
                                     "nodes": [], "states": {}}}
    for n in bundle.get("nodes") or []:
        if not n.get("ok"):
            continue
        for c in n.get("cards") or []:
            if c.get("kind") != "llm-router":
                continue
            mid = c.get("service") or "?"
            row = out.setdefault(mid, {"id": mid, "owned_by": "mesh-gateway",
                                       "nodes": [], "states": {}})
            if n.get("node") not in row["nodes"]:
                row["nodes"].append(n.get("node"))
            row["states"][n.get("node")] = c.get("state")
    return list(out.values())
