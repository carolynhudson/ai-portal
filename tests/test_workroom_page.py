"""Workroom PAGE: the newest-first window, the collapsed older index,
expand-in-place, the pager, and the whole-tile link.

The owner's two complaints are the spec: (1) tiles must register clicks
anywhere in the tile (stretched-link CSS), (2) a growing bench must not
render every transcript per visit -- newest n expanded, older collapsed
to one-line rows (age, kind, source, first line) that expand IN PLACE.
Nothing is dropped: the index keeps every post reachable.
"""
import os
import tempfile

os.environ.setdefault("AI_PORTAL_HOME", tempfile.mkdtemp(prefix="aipp-wrp-"))
os.environ.setdefault("AI_PORTAL_PASSWORD", "test-password-123")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core import db, workroom  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    """The module's temp AI_PORTAL_HOME IS the private base (paths build
    from it, so every request thread agrees -- a thread-local conn patch
    would be invisible to TestClient's worker thread)."""
    db.init()
    c = TestClient(app)
    r = c.post("/login", data={"password": os.environ["AI_PORTAL_PASSWORD"]},
               follow_redirects=False)
    assert r.status_code == 302
    return c


def _bench_with(slug, n_posts):
    """Each test gets its OWN bench: the temp base is module-scoped and
    posts accumulate across tests; a shared bench would make counts lie."""
    workroom.seed(slug, f"Window Test {slug}", "page windowing")
    for i in range(1, n_posts + 1):
        workroom.post(slug, "note", f"post {i} body: " + ("x" * 40))
    return list(reversed(db.query(
        "SELECT id, text FROM wb_posts WHERE bench=? ORDER BY id", (slug,))))


def test_window_newest_first_with_collapsed_index(client):
    ordered = _bench_with("wr-a", 20)  # ordered: newest first
    r = client.get("/workroom?bench=wr-a")
    assert r.status_code == 200
    t = r.text
    assert "newest first · 15 of 20 shown" in t
    assert t.count('class="wr-post"') == 15
    assert t.count('class="wr-oldrow"') == 5
    # the newest post is expanded; the oldest lives as an index row
    assert ordered[0]["text"][:40] in t
    assert 'class="wr-oldtext"' in t and "post 1 body" in t
    # the index row names age, kind, source, and links expand-in-place.
    # ids are GLOBAL in the shared suite base (other modules post first),
    # so never assert an id literal -- derive it.
    assert "&open=" in t and f"#{ordered[-1]['id']} note" in t


def test_open_expands_in_place(client):
    ids = [p["id"] for p in _bench_with("wr-b", 20)]
    r = client.get(f"/workroom?bench=wr-b&open={ids[-1]}")
    t = r.text
    assert t.count('class="wr-post"') == 16      # 15 + the opened one
    assert t.count('class="wr-oldrow"') == 4
    assert "collapse opened" in t
    # expanding several rows keeps the rest collapsed
    assert client.get(f"/workroom?bench=wr-b&open={ids[-1]},{ids[-2]},{ids[-3]}").text.count(
        'class="wr-post"') == 18


def test_pager_shows_more(client):
    _bench_with("wr-c", 20)
    t = client.get("/workroom?bench=wr-c").text
    assert "show 5 more" in t and "&n=20" in t
    t2 = client.get("/workroom?bench=wr-c&n=20").text
    assert t2.count('class="wr-post"') == 20
    assert 'class="wr-oldrow"' not in t2          # nothing left to collapse
    assert "15 of 20" not in t2 and "20 of 20 shown" in t2


def test_tile_is_a_whole_area_link(client):
    _bench_with("wr-d", 1)
    t = client.get("/workroom?bench=wr-d").text
    assert 'href="/workroom?bench=wr-d"' in t
    # the stretched-link rule is the click-anywhere fix (CSS, not JS)
    assert ".wr-bench a::after{content:\"\";position:absolute;inset:0" in t
    assert "min-height:44px" in t                 # tap-target floor


def test_small_bench_needs_no_window_chrome(client):
    workroom.seed("tiny", "Tiny Bench", "few posts")
    workroom.post("tiny", "note", "only word")
    t = client.get("/workroom?bench=tiny").text
    assert "newest first · 1 of 1 shown" in t
    assert 'class="wr-oldrow"' not in t
    assert "show 15 more" not in t
