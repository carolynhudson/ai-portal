"""Machine identities for the HALO mesh (MESH_PROPOSAL.md, phase P0).

The knowledge base has always been one machine's: the MCP server is a stdio
child of this box's harness, so a second machine cannot reach it. The first
thing a second machine needs is not a new store - it is a **name**.
``memory.source`` is a string an agent types about itself, which is
provenance in name only: it cannot be checked, scoped, or revoked.

This module is that name, made checkable:

* tokens are random, shown **once**, and stored **hashed** - a stolen
  database cannot be replayed as an agent, and the portal cannot print a
  live token back out;
* scopes are the door's policy, not the agent's claim: ``read`` (kb_search,
  kb_read, memory_search, memory_recent) and ``write`` (memory_add).
  Destructive tools (``memory_remove``) are portal-session only - no issued
  token carries them at all;
* revocation is a status flip, and ``last_seen`` is the liveness the mesh's
  feed will show.

P0 issues read tokens. ``write`` exists in the schema and in the door's
policy so that P1 (quarantined writes) is a policy change, not a migration.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time

from . import db

SCOPES = ("read", "write")
ACTIVE = "active"
REVOKED = "revoked"

# A name is a slug, and one name is one identity: it will appear in the audit
# trail and in the mesh feed, so it must be readable and stable.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")

# last_seen is a liveness signal, not a metric: one write per identity per
# half-minute is plenty for a feed and keeps the door's hot path cheap.
_SEEN_FLOOR = 30.0


class AgentsError(Exception):
    """A refusal the caller can show an operator."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(name: str, machine: str = "", scopes: tuple[str, ...] = ("read",)) -> tuple[dict, str]:
    """Create an identity. Returns (row, cleartext token) - the token exists
    only in this return value; the store keeps its hash."""
    name = str(name or "").strip().lower()
    if not NAME_RE.match(name):
        raise AgentsError(
            "an agent name is 2-32 characters of lowercase letters, digits and "
            "hyphens, starting with a letter or digit (rainbowblock-forge is fine)")
    wanted = tuple(dict.fromkeys(str(s).strip().lower() for s in scopes))
    if not wanted:
        raise AgentsError("an identity needs at least one scope")
    for s in wanted:
        if s not in SCOPES:
            raise AgentsError(f"unknown scope {s!r}; the scopes are {', '.join(SCOPES)}")
    if "write" in wanted and "read" not in wanted:
        raise AgentsError("write implies read - a writer that cannot read the KB "
                          "cannot check whether the fact is already there")
    db.init()
    existing = db.query("SELECT name, status FROM agents WHERE name = ?", (name,))
    if existing and existing[0]["status"] == ACTIVE:
        raise AgentsError(f"{name!r} already has an active identity - revoke it first "
                          "if you mean to replace it")
    token = secrets.token_urlsafe(32)
    now = time.time()
    if existing:
        # A revoked name is free again, and the row is reused: the history of an
        # identity lives in the audit trail (events), not in a second row, and
        # "one row per name" is what makes the listing answer "who is this".
        db.execute(
            """UPDATE agents SET machine = ?, scopes = ?, token_hash = ?, status = ?,
                              created_at = ?, last_seen = 0
               WHERE name = ?""",
            (str(machine or "").strip(), ",".join(wanted), _hash(token), ACTIVE, now, name))
    else:
        db.execute(
            """INSERT INTO agents (name, machine, scopes, token_hash, status, created_at, last_seen)
               VALUES (?,?,?,?,?,?,?)""",
            (name, str(machine or "").strip(), ",".join(wanted), _hash(token), ACTIVE, now, 0.0))
    # The audit names the identity and its scopes, never the token: the audit
    # is readable, the token is not.
    db.event("portal", "agent.issue", f"name={name} scopes={','.join(wanted)} machine={machine or '-'}")
    return {"name": name, "machine": str(machine or "").strip(),
            "scopes": list(wanted), "status": ACTIVE, "created_at": now, "last_seen": 0.0}, token


def verify(token: str) -> dict | None:
    """The identity behind a token, if it is active. Touches last_seen."""
    token = str(token or "").strip()
    if not token:
        return None
    db.init()
    rows = db.query(
        "SELECT name, machine, scopes, status, created_at, last_seen FROM agents "
        "WHERE token_hash = ? AND status = ?", (_hash(token), ACTIVE))
    if not rows:
        return None
    row = dict(rows[0])
    now = time.time()
    if now - float(row.get("last_seen") or 0.0) > _SEEN_FLOOR:
        db.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (now, row["name"]))
        row["last_seen"] = now
    row["scopes"] = [s for s in (row.get("scopes") or "").split(",") if s]
    return row


def touch(name: str) -> None:
    db.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (time.time(), name))


def revoke(name: str) -> bool:
    db.init()
    name = str(name or "").strip().lower()
    rows = db.query("SELECT name FROM agents WHERE name = ? AND status = ?", (name, ACTIVE))
    if not rows:
        return False
    db.execute("UPDATE agents SET status = ? WHERE name = ?", (REVOKED, name))
    db.event("portal", "agent.revoke", f"name={name}")
    return True


def list_agents() -> list[dict]:
    """Every identity, current and revoked, newest first. The token hash never
    leaves this module: there is no path that prints a live token."""
    db.init()
    rows = []
    for r in db.query("SELECT name, machine, scopes, status, created_at, last_seen "
                      "FROM agents ORDER BY created_at DESC"):
        d = dict(r)
        d["scopes"] = [s for s in (d.get("scopes") or "").split(",") if s]
        rows.append(d)
    return rows
