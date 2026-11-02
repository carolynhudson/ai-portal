"""The service-verb lookup, stated as the owner stated it and pinned.

(2026-11-02; the hero wiring is a later round - this pins the rule so the
wiring round cannot quietly invent a different one.)
"""
import pytest

from app.core.svc_actions import actions_for


def test_running_offers_stop_and_restart_not_start():
    for state in ("active", "activating", "auto-restart", "running"):
        a = actions_for(state)
        assert a == {"start": False, "stop": True, "restart": True}, state


def test_stopped_or_stopping_offers_only_start():
    for state in ("inactive", "deactivating", "stopping", "failed", "dead"):
        a = actions_for(state)
        assert a == {"start": True, "stop": False, "restart": False}, state


def test_missing_disabled_masked_offer_nothing():
    for state in ("missing", "not-found", "disabled", "masked"):
        assert actions_for(state) == {"start": False, "stop": False,
                                      "restart": False}, state


def test_unknown_shows_everything():
    """Absence of knowledge never becomes absence of control."""
    for state in ("unknown", "", None, "garbage-from-a-new-init-system"):
        assert actions_for(state) == {"start": True, "stop": True,
                                      "restart": True}, state


@pytest.mark.parametrize("state", ["active", "ACTIVE ", "inactive"])
def test_the_lookup_is_total_and_caseless(state):
    assert set(actions_for(state)) == {"start", "stop", "restart"}
