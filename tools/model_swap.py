#!/usr/bin/env python3
"""Swap which model the router has loaded — the safe way, with the agent sessions closed.

    .venv/bin/python tools/model_swap.py --to repo:QUANT [--verify] [--verbose]

Why this is a script and not a portal/agent action: this box runs its agent
sessions on the router's own model (measured 2026-10-16: the portal's harness
sessions answer from the same llama-server that a swap evicts). The router runs
`--models-max 1` on purpose — the owner's OOM history, the 108 GiB shared-RAM
aperture ceiling, and lazy-mode on the GSQ-RCO quants are the reason. A swap is
therefore an operation that must not be taken from inside a model connection:
this script refuses to run while anything is completing on the router.

What it does:

  1. READ the router (GET /models): current loaded set, and the target's row.
  2. REFUSE while any slot on the loaded child is busy (an in-flight completion
     belongs to someone — most likely to the very session that would be evicted).
  3. `POST /models/load {"model": <id>}` (the verb the server binary names;
     measured b11669: /models/load, /models/unload, /models/sse).
  4. Poll until the target reports `loaded` (the model's budget comes from
     presets.ini — the same file the portal generates, untouched here).
  5. With --verify: a 1-token chat completion against the target, the same
     contract /v1/chat/completions has to answer.
  6. Report, plainly, what the router now has loaded (with models-max 1 the
     router evicts the old model itself; this script never unloads).

It writes nothing. It reads only the router; presets.ini, the portal DB, and the
harness are all out of scope, by design (slot_bench.py's discipline).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

ROUTER = "http://127.0.0.1:8080"


def _get(path: str, timeout: float = 10.0):
    with urllib.request.urlopen(ROUTER + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post(path: str, body: dict, timeout: float = 60.0):
    req = urllib.request.Request(ROUTER + path, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def _loaded_rows(rows: list[dict]) -> list[dict]:
    return [m for m in rows if (m.get("status") or {}).get("value") == "loaded"]


def _children_busy(verbose: bool) -> tuple[bool, str]:
    """True when any slot on any loaded child is processing. The child port is in
    the router's status args (`--port <n>`); the child's /slots says busy/idle."""
    busy: list[str] = []
    for m in _loaded_rows(_get("/models").get("data", [])):
        args = (m.get("status") or {}).get("args") or []
        port = None
        for i, a in enumerate(args):
            if a == "--port" and i + 1 < len(args):
                port = args[i + 1]
        if not port:
            continue
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/slots", timeout=10) as r:
                slots = json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001 - census, not control
            if verbose:
                print(f"  (child {port} /slots unreadable: {e})")
            continue
        for s in slots if isinstance(slots, list) else []:
            state = s.get("state") or s.get("status")
            if state and str(state) not in ("idle", "waiting"):
                busy.append(f"{m['id']} slot {s.get('id')} is {state}")
    return (bool(busy), "; ".join(busy))


def main() -> int:
    ap = argparse.ArgumentParser(description="Swap the router's loaded model, quiescent-box only.")
    ap.add_argument("--to", required=True, help="model id to load (as the router names it)")
    ap.add_argument("--verify", action="store_true", help="one-token chat completion after load")
    ap.add_argument("--yes", action="store_true", help="skip the Enter confirmation (scripts only)")
    ap.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for the load")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    rows = _get("/models").get("data", [])
    target = next((m for m in rows if m.get("id") == args.to), None)
    if target is None:
        print(f"refused: the router does not list {args.to!r}. "
              "Its catalog is presets.ini's account — apply a personality that carries a "
              "config for this model first (the portal's Apply), then rerun.", file=sys.stderr)
        return 2

    busy, why = _children_busy(args.verbose)
    if busy:
        print(f"refused: {why}. Close the sessions that own these completions "
              "(the model being evicted is the model they are answering from), rerun.",
              file=sys.stderr)
        return 3

    now = [m["id"] for m in _loaded_rows(rows)]
    print(f"router loaded now: {now or '(nothing)'}")
    if args.to in now:
        print(f"{args.to} is already loaded; nothing to do.")
        return 0

    if not args.yes:
        # The /slots census is best-effort (children expose it only with --slots),
        # so the guard is the human: a swap evicts the model agent sessions on this
        # box are answering FROM. Say it, then require the Enter.
        print(f"about to load {args.to}. With --models-max 1 the router evicts "
              f"{now or '(nothing)'} — any agent session running on it loses its model "
              "connection. Close those sessions first.")
        if input("type Enter to proceed, anything else to abort: ").strip():
            print("aborted.")
            return 1

    code, resp = _post("/models/load", {"model": args.to})
    print(f"POST /models/load -> HTTP {code}: {json.dumps(resp)[:200]}")
    if code != 200:
        return 4

    deadline = time.time() + args.timeout
    state = None
    while time.time() < deadline:
        time.sleep(5)
        rows = _get("/models").get("data", [])
        row = next((m for m in rows if m.get("id") == args.to), None)
        state = row and (row.get("status") or {}).get("value")
        if args.verbose:
            print(f"  {state}")
        if state == "loaded":
            break
    if state != "loaded":
        print(f"the target did not reach 'loaded' (state: {state}); leaving the router alone. "
              "Check its log: systemctl --user status ai-llama-router", file=sys.stderr)
        return 5

    print(f"loaded: {args.to}  (router now: {[m['id'] for m in _loaded_rows(rows)]})")
    if args.verify:
        code, resp = _post("/v1/chat/completions", {
            "model": args.to, "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}]})
        ok = code == 200 and "choices" in resp
        print(f"verify /v1/chat/completions -> HTTP {code}: {'ok' if ok else json.dumps(resp)[:200]}")
        return 0 if ok else 6
    return 0


if __name__ == "__main__":
    sys.exit(main())
