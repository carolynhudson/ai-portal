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
    """A private base for the module's db calls: same schema and migrations, real
    FTS5, no portal rows. db.init() runs on the patched connection so the v2
    embedding-column migration is on the path, exactly as production sees it."""
    conn = sqlite3.connect(str(tmp_path / "mem.db"))
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(db._local, "conn", conn, raising=False)  # the attr is set on first use; not yet
    db.init()
    yield conn


@pytest.fixture()
def embedder(monkeypatch):
    """A deterministic stub of the sidecar: vectors by keyword, so ranking is
    assertable without a model. The real client (app/core/embeddings.py) is a
    transport; these tests are about the blend."""
    def vec_for(text: str) -> list[float]:
        if "quant" in str(text).lower():
            return [1.0, 0.0]
        if "color" in str(text).lower():
            return [0.0, 1.0]
        return [0.7, 0.7]
    monkeypatch.setattr("app.core.embeddings.live", lambda timeout=3.0: True)
    monkeypatch.setattr("app.core.embeddings.embed_documents",
                        lambda texts, title="none": [vec_for(t) for t in texts])
    monkeypatch.setattr("app.core.embeddings.embed_query",
                        lambda q: vec_for("quant" if "quant" in str(q).lower()
                                          else ("color" if "color" in str(q).lower() else "")))
    return vec_for


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


# --------------------------------------------------------- hybrid recall (RAG v2)

def test_hybrid_recall_finds_the_semantic_match_the_words_miss(embedder, mem_db):
    """The claim of embeddings over FTS, asserted where it must show: a query whose
    words appear nowhere, and whose meaning lives in one row. FTS alone answers
    nothing; the blend ranks the semantic match first — the exact case the pi-era
    context tools underdelivered on.

    The writes go through the MCP door, not memory.add directly: embed-on-write is
    the door's policy (the store stays embedder-agnostic), so this is the path an
    agent's write actually travels.
    """
    import json

    def add(text):
        r = kb_mcp.handle({"jsonrpc": "2.0", "id": 20, "method": "tools/call", "params": {
            "name": "memory_add", "arguments": {"text": text}}})
        return json.loads(r["result"]["content"][0]["text"])

    add("the chosen quants are IQ3_S and IQ3_XXS for the big models")
    add("the operator picked red as the accent color for the portal")
    hits = kb_mcp._hybrid_search("quantization", 5)
    assert hits, "hybrid search answered nothing to a query it can embed"
    assert hits[0]["text"].startswith("the chosen quants"), hits[0]["text"]
    r = kb_mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "memory_search", "arguments": {"query": "quantization"}}})
    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload and "quants" in payload[0]["text"]


def test_a_new_memory_is_vectorized_while_the_sidecar_is_live(embedder, mem_db):
    import json
    r = kb_mcp.handle({"jsonrpc": "2.0", "id": 21, "method": "tools/call", "params": {
        "name": "memory_add",
        "arguments": {"text": "the router preset top-k is 20 and min-p is 0.0"}}})
    row = json.loads(r["result"]["content"][0]["text"])
    assert kb_mcp._hybrid_search("top-k preset", 5)[0]["id"] == row["id"]
    assert memory.missing_embedding() == [], "the embed-on-write pass skipped this row"


def test_search_and_write_survive_the_sidecar_being_down(mem_db, monkeypatch):
    """FTS-only is a state, not a failure: a memory store that refuses writes
    because an optional accelerator is offline is an outage nobody asked for."""
    monkeypatch.setattr("app.core.embeddings.live", lambda timeout=3.0: False)
    row = memory.add("the harness answers on port 4096")
    assert memory.missing_embedding(), "with the sidecar down the row must queue for reindex"
    hits = kb_mcp._hybrid_search("4096", 5)
    assert hits and hits[0]["id"] == row["id"]


def test_reindex_backfills_after_the_sidecar_returns(embedder, mem_db, monkeypatch):
    import json
    monkeypatch.setattr("app.core.embeddings.live", lambda timeout=3.0: False)
    row = memory.add("the aperture ceiling is 108 GiB, measured by the owner")
    monkeypatch.setattr("app.core.embeddings.live", lambda timeout=3.0: True)
    r = kb_mcp.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                       "params": {"name": "memory_reindex", "arguments": {}}})
    payload = json.loads(r["result"]["content"][0]["text"])
    assert payload["embedded"] == 1, payload
    assert memory.missing_embedding() == []
    assert len(memory.embedded_rows()) == 1
