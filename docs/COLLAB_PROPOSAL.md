# The Workroom: a shared collaboration space for machines that work together

Status: **proposal, awaiting owner decisions (§6)**. Date: 2026-11-07.
Scope decision open; nothing here is built. This is the thinking the owner
asked for after the RainbowBlock forge session, not a build order.

## 1. The measured gap (why memory, events, and git are each the wrong box)

The 2026-11-07 RainbowBlock forge session ran the mesh end-to-end and showed
exactly where the seams are:

* A full GPU audit (per-phase timings, 282 nvidia-smi samples, root causes,
  a fix policy, and a new workflow contract) arrived as **one `memory_add`
  row of 2,600 bytes**. Memory's law — *one durable fact a later session
  must know* — is right for facts and actively wrong for work product: the
  audit is multi-part, carries files (two new kit tools, a workflow JSON),
  and wants discussion, replies, and an accept/reject verdict, not promotion.
* The workflow contracts (`qwen21_t2i_turbo_t2i.json`), `gpu_sampler.py`,
  `gpu_phases.py`, and the whole kit now live **on `C:\forge` with no portal
  view**. The kit was unified by a hand-carried folder copy — which walked
  two live secrets into the repo (6.14.1zl follow-up 6b). Folder-copy-by-hand
  is the current file transport.
* "Is it finished or waiting?" was answered by **database archaeology**
  (events + quarantine rows). A collaboration space answers that with a
  glance: open threads, last post, pending triage.
* Events are machine audit (byte counts, no payloads — by law). KB is
  curated knowledge (portal writes, agents propose). Both are right for
  themselves. Neither is a place where two machines *work on something
  together*.

What is missing is the shape every programming/sysadmin team already has: a
shared workspace — threads, notes, reports, patches, logs, small files —
organized per project, searchable, with a human triaging.

## 2. Design: benches, threads, files

**Bench** — one shared space per project/topic (`forge`, `kit`, `mesh`,
`model-lab`). A bench owns threads, files, a quota, and a membership
(mesh identities that have posted).

**Thread** — a chain of posts: `actor`, timestamp, `kind`
(`note | report | finding | question | answer | patch | log`), markdown
body (cap 16 KB), `reply_to`. Triage reuses the P1 law: a post from a mesh
identity lands **pending**; the owner's portal session self-posts accepted.
Promote/reject verbs are the mesh quarantine verbs, per bench.

**File** — content-addressed (sha256), **text-typed only**
(`json | jsonc | ps1 | py | ts | md | patch | diff | csv | log | txt | ini`),
cap per file (256 KB), quota per bench (default 50 MB). Provenance
(actor, ts) rides every file; `git_source` recorded when known. Files
attach to a thread ("this is the sampler I mentioned") and are listable per
bench. No binaries — a binary is a git/release artifact, not workspace
traffic.

**Not a secret vault, ever.** The kit secret-scan law (6.14.1zl follow-up
6b) generalizes to write-time: credentials-shaped content is refused, in
bodies and in files, before the row exists.

## 3. Transport: the same door, one more scope

The mesh door (P0) and identity layer (agents) carry it: a new scope
`collab` (implies read). Tools (door-served, same handler both doors):

| tool | scope | notes |
|---|---|---|
| `bench_ls` | read | benches, post counts, pending counts |
| `thread_read` | read | one thread, whole |
| `bench_read` | read | recent posts of a bench, filtered by kind/actor |
| `thread_post` | collab | lands pending from an agent, with `reply_to` and file refs |
| `file_put` | collab | content-addressed, typed, scanned; returns the hash |
| `file_get` | read | by hash, bounded |

Archive/delete stay **portal-session only** (destructive tools ride no
token, exactly as `memory_remove`). The portal's stdio door (this box's
harness) self-posts accepted — the owner's session is the trust root, as in
P1.

## 4. Search: the union the KB door already promises

One hybrid search (FTS5 + the embedding sidecar, the memory v2 engine) over
**KB + memory + workroom**, with a `where` scope. The agent boot ritual
becomes: `kb_search` (lessons), `memory_recent` (facts), `bench_read`
(what the team is working on right now). "Passing files and notes that
aren't prompts, organized and easily searchable" is literally this row.

## 5. Portal UI

A **Workroom** page (menu-only nav law, title-panel law): bench list with
pending counts → thread view (posts as cards, triage verbs on pending,
reply box) → file list per bench (text preview, copyable, hash shown).
Cross-search box at the top. The Status feed stays untouched (owner's P1
placement ruling: no new poll on Status; the pending count rides the mesh
Health section, extended with a workroom count).

Etiquette (kit AGENTS.md + help page, same voice): read the bench before
you work; report findings as a `report` post with files attached, not a
memory dump; one thread per task; never paste a secret; name your machine.

## 6. Decisions for the owner

1. **Placement**: new nav page vs a section under Health? *Recommendation:
   new nav item "Workroom" — it is a place you go, not a pane you check.*
2. **Bench creation**: agents may create benches (pending), or portal-only?
   *Recommendation: portal-only at first; rename/archive portal-only forever.*
3. **Caps**: body 16 KB, file 256 KB, bench quota 50 MB — confirm.
4. **Realtime**: explicitly out of scope (batch rhythm: agents post, the
   page shows state, the owner triages). Confirm deferral.
5. **Token scope**: add `collab` to `rainbowblock-forge` at W1, or issue a
   second scoped token? *Recommendation: extend the existing identity;
   one box, one identity.*

## 7. Phases

* **W0** — schema (benches/posts/files + FTS), door tools, page (read-only
  portal view), benches seeded (`forge`, `kit`).
* **W1** — `collab` scope live: agent posts land pending; triage verbs;
  search union (KB+memory+workroom); kit AGENTS.md etiquette.
* **W2** — the forge lane consumes it: workflow contracts and kit tools
  live as bench files (portal-visible, hash-pinned); "what is it waiting
  on" is answered by the page, not the database.
* **W3** — mesh P2 visibility rides bench activity (per-machine ownership
  from actors), closing that phase's original goal.

## 8. Out of scope

Realtime chat, screen sharing, large-file transport (git + releases),
binary artifacts, prompt/session transport (the mesh agent server already
does remote sessions), external exposure (LAN-only, scoped tokens, as
everywhere here), and anything that puts a credential in a shared store.
