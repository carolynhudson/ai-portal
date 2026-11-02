"""Which service verbs a unit's state can honestly offer. (Owner, 2026-11-02.)

The status hero currently shows start/stop/restart for every service, always.
The owner's rule states the lookup that the hero should use:

* **active / activating** - stop and restart (a start you cannot need);
* **inactive / failed / deactivating** - start only ("stopped or stopping
  only the start button is visible");
* **missing / disabled / masked** - nothing: there is no unit to verb;
* **unknown** - all three: when the state is not known, the page must not
  hide an action the operator may need. A machine-safety rule in UI form:
  absence of knowledge never becomes absence of control.

Pure and total: any string maps, unknown strings are `unknown`, and nothing
here imports systemd, FastAPI, or a template. The dashboard wiring is its own
round - the UI contract (spec fixtures, confirm-gating, exercise coverage)
moves with the buttons, and the owner's standing rule is to ship the tested
piece, not the measured-and-guessed one.
"""
from __future__ import annotations

# systemd ActiveState/UnitFileState words the portal's payloads carry, plus
# the sentinel states the portal synthesizes (unit file absent -> "missing").
ACTIVE_STATES = {"active", "activating", "auto-restart", "deactivating",
                 "inactive", "failed", "missing", "disabled", "masked",
                 "not-found", "error", "unknown"}

ALL_ACTIONS = ("start", "stop", "restart")


def actions_for(state: str) -> dict[str, bool]:
    """{start, stop, restart} visibility for one unit state.

    `deactivating` belongs with stopped (the owner's "stopping"): a unit on
    its way down is not a unit you restart. `failed` is start-only: a dead
    unit's restart is a start with extra steps.
    """
    s = (state or "").strip().lower()
    if s in ("active", "activating", "auto-restart", "running"):
        return {"start": False, "stop": True, "restart": True}
    if s in ("inactive", "deactivating", "stopping", "failed", "dead"):
        return {"start": True, "stop": False, "restart": False}
    if s in ("missing", "not-found", "disabled", "masked"):
        return {"start": False, "stop": False, "restart": False}
    return {"start": True, "stop": True, "restart": True}   # unknown: show all
