# Node contract — the mini-portal spec (2026-11-12)

Goal (owner): the halo portal is the beta of what every mesh node runs.
Every node is a **mini-portal**: same service/management interfaces, node-
specific code behind them (OS, hardware, capacity all differ). The portal
(halo) centralizes what makes the whole work — code, knowledge, inventory,
monitoring, updates — and the mesh keeps every agent informed of what is
available, where, right now. This document is the contract every node
implements; AI_MESH_PROPOSAL.md is the architecture it lives in.

Two implementations already exist and prove the contract is real, not
aspirational: the **portal** (reference: all six interfaces, Python/FastAPI)
and **RainbowBlock** (forge-mcp + services.json + update_llama.ps1 + the
self-managing router: cold start, --sleep-idle-seconds 900,
--models-memory-margin 16 GiB, LRU unload, ComfyUI /free after 20 render-
idle minutes; rest state 48 GB RAM free, 0 GB llama resident). meshd (Rust,
crates scaffolded on the forge) is the portable third implementation; the
MacBook joins as a fourth.

## The six interfaces (wire = MCP; OpenAI for LLM traffic)

1. **Identity + health** (`node.health`): version, build sha, pid, uptime,
   host facts, resources measured (RAM/VRAM/disk/thermal/queue). Lineage:
   the portal's /healthz ("deployed" == "serving") and Health page; the
   forge's forge_health. Must be cheap, always up, and carry the node's
   manifest version.
2. **Service registry** (`node.services`): every managed service with
   state (running/stopped/idle), port, auth mode, launcher, health probe,
   idle policy — and the router pattern generalized: services report their
   *loaded items* (models, engines) like /v1/models reports loaded/unloaded.
   Lineage: portal systemd panel + presets.ini registry; the forge's
   C:\forge\etc\services.json + router state listing. Registry vs reality
   is drift; drift at boot is a finding (the nemotron lesson).
3. **Capability manifest** (`node.caps`): what the node can DO, measured:
   contracts with fills/tier/models/validated-history + limits +
   requirements (vram_gb_min, backend cuda|mps|cpu). The forge's forge_caps
   (16 image contracts) is the reference shape; LLM ids ride the registry.
   Capability gating is the honesty layer: MacBook advertises MPS-runnable
   tiers, halo advertises zero image contracts (CPU), nobody routes work a
   box cannot run.
4. **Leases + admission** (`node.lease`): reserve-then-load, TTL'd,
   capacity-bounded; the mechanism behind "don't hurt running tasks" (the
   founding OOM incident). Idle law is the lease's twin: resources return
   to the host when idle (the personal-PC law; forge: 900 s router sleep,
   20 min ComfyUI free).
5. **Build/update mechanism** (`node.update`): the portal's install.sh/
   backup/restore/healthz-verify lineage, generalized: fetch -> stage ->
   verify -> swap-with-rollback -> smoke, ON DEMAND, never silent. The
   forge's update_llama.ps1 (fetch -> MSVC build -> staged release ->
   verify -> swap w/ .bak + router smoke) is the second proof. Feature
   releases flow portal-repo -> node: node pulls, materializes per-OS
   launcher from the recipe, runs the chain, reports versions. Fleet
   versions live on the control plane; drift visible.
6. **Management verbs** (`node.manage`): start/stop/switch/restart —
   observations free, mutations gated (the mesh quarantine law, ported):
   user-scope acts instant for scoped identities; node-policy acts land
   pending for the owner. Everything audited.

## Service-card schema (v0, portal response to forge SERVICE_MESH.md)

The advertisement unit, one per capability/service, JSON, served by
`node.caps` and aggregated by the control plane:

```json
{ "node": "rainbowblock", "kind": "image-forge", "service": "qwen21_t2i_lightning4",
  "tier": "fast-4step", "fills": ["prompt","width","height","noise_seed"],
  "requires": { "vram_gb_min": 10, "backend": "cuda" },
  "limits": { "w32": [256,1536], "concurrency": 1, "timeout_s": [30,1800] },
  "idle_policy": { "free_after_s": 1200 }, "state": "idle",
  "measured": { "warm_s": 10.1, "sharpness": 3343 }, "pack": "mesh/contracts/image-forge@<ver>" }
```

`mesh.status` = read-only aggregation (services.json + caps + /v1/models
per node) — the forge's ordered path, agreed: status -> leases+idle
governance -> place/gateway -> pack distribution.

## Node-specific code law

Interfaces are uniform; implementations are free. Python kit where Python
lives (forge kit, portal), Rust meshd for the portable daemon, node
launchers per OS (systemd user units / scheduled tasks at logon /
LaunchAgents), no-admin law everywhere. One token per node (the shared
subagent token today; P1 replaces with CA certs — the forge's ONE-token
law is the migration shape). Secrets ride env/broker, never config files
(broker pending owner GO).

## Contract packs (claimed: `mesh/contracts/` in the portal repo)

The shareable unit for engine capabilities: workflow JSONs + post-tools +
verifier specs + requirements, versioned; nodes materialize per-OS
launchers. The image-forge icon suite (plate/scale/verify) and the sprite-
cell suite (deterministic slice/normalize — the plate-law split) grow as
packs, not as node code. Portal hosts the repo (it is the repo); nodes
pull.

## VRAM sharing policy (portal position, forge ask)

RainbowBlock is an image node: GPU is for images by default. Router
presets stay -ngl 0 for >8 GB filesets (gpt-oss-20b cannot fit; big MoEs
stay CPU by PCIe-3 arithmetic). Small Q4 chat classes (4b ~2.6 GB, 8b
~5 GB) MAY run -ngl 99 (measured 128/75.9 t/s = 6.6x CPU lane) when
granted a VRAM lease: render queue idle + within margin + TTL'd; the
router's 900 s sleep returns VRAM at rest. The gateway's stickiness + the
lease ledger arbitrate; no service free-loads the GPU.
