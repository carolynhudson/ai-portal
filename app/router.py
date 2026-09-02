"""Live status of the llama router (``ai-llama-router``).

``snapshot()`` feeds the dashboard: it checks the systemd unit, probes the
router's web UI and its OpenAI-compatible ``/v1/models`` endpoint, and parses
the recent journal for the loaded model, tokens/s readings, and log events.
Results are cached for ``_TTL`` seconds so refreshes stay cheap.
"""
from __future__ import annotations

import re
import time

from .core import db, paths, systemd

_PROBE_TIMEOUT = 3.0
_TTL = 2.0
_JOURNAL_LINES = 1500

# b10641-era logs print per-request speed as "tg = 12.34 t/s"; older builds
# used "N tokens/s", kept as a fallback for pinned releases.
_TPS_RE = re.compile(r"tg\s*=\s*(\d+(?:\.\d+)?)\s*t/s")
_TPS_LEGACY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*tokens/s")
_MODEL_RE = re.compile(r"loading model\s+'?([^'\s]+)'?")
# The router logs one "proxy_request" line per forwarded request, which
# includes our own /metrics poll — drop those so they don't bury real events.
_PROXY_RE = re.compile(r"proxying request to model")

_cache: tuple[float, dict] = (0.0, {})


def _probe(url: str) -> dict:
    """Plain HTTP probe of the router web UI (status + latency only)."""
    import httpx

    t0 = time.time()
    try:
        r = httpx.get(url, timeout=_PROBE_TIMEOUT)
        return {"ok": r.status_code < 500, "status": r.status_code,
                "ms": int((time.time() - t0) * 1000), "url": url}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": None, "ms": int((time.time() - t0) * 1000),
                "url": url, "error": str(e)}


def _api() -> dict:
    import httpx

    url = f"http://{paths.ROUTER_HOST}:{paths.ROUTER_PORT}/v1/models"
    t0 = time.time()
    try:
        r = httpx.get(url, timeout=_PROBE_TIMEOUT)
        ms = int((time.time() - t0) * 1000)
        data = r.json().get("data", [])
        ids = [m.get("id") for m in data if m.get("id")]
        loaded = next((m.get("id") for m in data
                       if m.get("id") and (m.get("status") or {}).get("value") == "loaded"),
                      None)
        return {"ok": True, "status": r.status_code, "ms": ms, "url": url,
                "count": len(ids), "ids": ids, "loaded": loaded}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": None, "ms": int((time.time() - t0) * 1000),
                "url": url, "count": 0, "ids": [], "loaded": None, "error": str(e)}


def _fetch_metrics(model_id: str | None) -> dict:
    """Parse the router's Prometheus ``/metrics`` into ``{name: value}``.

    Router mode requires ``?model=<id>`` (bare /metrics answers 400
    "model name is missing"), so metrics are only fetched for a loaded model.
    Best-effort: returns ``{}`` if the router lacks --metrics, is unreachable,
    or nothing is loaded. Metric base names are mapped to their last value.
    """
    import httpx

    if not model_id:
        return {}
    url = f"http://{paths.ROUTER_HOST}:{paths.ROUTER_PORT}/metrics"
    try:
        r = httpx.get(url, params={"model": model_id}, timeout=3.0)
        r.raise_for_status()
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    for line in r.text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        name, val = parts
        try:
            out[name.split("{")[0]] = float(val)
        except ValueError:
            continue
    return out


def _token_totals(prom: dict) -> dict:
    """Cumulative prompt (in) / generated (out) tokens from the metric names.

    llama-server's exact counter names vary a little by version, so this
    matches on substrings rather than hard-coding one spelling.
    """
    def find(*need) -> float | None:
        for key, v in prom.items():
            if all(p in key for p in need):
                return v
        return None

    prompt = find("prompt", "token", "total")
    generated = None
    for key, v in prom.items():
        if "token" in key and "total" in key and "prompt" not in key:
            generated = v
            break
    return {"prompt_tokens": prompt, "generated_tokens": generated}


def _classify(line: str) -> str:
    low = line.lower()
    if any(k in low for k in ("error", "fatal", "panic", "traceback", "segfault")):
        return "error"
    if "tokens/s" in line or " t/s" in line:
        return "tps"
    if "model" in low and ("load" in low or "unload" in low):
        return "model"
    if "http request" in low or "slot" in low:
        return "request"
    return "log"


def _parse(lines: list[str]) -> dict:
    tps: list[float] = []
    loaded: str | None = None
    events: list[dict] = []
    for ln in lines:
        if _PROXY_RE.search(ln):
            continue
        for rx in (_TPS_RE, _TPS_LEGACY_RE):
            for m in rx.finditer(ln):
                tps.append(float(m.group(1)))
        mm = _MODEL_RE.search(ln)
        if mm:
            loaded = mm.group(1)
        events.append({"kind": _classify(ln), "text": ln.rstrip()})
    return {
        "events": events[-80:],
        "tps_recent": tps[-90:],
        "tps_latest": tps[-1] if tps else None,
        "loaded": loaded,
    }


def _build() -> dict:
    base = f"http://{paths.ROUTER_HOST}:{paths.ROUTER_PORT}"
    svc = systemd.unit_state(paths.ROUTER_UNIT)
    parsed = _parse(systemd.journal_tail(paths.ROUTER_UNIT, _JOURNAL_LINES))
    api = _api()
    prom = _fetch_metrics(api.get("loaded")) if db.get_setting("router_metrics", True) else {}
    return {
        "host": base,
        "loopback_only": paths.ROUTER_HOST in ("127.0.0.1", "localhost"),
        "service": {"name": paths.ROUTER_UNIT, "state": svc.get("ActiveState", "unknown"),
                    "sub": svc.get("SubState"), "since": svc.get("Since")},
        "web": _probe(base + "/"),
        "api": api,
        "loaded": api.get("loaded") or parsed["loaded"],
        "tps": {"latest": parsed["tps_latest"], "recent": parsed["tps_recent"]},
        "totals": dict(_token_totals(prom), enabled=bool(prom)),
        "activity": parsed["events"],
    }


def snapshot() -> dict:
    """A fresh (or <2s-old cached) probe of the router, for the dashboard."""
    global _cache
    now = time.time()
    if now - _cache[0] >= _TTL:
        try:
            _cache = (now, _build())
        except Exception as e:  # noqa: BLE001
            _cache = (now, {"host": f"http://{paths.ROUTER_HOST}:{paths.ROUTER_PORT}",
                            "loopback_only": True, "service": {"state": "unknown"},
                            "web": {"ok": False},
                            "api": {"ok": False, "count": 0, "ids": [], "loaded": None},
                            "loaded": None, "tps": {"latest": None, "recent": []},
                            "activity": [], "error": str(e)})
    return _cache[1]
