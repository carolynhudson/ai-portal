"""Mesh identities and the HTTP door (MESH_PROPOSAL.md, phase P0).

The gap these tests exist for: the knowledge base was one machine's. The
stdio MCP server is a child of this box's harness, and `memory.source` was a
string an agent typed about itself. P0 gives the KB a door and the door an
identity, and the pins below are the promises that make a second machine
safe to let in:

  * a token is shown once and stored hashed - nothing in the store, the
    listings, or the audit trail can print a live token;
  * scopes are the door's policy: a read identity cannot see, and cannot
    call, a write tool - and the refusal names the scope, so the agent that
    hits it knows what to ask for;
  * destructive tools are on no token at all;
  * the audit says who called what, never what they typed.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.core import agents, db, kb_http


@pytest.fixture()
def mesh_db(monkeypatch, tmp_path):
    """A private base: same schema and migrations, no portal rows."""
    conn = sqlite3.connect(str(tmp_path / "mesh.db"))
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(db._local, "conn", conn, raising=False)
    db.init()
    yield conn


class Req:
    """The slice of a request the door reads: method, headers, cookies."""

    def __init__(self, method="POST", headers=None, cookies=None):
        self.method = method
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.cookies = cookies or {}


def call(door_req, payload):
    raw = b"" if payload is None else json.dumps(payload).encode()
    return kb_http.handle_request(door_req, raw)


def body_of(resp):
    return json.loads(resp.body)


def token_of(name="rainbowblock-forge", scopes=("read",)):
    _row, token = agents.issue(name, "RainbowBlock.local", scopes)
    return token


# --------------------------------------------------------------- identities

def test_a_token_is_shown_once_and_stored_hashed(mesh_db):
    row, token = agents.issue("rainbowblock-forge", "RainbowBlock.local", ("read",))
    assert row["name"] == "rainbowblock-forge" and row["scopes"] == ["read"]
    assert token and len(token) >= 32

    # the store holds no cleartext, and no listing can produce it
    raw = mesh_db.execute("SELECT token_hash FROM agents").fetchone()[0]
    assert token not in raw and len(raw) == 64

    listed = agents.list_agents()
    assert [a["name"] for a in listed] == ["rainbowblock-forge"]
    assert "token_hash" not in listed[0], "the listing must not carry the credential"

    assert agents.verify(token)["name"] == "rainbowblock-forge"
    assert agents.verify(token + "x") is None
    assert agents.verify("") is None


def test_names_are_slugs_and_scopes_are_shaped(mesh_db):
    with pytest.raises(agents.AgentsError):
        agents.issue("Rainbow Block!", "m", ("read",))
    with pytest.raises(agents.AgentsError):
        agents.issue("ok-name", "m", ("write",))          # write implies read
    with pytest.raises(agents.AgentsError):
        agents.issue("ok-name", "m", ("sudo",))           # unknown scope
    agents.issue("ok-name", "m", ("read", "write"))
    with pytest.raises(agents.AgentsError):
        agents.issue("ok-name", "m", ("read",))           # one active identity per name
    assert agents.revoke("ok-name") is True
    # a revoked name is free again: rotation is revoke-then-issue, and the
    # revoked row stays for the audit trail
    agents.issue("ok-name", "m", ("read",))
    assert agents.revoke("ok-name") is True
    assert agents.revoke("ok-name") is False


def test_revocation_kills_the_token_and_the_audit_names_the_who(mesh_db):
    token = token_of()
    assert agents.verify(token) is not None
    agents.revoke("rainbowblock-forge")
    assert agents.verify(token) is None
    events = [dict(r) for r in db.query("SELECT actor, kind, detail FROM events")]
    kinds = [e["kind"] for e in events]
    assert "agent.issue" in kinds and "agent.revoke" in kinds


# ------------------------------------------------------------------- door

def test_the_door_refuses_anonymity(mesh_db):
    resp = call(Req(), {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp.status_code == 401
    assert "Bearer" in body_of(resp)["error"]["message"]

    resp = call(Req(headers={"authorization": "Bearer not-a-token"}),
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp.status_code == 401

    resp = call(Req(method="GET"), None)
    assert resp.status_code == 405, "GET answers with a pointer, not a stack trace"


def test_a_read_identity_sees_reads_and_no_writes(mesh_db):
    token = token_of()
    resp = call(Req(headers={"authorization": f"Bearer {token}"}),
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in body_of(resp)["result"]["tools"]}
    assert {"kb_search", "kb_read", "memory_search", "memory_recent"} <= names
    assert "memory_add" not in names, (
        "a read agent must not be offered the write tool it cannot use")
    assert "memory_remove" not in names, "a destructive tool is on no token"


def test_the_door_answers_the_questions_the_windows_agent_paid_for(mesh_db):
    """The P0 acceptance, in miniature: the remote machine asks the KB a
    question, gets section refs, and writes nothing."""
    token = token_of()
    req = Req(headers={"authorization": f"Bearer {token}"})
    resp = call(req, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "kb_search",
                                 "arguments": {"query": "powershell parser ASCII",
                                               "limit": 5}}})
    hits = json.loads(body_of(resp)["result"]["content"][0]["text"])
    assert hits, "the KB recorded the PowerShell lessons - a door that cannot find them is decorative"
    assert all(h["ref"] for h in hits), "cite the ref: answers arrive with their address"

    # and the same identity cannot write, even by name
    resp = call(req, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                      "params": {"name": "memory_add",
                                 "arguments": {"text": "poison", "source": "x"}}})
    assert resp.status_code == 200, "a refusal is a tool result, not a transport error"
    text = body_of(resp)["result"]["content"][0]["text"]
    assert "scope" in text and "memory_add" in text
    assert "memory_add" not in resp.headers["X-Mesh-Scope"]
    assert db.query("SELECT id FROM memory") == [], "a read identity wrote to the store"


def test_a_write_identity_can_write_and_the_audit_keeps_its_mouth_shut(mesh_db):
    token = token_of("writer", ("read", "write"))
    req = Req(headers={"authorization": f"Bearer {token}"})
    resp = call(req, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                      "params": {"name": "memory_add",
                                 "arguments": {"text": "the forge VAE lives in models/vae/ae.safetensors",
                                               "tags": "forge", "source": "mesh"}}})
    assert not body_of(resp)["result"].get("isError"), body_of(resp)["result"]
    rows = [dict(r) for r in db.query("SELECT text, source FROM memory")]
    assert rows and rows[0]["source"] == "mesh", (
        "a write through the door carries the door's provenance, not the agent's claim")

    # the audit records the identity and the tool, never the argument text
    events = " ".join(str(dict(r)) for r in db.query("SELECT actor, kind, detail FROM events"))
    assert "agent:writer" in events and "mcp.call" in events
    assert "ae.safetensors" not in events, "the audit copied the payload"
    assert token not in events, "a live token reached the audit trail"


def test_a_malformed_body_is_refused_loudly_and_cheaply(mesh_db):
    """One JSON-RPC object per POST, and the refusal costs no database work."""
    token = token_of()
    req = Req(headers={"authorization": f"Bearer {token}"})
    assert kb_http.handle_request(req, b"not json{").status_code == 200
    assert body_of(kb_http.handle_request(req, b"not json{"))["error"]["code"] == -32600
    resp = call(req, [{"jsonrpc": "2.0", "method": "initialize"}])
    assert body_of(resp)["error"]["code"] == -32600, "one JSON-RPC object per POST"
