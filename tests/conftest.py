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
import shutil
import tempfile
from pathlib import Path

import pytest

# ------------------------------------------------------------------ the guard that matters most
#
# `app.core.paths` resolves HOME from `AI_PORTAL_HOME` **at import time**, before any fixture runs.
# Every test module in this suite therefore sets a throwaway home at its own top, before it imports
# anything from `app/`. A module that forgets does not fail: it silently gets the real `~/ai`, and a
# fixture that "clears the library" then clears the owner's actual models.
#
# That is not hypothetical. A test file added on 2026-09-25 set `AI_PORTAL_PASSWORD` and not
# `AI_PORTAL_HOME`, and its fixture's `shutil.rmtree` over `paths.LLAMA_MODELS` deleted 13 model
# directories and 274 GiB of GGUFs from the live library. Nothing in the test looked wrong; the
# library it was clearing was not a fixture. It is recorded here because a footgun that has already
# fired once is not a documentation problem.
#
# So the home is set here, at conftest import, which is earlier than any test module's import. A
# module can still override it deliberately (test_modelindex.py does, to point at its own home);
# it cannot accidentally fall back to the real one.
# A fresh directory per run rather than a fixed name. A fixed name survives between runs, and whatever
# an earlier run left behind arrives as state this run never created: `model_index` rows for models
# that are no longer on disk, a snapshot a rollback test "restores" that some other test wrote, a
# presets.ini whose diff starts out empty. Each of those reads like a code bug. Making the home fresh
# is what makes "the suite is green" a statement about this run.
# Do NOT set AI_PORTAL_PASSWORD here. It is tempting — a module that forgets to set
# one gets a KeyError out of its own `os.environ["AI_PORTAL_PASSWORD"]` rather than a
# clean refusal — but test modules claim it with `setdefault`, so a default placed
# here wins the race and every module that then logs in with the password it *thinks*
# it set fails. Failed logins are counted, the throttle table lives in the one
# session-wide home, and after ten of them every later module gets a 401: that was
# measured, 56 failures and 12 errors from a single line added here.
if "AI_PORTAL_HOME" in os.environ:
    SUITE_HOME = os.environ["AI_PORTAL_HOME"]
    _MADE_HOME = False
else:
    SUITE_HOME = tempfile.mkdtemp(prefix="aiportal-suite-")
    os.environ["AI_PORTAL_HOME"] = SUITE_HOME
    _MADE_HOME = True


@pytest.fixture(autouse=True, scope="session")
def _never_the_real_home():
    """Fail loudly if anything has resolved `app.core.paths` onto a real home.

    Checked after imports, because that is when `paths` froze its value -- which is also the point at
    which a mistake has already been made, so this is an alarm, not a prevention. Prevention is the
    `SUITE_HOME` line above plus tests/sandbox.py refusing to wipe anything outside a temp home.
    """
    from app.core import paths

    real_home = Path.home().resolve()
    resolved = Path(paths.HOME).resolve()
    if resolved == real_home:
        pytest.exit(f"tests are pointed at the real home ({resolved}); "
                    f"AI_PORTAL_HOME must be a throwaway directory", returncode=1)
    if not str(resolved).startswith(str(Path(tempfile.gettempdir()).resolve())):
        pytest.exit(f"tests are pointed outside the temp directory ({resolved}); "
                    f"a fixture that clears a library must not be able to reach a real one",
                    returncode=1)

    yield
    # Only ever remove a directory this run made. An operator pointing the suite at a home of their
    # own is a debugging affordance, and cleaning up after them is not mine to do.
    if _MADE_HOME:
        shutil.rmtree(SUITE_HOME, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_inherited_login_throttle():
    """Each module's failed logins stay its own.

    `app.core.ratelimit` keeps its counters in module-level state, and the whole suite
    runs in one process against one home. So failed attempts from one module are
    visible to every later module: once `MAX_FAILURES` (10) accumulate, unrelated
    tests start getting 401s from endpoints they never touched. That was observed —
    one bad login path in a fixture turned into 56 failures and 12 errors spread
    across test_hero, test_jobs, test_network, test_comfyui and others, none of which
    had anything to do with auth. Clearing the counters around each test keeps an
    auth experiment local to the test that ran it; the throttle itself stays covered
    by `tests/test_network.py::test_login_throttles_repeated_failures`, which calls
    `ratelimit.reset()` itself and does its sequence inside a single test.
    """
    try:
        from app.core import ratelimit
    except Exception:  # noqa: BLE001 — the guard is optional if app/ is unimportable
        return
    ratelimit.reset()
    yield
    ratelimit.reset()


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
