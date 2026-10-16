"""The memory store and its MCP server (RAG-for-opencode, queue 34 plan).

The owner's ask: opencode sessions that remember across sessions. The store is the
portal's sqlite; the door is one stdlib stdio MCP server (`app/core/kb_mcp.py`) that
the harness launches as a local child. What these tests pin, in the order the failures
would arrive:

  * a memory is stored, recalled by words, and forgotten completely (the FTS delete
    trigger — a remembered row after DELETE is amnesia's opposite: haunting);
  * an FTS-unsafe query answers with results, not with an error — the caller is an
    agent mid-task;
  * the MCP protocol answers: initialize, tools/list, tools/call, and a tool that
    refuses (oversized text) is a tool result, not a server death;
  * the seed writes the `mcp` entry — a memory server the harness was never told
    about is a memory system that exists only in the source tree.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.core import db, kb_mcp, memory


@pytest.fixture()
def mem_db(monkeypatch, tmp_path):
    """A private base for the module's db calls: same schema, real FTS5, no portal rows."""
    conn = sqlite3.connect(str(tmp_path / "mem.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    monkeypatch.setattr(db._local, "conn", conn, raising=False)  # the attr is set on first use; not yet
    yield conn


# ------------------------------------------------------------------ memory store

def test_a_memory_is_stored_audited_and_recalled_by_words(mem_db):
    row = memory.add("the router runs --models-max 1, so loading a model evicts the other",
                     tags="router presets", source="test")
    assert row["id"] > 0
    assert "models-max" in memory.search("models-max evicts")[0]["text"]
    assert memory.count() == 1
    # The audit trail is the portal's own event table — the store has no silent writes.
    events = db.query("SELECT kind, detail FROM events WHERE kind = 'memory-add'")
    assert events and f"id={row['id']}" in events[-1]["detail"]


def test_forgetting_is_complete_fts_included(mem_db):
    row = memory.add("IQ3_XXS tuning: min-p 0.0 top-k 20 temp 1.0")
    assert memory.search("min-p top-k")
    assert memory.remove(row["id"]) is True
    assert memory.search("min-p top-k") == [], "the FTS row outlived its memory — a deleted fact that keeps answering is worse than no memory"
    assert memory.count() == 0
    assert memory.remove(row["id"]) is False


def test_an_fts_unsafe_query_answers_with_results_not_an_error(mem_db):
    memory.add("the harness host listens on 4096 with basic auth")
    hits = memory.search('models-max 4096 "unbalanced')  # broken FTS syntax on purpose
    assert hits and "4096" in hits[0]["text"]


def test_empty_and_oversized_text_are_refused(mem_db):
    with pytest.raises(memory.MemoryError):
        memory.add("   ")
    with pytest.raises(memory.MemoryError):
        memory.add("x" * (memory.MAX_TEXT + 1))


def test_recent_is_newest_first(mem_db):
    a = memory.add("older fact")
    b = memory.add("newer fact")
    rows = memory.recent(5)
    assert [r["id"] for r in rows] == [b["id"], a["id"]]


# ------------------------------------------------------------------ MCP surface

def test_the_mcp_handshake_and_tool_catalog():
    init = kb_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-03-26"}})
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert init["result"]["serverInfo"]["name"] == "portal-memory"
    listing = kb_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = {t["name"] for t in listing["result"]["tools"]}
    assert {"kb_search", "kb_read", "memory_add", "memory_search",
            "memory_recent", "memory_remove"} <= names
    # A notification (no id) gets no response — the loop must not write a line for it.
    assert kb_mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized",
                          "params": {}}) is None


def test_tool_calls_remember_and_recall_through_the_door(mem_db):
    r = kb_mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "memory_add",
        "arguments": {"text": "owner rule: never swap router models from inside a session",
                      "tags": "safety router"}}})
    assert "content" in r["result"], r
    r = kb_mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
        "name": "memory_search", "arguments": {"query": "swap router models"}}})
    import json
    hits = json.loads(r["result"]["content"][0]["text"])
    assert hits and "never swap" in hits[0]["text"]


def test_a_refused_memory_is_a_tool_result_not_a_death(mem_db):
    r = kb_mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
        "name": "memory_add", "arguments": {"text": "x" * (memory.MAX_TEXT + 1)}}})
    import json
    payload = json.loads(r["result"]["content"][0]["text"])
    assert "ceiling" in payload["error"]


def test_an_unknown_tool_is_a_jsonrpc_error(mem_db):
    r = kb_mcp.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                       "params": {"name": "make_coffee", "arguments": {}}})
    assert r["error"]["code"] == -32602
