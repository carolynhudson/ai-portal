"""Suite-wide guards that individual test files cannot provide for themselves.

**No background model-index scan.** At app startup the portal starts a daemon thread to scan the
model library off the request path (`app.main._index_startup`), so a slow mount cannot stall a page
load. That is correct in production and a race in this suite: the thread scans whatever
`app.core.paths` points at *when it happens to run*, and a dozen test files build the app while
monkeypatching `paths` to their own throwaway homes. A scan has been observed landing in the middle of
an unrelated test in `tests/test_personality.py` and replacing that test's fixture rows with another
file's models -- which is why the budget tests there failed at random, a different test each run, at
HEAD, with nothing in the failing test touching the index.

So the scan is switched off for the whole suite, and each test that actually cares about it
re-enables it and waits for the rows (see `tests/test_modelindex.py::test_app_startup_populates...`).
The startup path stays covered; it simply stops happening at a moment nobody controls.

**Warm-up threads are joined, not orphaned.** A test that re-enables the scan must not leave its
thread running for the tests that come after it.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _no_background_index_scan():
    old = os.environ.get("AI_PORTAL_INDEX_STARTUP")
    os.environ["AI_PORTAL_INDEX_STARTUP"] = "0"
    yield
    if old is None:
        os.environ.pop("AI_PORTAL_INDEX_STARTUP", None)
    else:
        os.environ["AI_PORTAL_INDEX_STARTUP"] = old


@pytest.fixture(autouse=True)
def _join_index_warmups():
    yield
    try:
        from app import main as app_main
    except Exception:  # noqa: BLE001 — a suite may run with app/ unimportable; the guard is optional
        return
    for thread in app_main.INDEX_STARTUP_THREADS:
        thread.join(timeout=10)
    app_main.INDEX_STARTUP_THREADS.clear()
