# The HALO mesh — shared knowledge for every connected machine

Status: **owner-approved in direction (2026-10-30, "full collaboration");
P0 shipped and live 2026-10-30 (§6.14.1w)** — door + identities, read-only.
P1–P3 not built. Measured ground below is current.

## 1. The ask

The owner's question, verbatim: *"should we use this halo system as a shared
repository and knowledge base for all these connected projects for different
systems and if so how can it be easy for you all to collaborate and see the
work each other has done."*

The answer is yes, with one split: the portal is the right home for
**knowledge** (journal, memory, docs, registry, audit). It is not the right
home for **code** — code wants git, and pushes staying owner-keyed is a
feature, not friction. So: shared knowledge now, shared code as a sidecar.

## 2. Measured ground (2026-10-30)

| thing | state |
|---|---|
| Knowledge base | `app/core/kb.py`: docs corpus, `DOC_ORDER` (14 docs), section refs, lexical + vector search |
| Shared memory | `app/core/memory.py`: sqlite table `(id, text, tags, source, created_at)` + FTS5, hybrid recall when embeddings live; **audited in portal events** |
| MCP door today | `app/core/kb_mcp.py` (247 lines): **stdio**, stdlib-only, JSON-RPC; tools `kb_search/kb_read/memory_add/memory_search/memory_recent/memory_remove` |
| Who can use it today | only the Halo harness: `harnessctl` seeds it into opencode config as a local child process — one machine, one lineage |
| Identity | `memory.source` is a **free-text string**. No agent identities, no scopes, no revocation |
| Auth today | session cookies: `cfg.make_token/token_valid` (`app/main.py:555`), one human session |
| Projects | registry table + Projects page; sessions launch in place. No machine-ownership column |
| Windows agent | opencode on RainbowBlock, provider = the Halo router, LAN-bound. Cannot see the KB; it re-learned four PowerShell parse failures that the KB already recorded |
| Remote-machine precedent | ComfyUI `8188` and opencode serve `4096` are LAN-pinned to `192.168.1.228` — the network discipline exists and works |

The Windows agent's four-run parse-error arc is the case study: the lesson
(`Language.Parser::ParseInput`, ASCII, PS5.1) was in the KB **the day it was
learned**, and the second machine paid three runs for it anyway. That is the
gap this proposal closes.

## 3. Design

### 3.1 The door — MCP over HTTP

`kb_mcp.py` stays stdio (the harness's child must stay dependency-free). The
new door is a portal endpoint speaking MCP streamable HTTP at `/mcp`,
loopback-bound, reusing the portal's own auth. Read tools free; write tools
gated by identity (3.2). One transport, two policies.

### 3.2 Identity — issued, scoped, revocable

New table `agent(id, name, machine, scopes, token_hash, status, created_at,
last_seen)`:

* Tokens issued on the Settings page, shown **once**, stored **hashed**.
* Scopes: `read` (kb + memory search/read) and `write` (memory_add).
* Revocation is a status flip; `last_seen` is the liveness signal the feed shows.
* Every `memory_add` writes the acting agent's id — provenance stops being a
  string an agent types about itself.

### 3.3 Trust — the quarantine tier

An agent that can write into the store that grounds my answers can poison
me. So remote writes are cheap, visible, and **not canonical**:

* New column `status`: `pending | promoted | rejected`. Remote writes land
  `pending`. Portal sessions and the owner promote; promotion is the audit
  event, so the promotion trail is the trust record.
* Search surfaces pending hits, flagged; promoted outrank pending.
* Hard red line, unchanged: **no credentials in the KB, ever.** Writes are
  scanned for the shapes this box already protects (`harness.key`,
  `PORTAL_PASSWORD.txt` contents, token-like strings) and refused with the
  reason, not silently redacted.
* The journal stays curated prose written by portal sessions. Agents
  **propose** journal entries; they do not write the journal.

### 3.4 Visibility — "see the work each other has done"

No new recording, only new reading, from events that already exist:

* **Status-page feed**: recent memories grouped by agent, recent journal
  section titles, sessions per machine, remote-agent liveness.
* **Projects page** gains machine ownership: which box owns the folder, last
  session, last write. The forge becomes a project row, not a rumor.

### 3.5 Boot etiquette (cheap, and the part that makes it stick)

A convention, in `AGENTS.md` on every machine, so it is behavior and not hope:

1. Boot = `memory_recent` + `kb_search` for the task's nouns.
2. Cite refs (`guide.md#section`), do not paraphrase.
3. One durable fact per write, phrased so a stranger can use it — not a transcript.
4. Say what machine you are on, in the write.

### 3.6 Code sidecar (phase 3, deliberately last)

Bare repos on the Halo box, one per project, agent **deploy keys** per repo.
Knowledge stays in the portal; code stays git. Pushes to `ai-portal`'s own
`main` remain owner-keyed — the mesh changes nothing about that.

## 4. Phases

* **P0 — the door + identities. SHIPPED 2026-10-30 (§6.14.1w), read-only,
  live.** The door is `/api/mcp` (a JSON API, so it lives in the `/api/`
  family; the page census caught `/mcp` and the census won). `agent` table +
  `app/core/agents.py`: tokens shown once, hashed, scoped, revocable,
  `last_seen`. Issuance runs as `tools/issue_agent_token.py`, which prints
  the paste-ready `mcp` block; the generated `opencode.json` carries that
  block with a literal placeholder token (secrets never live in kit files)
  — a Settings issuance UI is a nicer skin over the same table, still open.
  Measured: read token → four tools, citable `kb_search` refs, `memory_add`
  refused naming the scope, zero writes. The Windows agent's own run of the
  acceptance (it asks, cites, writes nothing) is the remaining witness.
  Side effect: the acceptance test found a shipped defect — `kb_search`
  handed every agent `ref: null` because it mapped a key `kb.search` never
  emitted; citations now carry the section id.
* **P1 — quarantine writes.** `status` column, write scope, secret-shape
  refusal, promote/reject in the UI. Acceptance: a remote write is visible
  as pending, searchable, flagged, and promoting it is an audited event.
* **P2 — visibility.** Status feed + projects machine column. Acceptance:
  with two machines active, one screen shows who did what, last.
* **P3 — code sidecar.** Bare repos + deploy keys. Acceptance: an agent
  clones/pushes a project repo, portal records the repo link, `main` pushes
  unchanged.

## 5. Decisions for the owner

1. **Read scope default**: any issued token gets `read` (documents are
   non-secret) — yes/no? *P0 shipped with this behavior (`--scopes` defaults
   to `read`); rule to confirm or change.*
2. **Auto-promotion**: portal-session writes self-promote; remote-agent
   writes always land `pending` — yes/no?
3. **Feed placement**: Status page card (always visible, costs a poll) vs a
   Health-page section (no new poll)?
4. **Partner MacBook**: in the mesh now, or after the relay proposal's
   consent checklist runs? Recommendation: after — consent before connectivity.
5. **Machine count assumption**: two now (Halo + RainbowBlock), MacBook
   later. Design assumes few, not fleets.

## 6. Out of scope

Multi-tenant hosting, external exposure (LAN-only, pinned, as everywhere
here), agent-to-agent messaging beyond the shared store, and anything that
puts a credential in the knowledge base.
