# mesh/contracts — the contract-pack repo (NODE_CONTRACT.md)

Shareable unit of engine capability for the mesh: a pack is
`<pack>@<version>/` containing workflow JSONs (ComfyUI graphs and/or
service recipes), post-tools, verifier specs, and `requirements.json`
(`vram_gb_min`, `backend: cuda|mps|cpu`, OS notes). Nodes materialize
per-OS launchers from the pack; the portal repo is the canonical host
(it is the repo). Packs are versioned; nodes report the pack version in
their capability manifest (`pack` field of the service card).

Planned packs (owner roadmap):
- `image-forge/` — icon/element suite (plate -> scale -> verify lineage),
  sprite-cell suite (gen character sheet -> deterministic
  slice/normalize/measure; the plate-law split, where cloud genAI fails).
- `llama/` — router preset recipes (ctx caps, spec-type, idle law).

Rule from the plate law: anything deterministic (composite, slice, key,
verify) lives in the pack as code; anything generative stays a prompt +
contract. Verifiers print measured values.
