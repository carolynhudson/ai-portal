# Features

One page per surface. Each says what it shows, what you can do, and where
the proof lives. (Screenshots are scrubbed: addresses, account paths and
token-shaped text are masked at capture.)

## Status — the machine, honest

![Status page](img/status.png)

The router's live pulse in one column: tokens per second, prompt-cache
reuse, which slot is generating, service RAM against the GPU aperture. The
attention list on top collects what needs the owner — a pending GPU-stack
update, a model index that went stale, a context-limit cutoff — each line
naming the page that can act on it.

Service cards run the truth: `active`, uptime, probe latency for web and
API, the unit name, and a log tail that stays readable. Red text here is
advice, never an action the portal took by itself.

Learn more: [PORTAL_PLAN §6.14 (the metrics rounds)](../PORTAL_PLAN.md),
[PORTAL_LAYOUT.md](../PORTAL_LAYOUT.md).

## Models — decoded from the files

![Models page](img/models.png)

The portal reads GGUF metadata itself: quant, architecture, trained
context, shard sets, sidecars (vision projectors, MTP drafters). The
library view answers the questions the router leaves implicit — which
files exist, which the router can load, which are invisible because of
layout, and what a migration to fix that would do.

The migration planner is the safety law made visible: copy and verify
before anything is deleted, size + GGUF integrity + newer-wins, the
router's loaded files never touched mid-session, every move updating the
index and the references that point at the old path.

Learn more: [MODEL_SIGNALS.md](../MODEL_SIGNALS.md),
[UPDATES.md](../UPDATES.md).

## Get Models — download with a receipt

![Get Models](img/getmodels.png)

One field takes a Hugging Face spec (`org/model:QUANT`). The portal plans
the file list and the byte count up front, then streams with pause,
resume, and cancel at file granularity. GGUF lands go through the
migration ladder into a proper router directory; single-file chains get
their sidecars in the same folder; safetensors land in ComfyUI's deepest
matching folder — copied, sha256-verified, never moved blind.

The witness: a 27B Q6_K plan of 21.4 GB, planned exactly, landed exactly,
filed into the library with its metadata intact.

Learn more: [PORTAL_PLAN §6.14.1zm (the -hf law)](../PORTAL_PLAN.md).

## Health — the coupling, visible

![Health](img/health.png)

Strix Halo's GPU memory is an aperture, and drivers, ROCm, torch, and
llama.cpp move together. The health pane shows the aperture against its
ceiling, the GPU stack's pending packages, the ROCm series llama.cpp was
built for, Vulkan, and disk. It checks that the aperture fix survived the
last kernel — because a silent reset puts the flagship model back to its
4096-token disguise.

The portal reports system updates. It never applies them. That line is
in the code, and the tests keep it there.

Learn more: [PORTAL_PLAN §6.12.0 (the aperture story)](../PORTAL_PLAN.md),
[UPDATES.md](../UPDATES.md).

## Personalities — the router, with a name

![Personalities](img/personalities.png)

A personality binds a model, its runtime settings, and (optionally) the
coding agent's voice. Presets are shown as the router will receive them;
edits go through a validated store with rollback, and the page tells you
which sections are live right now.

This is where GGUF decoding earns its keep: quant sizes, context budgets,
and slot math arrive from the files, not from guesswork.

Learn more: [PORTAL_SETTINGS.md](../PORTAL_SETTINGS.md).

## Projects — real sessions, started from a page

![Projects](img/projects.png)

Pick a folder, pick a personality, launch. The portal starts a coding
session on the box's harness host and keeps a durable view of it: history,
input, a quit that asks first. Close the browser; the session lives. Open
the portal an hour later, from another machine, and the page takes you
back to the work.

Learn more: [PORTAL_PLAN §6.14.1m–1x (harness + projects)](../PORTAL_PLAN.md).

## Workroom — the machines post their work

![Workroom](img/workroom.png)

Benches are project spaces. Agents post findings, reports, questions,
patches, logs — through the portal's MCP door, where remote writes land
quarantined. Promote publishes; reject deletes. The first bench is a real
chain-pick research project; the first posts were a Windows box's measured
A/B of four model chains. The record stays with the project.

Learn more: [COLLAB_PROPOSAL.md](../COLLAB_PROPOSAL.md),
[QWEN_IMAGE_21_RESEARCH.md](../QWEN_IMAGE_21_RESEARCH.md).

## Settings — the network, on the record

![Settings](img/settings.png)

Per-service bind (loopback/LAN/all), ports validated live, the exact
`ufw` lines the firewall would need shown before anything runs, agent
tokens for the mesh door (issued once, stored hashed, scoped, revocable),
router API-key management, and the remote image-forge link. The red lines
are the portal telling you where you are exposed — the advice, not the
act.

Learn more: [PORTAL_SETTINGS.md](../PORTAL_SETTINGS.md),
[DEBUGGING.md](../DEBUGGING.md).

## Upgrades — the update desk

![Upgrades](img/updates.png)

llama.cpp: releases on disk, the running one, the newest upstream, changelog
between, a clean-room build on a pinned commit, and rollback that points
the symlink farm back. ComfyUI: the same discipline. The GPU stack:
reported, never applied.

Learn more: [UPDATES.md](../UPDATES.md).
