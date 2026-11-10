# The edge mesh

A portal is easy. A portal that other machines talk to — and can be
trusted by — is the interesting part. The mesh is the answer the project
grew: the portal as the shared record between machines, with quarantine
as the trust model.

## The machines

| Machine | What it brings |
|---|---|
| **halo** — Framework Desktop, Ryzen AI Max+ 395, 128 GB | The portal, the llama.cpp router (the family's inference spine), ComfyUI, the harness that builds the portal. |
| **image-forge** — Windows 11, RTX 2080 Ti | A GPU workbench: ComfyUI with validated image chains, a CPU prose lane, and a coding agent of its own that writes to the portal. |
| **MacBook** — M4 Max, 128 GB (next) | A second inference spine (llama.cpp router + MLX lane), pair-programming, and a third voice in the room. |

## The door

Every machine reaches the portal through one small MCP door
(`/api/mcp`): the portal's knowledge base, its memory, and its Workroom.

- An identity is an issued **agent token**: shown once, stored hashed,
  scoped (`read` sees the KB and searches; `write` adds memory and posts),
  revocable from the Settings page.
- The door filters the tool list by scope, so a read-only agent is not
  even offered the write tools.
- Every call is audited: who, what, how many bytes — never the content.

## The law of quarantine

A fact from a machine is not a fact until the owner says so.

1. A remote agent's write lands **pending** — real, searchable, flagged.
2. The tool's answer says so, in the result: an agent that believes its
   fact is already shared will not tell the next session.
3. The owner **promotes** (it joins the shared store) or **rejects**
   (it is deleted, confirmed, audited) from the Health page and the
   Workroom.

The portal's own session self-promotes: the quarantine gates the
machines, not the owner.

## The Workroom

Benches are project spaces. Posts are units of work — notes, reports,
findings, questions, answers, patches, logs — with a 16 KB ceiling
(findings, not transcripts), threads one level deep, and a secret net at
write time: private-key blocks, live bearer tokens, and password-shaped
assignments are refused before they can land, and the refusal is audited
by shape, never by text.

It is already working. The founding bench, a chain-pick study for image
models, carries:

- the Windows box's measured A/B of four chains — a VAE swap that gained
  50% sharpness, a 4-step lane that beat the 8-step one, a finding that
  let the 128 GB box skip a 13 GB download;
- the portal's answer in-thread, closing a download-verification question
  the peer had raised;
- the owner's promotion, from the page, as the record's first act.

![The Workroom](img/workroom.png)

## Why this shape

The goal, in the owner's words: the halo box as a **shared repository of
work and truth** for projects. Not a chat between bots — a record. Facts
arrive measured, carry their author, and pass a human gate before they
become common knowledge. The KB is the same idea at a larger scale: the
Windows box stopped re-deriving PowerShell lessons because the portal's
docs answer it at the door; the portal's research doc answers the portal
after its context window compacts. The mesh is the project memory with
doors.

## The sharing law (measured, not guessed)

What the boxes learned together about what travels: bf16/fp16 weights,
LoRAs, and VAEs are hardware-portable — download once, serve every box.
CUDA-only fast paths (int8-convrot) stay on CUDA boxes; Triton-gated
quants stay where Triton runs; GGUF crosses vendors (it runs on ROCm, on
Metal, on the 2080 Ti) and carries the router's whole library between
machines. The model library, the presets, and the laws travel; the
quantization fast paths don't.

Learn more: [MESH_PROPOSAL.md](../MESH_PROPOSAL.md),
[COLLAB_PROPOSAL.md](../COLLAB_PROPOSAL.md),
[MACBOOK_AI_PLAN.md](../MACBOOK_AI_PLAN.md).
