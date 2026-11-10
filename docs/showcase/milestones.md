# Milestones

What the project has survived, in the order it happened. Each entry has a
record in the project journal; the links go to it.

## The house before the portal

The box arrived like every AI box does: three ComfyUI folders, two llama.cpp
clones, models scattered across five places, and a Python environment nobody
dared touch. The first act was a survey — every file, size, and symlink,
written down — then a phased migration to one tree (`~/ai/`) with rollback
instructions written before anything moved. Phases one through five ran in
August 2026; the survey survives as
[ENVIRONMENT.md](../ENVIRONMENT.md).

## The 4096-token mystery

The flagship model — 88 GB of weights — loaded cleanly and reported a
4096-token context. A trained context of 262,144 reduced to 4096 looks like
a model defect. It was a driver: the iGPU's window onto system RAM
(the GTT aperture) defaults to 64 GiB, and the model's caches were the part
that didn't fit. The symptom hid the cause for a week because the model
loaded *successfully*.

The owner's fix moved the limit off the kernel command line and onto the
AMD TTM module — a window of 108 GiB, the top of the stable band on this
OS. The lesson went into the portal: the health pane now reads the aperture
and warns when a kernel update silently resets it, because this bug comes
back by itself. The portal's conclusion, written down: **a model that loads
and reports a context far below its trained length is a low-memory failure
wearing a model-defect mask** — and that shape is worth detecting on any
machine, not just this one.

Record: [PORTAL_PLAN §6.12.0](../PORTAL_PLAN.md).

## llama.cpp, kept alive

Updating a GPU-coupled C++ project on ROCm is where good machines go to
sulk. The portal turned it into a desk: pinned-commit clean-room builds
(HIP and Vulkan), a release registry, a symlink farm that decides what
`llama-server` means, a changelog between the running release and the
newest, and a rollback that repoints the farm in one click. The same
discipline governs ComfyUI. System (apt) packages — the ROCm series that
couples to everything — are reported with the exact command, and the
portal never runs it. That line is in the code, and a test suite keeps it
there.

Record: [UPDATES.md](../UPDATES.md),
[journal §6.14.1q (GPU update desk)](../PORTAL_PLAN.md).

## Reading the models

The router accepts a directory layout with quiet rules (one level deep,
each subdirectory one model, sidecars named by pattern). The portal
decoded GGUF itself — quant, architecture, trained context, vocab, shard
completeness — and built a living index: files scanned from the library
and the HF cache, problems named, counts and stats recorded on every scan,
alerts routed to the page that can act.

That decoding earns its keep upstairs: personalities bind models and
runtime settings with real context budgets; the quant menu answers "which
Q4 of this model, how many bytes, which parts" before a download starts.
Layouts the router can't see get a migration plan, not a surprise.

Record: [MODEL_SIGNALS.md](../MODEL_SIGNALS.md),
[journal §6.14.1zm (the -hf law + witness)](../PORTAL_PLAN.md).

## The download that can't lie

A model download is a plan and a receipt. The Get Models pane takes one
field — `org/model:QUANT` — plans the exact file list and byte count
(LoRAs and imatrices never travel; sidecars follow their model), streams
with retries, and verifies at the door: sha256 for safetensors, size and
integrity for GGUF, resumable at file granularity. The witness run: a
21.4 GB Q6_K plan, landed byte-exact, filed into the router's library with
its provenance attached, cache released. A peer box's tale of a truncated
download that "succeeded" is the counter-example the law exists for.

Record: [journal §6.14.1zm](../PORTAL_PLAN.md).

## Projects, and a harness you can reach

A folder, a personality, a launch: a coding session starts on the box and
stays reachable — from the browser, from the network, after restarts. The
projects page keeps the registry, the scan, and the fixed shortlist for a
new box; the session view keeps the history. The harness (opencode) is a
first-class service the portal can update, with its plugins, its
per-personality models, and its config edits validated before they land.

Record: [journal §6.14.1m–1x](../PORTAL_PLAN.md),
[MESH_PROPOSAL.md](../MESH_PROPOSAL.md).

## Memory the agents can reach

The portal's knowledge base — every design doc, journal entry, and lesson —
is searchable by the agents through a small MCP door, and they can write
back: durable memories with FTS and vector recall. Writes from remote
machines land quarantined (the mesh's law: the owner promotes, the owner
rejects), flagged in every search until then. The box's own harness reads
its own lessons the day after they are learned, instead of re-deriving them
at four install-runs a week.

Record: [MESH_PROPOSAL.md](../MESH_PROPOSAL.md),
[journal §6.14.1zl (mesh P1)](../PORTAL_PLAN.md).

## The Workroom, and the first real exchange

Benches are project spaces; posts are the work — findings, reports,
questions, patches. The founding bench dogfooded itself: a two-box research
project on image models, where the Windows box posted a measured A/B of
four chains (a VAE swap that gained 50% sharpness; a 4-step lane that beat
the 8-step one at a third of the time), answered the portal's open
questions with numbers, and had its report promoted by the owner from the
same page it was written to. The research doc, the bench, and the portal's
memory are one record now.

Record: [COLLAB_PROPOSAL.md](../COLLAB_PROPOSAL.md),
[QWEN_IMAGE_21_RESEARCH.md](../QWEN_IMAGE_21_RESEARCH.md).

## The safety law, tested

Destructive actions confirm; rejects confirm; deletions verify before they
remove; the portal never applies system updates; nothing restarts by
accident; the password is generated once, stored at 0600, and never
printed by a log; the Workroom refuses secrets at write time and audits
the refusal by shape. And the suite proves it: 1,366 tests, including a
harness that drives every page in a real DOM, watches for dead controls,
and fails a nav bar that overflows a phone by 36 pixels.

Record: [PORTAL_PLAN.md](../PORTAL_PLAN.md) (the journal; it has the
dates), [tests/](../tests/).

## Next

The 128 GB MacBook joins the mesh as a second AI box (the plan is written:
router spine, MLX speed lane, pair-programming lane). The mesh grows toward
the owner's phrase: a shared repository of work and truth.

Record: [MACBOOK_AI_PLAN.md](../MACBOOK_AI_PLAN.md).
