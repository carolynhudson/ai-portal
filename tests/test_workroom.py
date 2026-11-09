"""The Workroom (W0-lite): benches, posts, quarantine, the door tools, the routes.

The laws under test (docs/COLLAB_PROPOSAL.md, mesh P1):

  * a portal post self-promotes; an `agent:*` post lands pending and the tool
    answer SAYS so (an agent that believes its post is shared will not tell
    the next session);
  * promote publishes, reject deletes — and the FTS row goes with the post
    (a rejected post that stays searchable is the quarantine leaking);
  * the 16 KB cap and the secret net refuse AT WRITE TIME, and the refusal is
    audited by shape, never by text;
  * replies ride the top-level id (threads are one level deep);
  * the door scopes: bench tools are visible to read tokens, thread_post is a
    write, and a read token calling it is a refusal, not a crash.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.core import db, kb_http, kb_mcp, workroom


@pytest.fixture()
def wb_db(monkeypatch, tmp_path):
    """A private base: same schema, real FTS5, none of the portal's rows."""
    conn = sqlite3.connect(str(tmp_path / "wb.db"))
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(db._local, "conn", conn, raising=False)
    db.init()
    workroom.ensure_seed()
    yield conn


# ------------------------------------------------------------------ benches

def test_ensure_seed_opens_the_founding_bench(wb_db):
    slugs = [b["slug"] for b in workroom.list_benches()]
    assert "qwen-image-2.1" in slugs and "general" in slugs
    workroom.ensure_seed()  # idempotent: no duplicate, no error
    assert len([b for b in workroom.list_benches() if b["slug"] == "general"]) == 1


def test_seed_rejects_bad_slugs(wb_db):
    for bad in ("", "A", "x", "has space", "Ünicode"):
        with pytest.raises(workroom.WorkroomError):
            workroom.seed(bad, "t")


def test_bench_counts_see_statuses(wb_db):
    workroom.seed("proj-x", "Proj X")
    workroom.post("proj-x", "note", "shared", author="portal")
    workroom.post("proj-x", "note", "from a peer", author="agent:peer")
    b = next(x for x in workroom.list_benches() if x["slug"] == "proj-x")
    assert b["posts"] == 1 and b["pending"] == 1


# -------------------------------------------------------------------- posts

def test_portal_post_self_promotes(wb_db):
    p = workroom.post("general", "note", "the owner's own word", author="portal")
    assert p["status"] == "promoted" and p["author"] == "portal"


def test_agent_post_is_quarantined(wb_db):
    p = workroom.post("general", "finding", "measured on the peer box",
                      author="agent:rainbowblock-forge")
    assert p["status"] == "pending"
    assert workroom.count_pending() == 1


def test_promote_publishes_and_reject_deletes(wb_db):
    p = workroom.post("general", "note", "peer work", author="agent:x")
    assert workroom.promote(p["id"])["status"] == "promoted"
    assert workroom.count_pending() == 0
    q = workroom.post("general", "note", "peer junk", author="agent:x")
    assert workroom.reject(q["id"]) is True
    assert workroom.search("junk") == []  # the FTS delete trigger, not a hide
    assert workroom.promote(q["id"]) is None
    assert workroom.reject(999) is False


def test_cap_refuses_transcripts(wb_db):
    with pytest.raises(workroom.WorkroomError, match="16384"):
        workroom.post("general", "note", "x" * (workroom.MAX_TEXT + 1))


def test_kinds_are_named(wb_db):
    for k in workroom.KINDS:
        assert workroom.post("general", k, f"a {k}")["kind"] == k
    with pytest.raises(workroom.WorkroomError):
        workroom.post("general", "meme", "hi")


def test_unknown_bench_and_empty_text_refuse(wb_db):
    with pytest.raises(workroom.WorkroomError, match="bench_ls"):
        workroom.post("nope", "note", "hi")
    with pytest.raises(workroom.WorkroomError, match="needs text"):
        workroom.post("general", "note", "   ")


# ----------------------------------------------------------------- threads

def test_replies_ride_the_top_level_id(wb_db):
    top = workroom.post("general", "question", "which TE?")
    a = workroom.post("general", "answer", "qwen3vl_8b", thread=top["id"])
    b = workroom.post("general", "note", "agree", thread=a["id"])  # reply to a reply
    assert a["thread"] == top["id"] and b["thread"] == top["id"]
    assert [p["id"] for p in workroom.thread("general", top["id"])] == [top["id"], a["id"], b["id"]]


def test_threads_cannot_cross_benches_or_ghosts(wb_db):
    workroom.seed("proj-y", "Proj Y")
    top = workroom.post("general", "note", "here")
    with pytest.raises(workroom.WorkroomError, match="not on bench"):
        workroom.post("proj-y", "answer", "there?", thread=top["id"])
    with pytest.raises(workroom.WorkroomError, match="no post"):
        workroom.post("general", "answer", "ghost", thread=4242)


# -------------------------------------------------------------- secret net

def test_secret_net_refuses_at_write_time(wb_db):
    for bad in ("password: hunter2pass",
                "Authorization: Bearer abcdef0123456789",
                "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA",
                "api_key = sk-live0123456789abcdef",
                "ghp_012345678901234567890123456789012345"):
        with pytest.raises(workroom.WorkroomError, match="secret"):
            workroom.post("general", "note", bad)
    # the refusal audits the SHAPE, never the text:
    ev = db.query("SELECT detail FROM events WHERE kind = 'wb-secret-refused'")
    assert ev and "hunter2" not in ev[0]["detail"]


def test_secret_net_trusts_placeholders(wb_db):
    for ok in ("password = <from env>",
               "Authorization: Bearer <token>",
               "set PASSWORD=... in the kit",
               "token: $PORTAL_TOKEN",
               "the kit law: password = your_password_here is a placeholder",
               "PORTAL_PASSWORD.txt is never printed"):
        assert workroom.post("general", "note", ok)["status"] == "promoted"


# ------------------------------------------------------------------- recall

def test_search_finds_and_flags(wb_db):
    workroom.post("qwen-image-2.1", "finding", "the 2.1 chain needs qwen3vl_8b TE")
    q = workroom.post("qwen-image-2.1", "finding", "int8-convrot measures 2.2 s/it",
                      author="agent:forge")
    hits = workroom.search("qwen3vl")
    assert hits and hits[0]["status"] == "promoted"
    pend = workroom.search("convrot")  # hyphens: FTS parses them away; the net catches
    assert pend and pend[0]["status"] == "pending"  # found AND flagged
    assert workroom.search("") == []


# ---------------------------------------------------------------- door tools

def test_door_bench_ls_and_read(wb_db):
    workroom.post("qwen-image-2.1", "note", "seeded work")
    ls = kb_mcp.call_tool("bench_ls", {})
    assert "qwen-image-2.1" in ls["content"][0]["text"]
    rd = kb_mcp.call_tool("bench_read", {"bench": "qwen-image-2.1"}, actor="agent:x")
    body = rd["content"][0]["text"]
    assert "seeded work" in body
    miss = kb_mcp.call_tool("bench_read", {"bench": "ghost"})
    assert "no bench" in miss["content"][0]["text"]


def test_door_thread_post_quarantines_agents(wb_db):
    res = kb_mcp.call_tool("thread_post",
                           {"bench": "qwen-image-2.1", "kind": "report",
                            "text": "forge findings: turbo 8-step validated"},
                           actor="agent:rainbowblock-forge")
    body = res["content"][0]["text"]
    assert '"status": "pending"' in body and "PENDING" in body
    own = kb_mcp.call_tool("thread_post", {"bench": "general", "kind": "note",
                                           "text": "portal word"}, actor="portal")
    assert '"status": "promoted"' in own["content"][0]["text"]
    err = kb_mcp.call_tool("thread_post", {"bench": "nope", "kind": "note", "text": "x"},
                           actor="agent:x")
    assert "no bench" in err["content"][0]["text"]


def test_door_scopes_list_the_bench_tools():
    assert "bench_ls" in kb_http.READ_TOOLS and "bench_read" in kb_http.READ_TOOLS
    assert "thread_post" in kb_http.WRITE_TOOLS
    assert "thread_post" not in kb_http.READ_TOOLS
    names = [t["name"] for t in kb_mcp.TOOLS]
    assert {"bench_ls", "bench_read", "thread_post"} <= set(names)


# -------------------------------------------------------------------- routes

@pytest.fixture
def wb_client(monkeypatch):
    from fastapi.testclient import TestClient
    from app.core import config as cfg
    from app.main import app
    c = TestClient(app)
    c.cookies.set(cfg.COOKIE_NAME, cfg.make_token(cfg.load()))
    return c


def test_workroom_page_requires_auth():
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).get("/workroom", follow_redirects=False).status_code == 302


def test_workroom_page_lists_benches(wb_client):
    r = wb_client.get("/workroom")
    assert r.status_code == 200
    assert "Qwen-Image-2.1" in r.text and "Workroom" in r.text
    assert "i-workroom" in r.text  # nav entry renders (menu-only nav law)


def test_workroom_routes_post_triage_and_bench(wb_client):
    r = wb_client.post("/api/workroom/post",
                       json={"bench": "general", "kind": "note", "text": "owner words"})
    assert r.status_code == 200 and r.json()["status"] == "promoted"
    pid = r.json()["id"]

    # an agent post, quarantined, straight into the triage list:
    row = kb_mcp.call_tool("thread_post", {"bench": "general", "kind": "note",
                                           "text": "peer words"}, actor="agent:p")
    aid = row["content"][0]["text"]
    import json as _json
    aid = _json.loads(aid)["id"]

    pend = wb_client.get("/api/workroom").json()
    assert pend["count_pending"] >= 1

    assert wb_client.post("/api/workroom/reject", json={"id": aid}).status_code == 409
    assert wb_client.post("/api/workroom/reject",
                          json={"id": aid, "confirm": True}).status_code == 200
    assert wb_client.post("/api/workroom/promote", json={"id": aid}).status_code == 404

    b = wb_client.post("/api/workroom/bench",
                       json={"slug": "test-bench", "title": "T", "goal": "g"})
    assert b.status_code == 200
    assert wb_client.get("/api/workroom/bench?bench=test-bench").json()["goal"] == "g"
    assert wb_client.get("/api/workroom/bench?bench=ghost").status_code == 404
    bad = wb_client.post("/api/workroom/post", json={"bench": "general", "kind": "note",
                                                     "text": "x" * 20000})
    assert bad.status_code == 409 and "16384" in bad.json()["detail"]
    assert wb_client.get(f"/workroom?bench=general").status_code == 200
    assert "owner words" in wb_client.get("/workroom?bench=general").text
    assert pid > 0
