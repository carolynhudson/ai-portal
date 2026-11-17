"""mesh_lease client: the portal as a placement ACTOR (owner GO 2026-11-17).

The goal: small models offload to VRAM (``-ngl 99``) on remote mesh nodes,
paid for through the node's OWN lease ledger -- admission first, GPU after.
The laws this module is built to:

* the LEDGER GATES ADMISSION: ``mesh_lease`` must grant before
  ``mesh_router_offload`` applies; meshd refuses an apply with no live
  lease (the lease id is the ticket; an expired lease cannot buy a
  renewal -- re-grant explicitly);
* forge presets stay ``-ngl 0``: the offload is a per-placement decision
  applied on the node (meshd -> forge-mcp patch + warm), reverted on
  release; GPU weights free at the router's own idle-sleep / model
  switch -- we never unload anything by force (owner migration lane);
* the confirm-gate is ROUTE-side and two-phase (gate -> explicit apply);
  this module's verbs run only after that second click;
* halo is NOT a target: halo's router is the portal's own migration
  lane (loopback doors are refused). The mesh client is for remote
  nodes; the local half of model control stays on the portal's pages.

State (portal-held offloads) lives at ``$MESH_DIR/offloads.json`` --
small, readable, honest about what this portal has asked for. No timers:
leases TTL-expire on the node; a tracked offload whose lease died says
so on the page (renew = re-offload, by law).
"""
from __future__ import annotations

import json
import os
import pathlib
import time
import uuid

from . import meshplace, meshstatus

OFFLOAD_NGL = 99          # the owner's offload degree (99 = full)
DEFAULT_TTL_S = 14400     # 4 h; renew is a fresh, admitted offload
DEFAULT_CTX = 8192
_MODEL_RE = __import__("re").compile(r"^[A-Za-z0-9_\-]+$")

# the two-phase confirm gate (route-side): a gate answer registers the
# intended effect; ONLY the matching nonce's apply button runs a verb.
# Memory-only, short TTL: a portal restart drops pending gates (safe way).
GATE_TTL_S = 900
_gated: dict[str, dict] = {}


def gate(node: str, model: str, op: str, vram_mib: int, ctx: int) -> str:
    now = time.time()
    for k in [k for k, v in _gated.items() if now - v.get("at", 0) > GATE_TTL_S]:
        _gated.pop(k, None)
    nonce = uuid.uuid4().hex[:12]
    _gated[nonce] = {"node": node, "model": model, "op": op,
                     "vram_mib": vram_mib, "ctx": ctx, "at": now}
    return nonce


def take(nonce: str) -> dict | None:
    """Pop the gated intent; None if unknown/expired (a stale tab cannot
    fire a verb -- the page re-renders and the operator re-gates)."""
    v = _gated.pop(nonce, None)
    if not v:
        return None
    if time.time() - v.get("at", 0) > GATE_TTL_S:
        return None
    return v


def _state_file() -> pathlib.Path:
    env = (os.environ.get("MESH_DIR") or "").strip()
    base = pathlib.Path(env).expanduser() if env else pathlib.Path.home() / ".mesh"
    return base / "offloads.json"


def load() -> list[dict]:
    try:
        d = json.loads(_state_file().read_text())
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _save(rows: list[dict]) -> None:
    p = _state_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, indent=1))


def _record(node: str, model: str, lease: dict) -> None:
    rows = [r for r in load() if not (r.get("node") == node and r.get("model") == model)]
    rows.append({"id": uuid.uuid4().hex[:8], "node": node, "model": model,
                 "lease_id": lease.get("id"), "vram_mib": lease.get("vram_mib", 0),
                 "ttl_s": lease.get("ttl_s", 0), "granted_unix": lease.get("granted_unix"),
                 "expires_unix": lease.get("expires_unix"), "at": time.time()})
    _save(rows)


def _unrecord(node: str, model: str) -> None:
    _save([r for r in load() if not (r.get("node") == node and r.get("model") == model)])


def tracked() -> list[dict]:
    """Portal-held offloads with lease truth computed (expired says so)."""
    now = time.time()
    out = []
    for r in load():
        r = dict(r)
        exp = r.get("expires_unix") or 0
        r["lease_expired"] = bool(exp and now > exp)
        r["hours_left"] = round((exp - now) / 3600, 1) if exp else 0.0
        out.append(r)
    return out


def _door_for(node: str) -> tuple[str, str]:
    """(door_url, one_token) for a node NAME, refusing local doors.
    The door list is meshplace's; discovery is node_caps per door."""
    tok, _src = meshstatus._token()
    if not tok:
        raise RuntimeError("no ONE-token (hub and file both empty) - the mesh doors are closed")
    hits = []
    for url in meshplace._doors():
        host = url.split("//", 1)[-1].split(":", 1)[0].split("/")[0]
        if host in ("127.0.0.1", "localhost", "::1"):
            continue          # halo lane: the portal's own router = owner migration lane
        try:
            caps = meshplace.mcp_tool(url, "node_caps", {}, tok)
            if (caps.get("node") or "") == node:
                hits.append(url)
        except Exception:  # noqa: BLE001 -- a down door is simply not the node
            continue
    if not hits:
        raise RuntimeError(f"no remote meshd door answers as node '{node}' "
                           "(loopback doors are never offload targets)")
    return hits[0], tok


def offload(node: str, model: str, vram_mib: int, ctx: int = DEFAULT_CTX,
            ttl_s: int = DEFAULT_TTL_S, job: str = "") -> dict:
    """Admission-then-apply. mesh_lease grants (measured headroom, minus
    committed); ONLY then mesh_router_offload applies -ngl 99 + warm on
    the node. Any failure releases the fresh lease (we never strand
    admission). Returns a flat summary for the page."""
    if not _MODEL_RE.match(model or ""):
        return {"ok": False, "state": "refused", "reason": "model id required ([A-Za-z0-9_-])"}
    vram = max(0, int(vram_mib))
    if vram <= 0:
        return {"ok": False, "state": "refused", "reason": "lease needs vram_mib > 0"}
    ctx = max(512, min(262144, int(ctx)))
    ttl = max(300, min(86400, int(ttl_s)))
    url, tok = _door_for(node)
    lease = meshplace.mcp_tool(url, "mesh_lease", {
        "vram_mib": vram, "ram_mib": 0, "ttl_s": ttl,
        "job": (job or f"aipp-offload:{model}")[:80]}, tok)
    if lease.get("decision") != "granted":
        return {"ok": False, "state": "denied", "node": node, "model": model,
                "reason": lease.get("reason", "lease denied"),
                "free_vram_mib": lease.get("free_vram_mib")}
    lid = (lease.get("lease") or {}).get("id", "")
    try:
        res = meshplace.mcp_tool(url, "mesh_router_offload", {
            "model": model, "lease_id": lid, "ngl": OFFLOAD_NGL, "ctx": ctx,
            "action": "apply"}, tok)
    except Exception as e:  # noqa: BLE001 -- apply unreachable: un-admit
        _release_lease(url, tok, lid)
        return {"ok": False, "state": "apply-failed", "node": node, "model": model,
                "reason": f"{type(e).__name__}: {e}"[:160], "lease_released": True}
    forge = res.get("forge") or {}
    ok = bool(forge.get("ok"))
    if not ok:
        _release_lease(url, tok, lid)
    else:
        _record(node, model, lease.get("lease") or {})
    return {"ok": ok, "state": "offloaded" if ok else "apply-failed",
            "node": node, "model": model, "ngl": OFFLOAD_NGL, "ctx": ctx,
            "lease_id": lid, "vram_mib": vram, "ttl_s": ttl,
            "lease_released": not ok,
            "warm_ms": forge.get("warm_ms"), "reason": forge.get("reason")
            or forge.get("error") or ""}


def _release_lease(url: str, tok: str, lid: str) -> dict:
    try:
        return meshplace.mcp_tool(url, "lease_release", {"id": lid}, tok)
    except Exception:  # noqa: BLE001 -- TTL cleans up; the page says so
        return {"decision": "release-unreachable"}


def release(node: str, model: str) -> dict:
    """Revert -ngl on the node, then release the tracked lease. Order
    matters: preset law first (the box must not be left mid-offload by a
    dead door). GPU weights free at the router's idle-sleep / model
    switch -- no forced unload, owner law."""
    rows = [r for r in load() if r.get("node") == node and r.get("model") == model]
    url, tok = _door_for(node)
    res = meshplace.mcp_tool(url, "mesh_router_offload", {
        "model": model, "lease_id": (rows[0].get("lease_id") if rows else ""),
        "action": "revert"}, tok)
    forge = res.get("forge") or {}
    ok = bool(forge.get("ok"))
    lr = {}
    if rows and rows[0].get("lease_id"):
        lr = _release_lease(url, tok, rows[0]["lease_id"])
    _unrecord(node, model)
    return {"ok": ok, "state": "reverted" if ok else "revert-failed",
            "node": node, "model": model,
            "reason": forge.get("reason") or forge.get("error") or "",
            "lease": lr.get("decision", "-"),
            "note": forge.get("note", "GPU weights release at idle-sleep (900 s) "
                    "or next model switch")}
