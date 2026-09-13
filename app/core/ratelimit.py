"""In-process throttle for the login endpoints.

The portal binds to the LAN by design (decision D2: app password, no IdP), so
anything on the local network — or anything that has compromised a box on it —
can hammer ``/login``. The password is scrypt (expensive server-side, which
caps the guess rate by itself) but a cheap per-source counter is still the
right hygiene: it turns a credential-stuffing run into a 429.

Deliberately in-process and in-memory:
  * single process (uvicorn), so there is nothing to coordinate;
  * a restart clearing the counters is acceptable — it also costs the attacker
    nothing;
  * no PII retention beyond a bounded dict of source → failure timestamps.

Keyed on the TCP peer address. If the portal is ever put behind a reverse
proxy, the proxy address must be added to :data:`app.core.network.trusted_proxies`
and the forwarded client address used instead — otherwise every login looks
like it comes from the proxy and one abusive client locks everyone out.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_failures: dict[str, list[float]] = {}

# Defaults; the Settings page can tune them (kept module-level so tests can
# shrink the window without sleeping).
WINDOW_S = 300        # failures older than this are forgotten
MAX_FAILURES = 10     # allowed per WINDOW_S per source
SWEEP_EVERY = 100     # housekeeping: prune stale sources this often
_ops = 0


def _sweep(now: float) -> None:
    global _ops
    _ops += 1
    if _ops % SWEEP_EVERY:
        return
    for k in [k for k, v in _failures.items() if not v or now - v[-1] > WINDOW_S]:
        _failures.pop(k, None)


def client_key(request) -> str:
    """Stable key for the caller: TCP peer address (defensive about None)."""
    try:
        return request.client.host if request.client else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def allow(key: str) -> tuple[bool, int]:
    """May ``key`` attempt a login now? Returns (allowed, retry_after_s)."""
    now = time.time()
    with _lock:
        _sweep(now)
        hits = [t for t in _failures.get(key, []) if now - t < WINDOW_S]
        _failures[key] = hits
        if len(hits) >= MAX_FAILURES:
            return False, int(WINDOW_S - (now - hits[0])) + 1
        return True, 0


def failure(key: str) -> int:
    """Record a bad attempt; returns how many attempts remain in the window."""
    now = time.time()
    with _lock:
        hits = [t for t in _failures.get(key, []) if now - t < WINDOW_S]
        hits.append(now)
        _failures[key] = hits
        return max(0, MAX_FAILURES - len(hits))


def success(key: str) -> None:
    """A good login clears that source's counter."""
    with _lock:
        _failures.pop(key, None)


def reset() -> None:
    """Test helper."""
    with _lock:
        _failures.clear()
