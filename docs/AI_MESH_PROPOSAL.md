# AI Mesh — the mesh of machines (proposal, 2026-11-11)

Goal (owner): every machine on the home network (halo, RainbowBlock, the
MacBook Pro, future nodes) runs a **mini-portal** — a small secure service
with a plain human status page and a real machine-facing interface. Agents
use it to enumerate nodes, read state and utilization, load/unload engines
(agentic harnesses, diffusion/transformation engines, task harnesses), and
choose nodes without hurting work already running. The portal (halo) is the
anchor: monitor, code repo for the network projects, knowledge store, model
inventory, and the agents' social workspace. The daemon family is **meshd**;
the image-forge pattern is the founding prototype and its brittleness is
the specification.

This is the machine mesh: MESH_PROPOSAL.md (portal mesh P1) carries shared
*knowledge*; this proposal carries shared *compute*. P1's laws port whole:
observations free, remote mutations land quarantined until the owner
promotes; halo is the shared repository of work and truth.

## 1. Why now — measured, not hypothetical

- **VRAM collisions are real damage**: the 21 GB qwen2512 chain beside the
  resident router kernel-OOM-killed the forge engine (6.14.1zv). "Don't
  hurt running tasks" is prose law; it wants a mechanism.
- **Storms happen** (6.14.1zz): RainbowBlock died, halo rebooted, /tmp
  wiped mid-arc. Trust anchors need UPS boxes (the Pi5 pair), nodes need
  durable state, and the fleet needs a version/state table that survives
  the night.
- **The prototype works**: forge-mcp (8096) answered render/verify/plate/
  scale/router over the wire; portal + bench + local harness collaborated
  across two OSes with zero shared filesystem. The shape is proven.
- **The prototype is brittle**: per-box firewall rules by IP, static key
  files (subagent_key.txt), task-at-logon fragility, files carried by the
  owner's hands, names that die off-LAN (mDNS). Each brittleness is a line
  in the design below.

## 2. Architecture (four layers, decided in this order)

### 2.1 Fabric — Headscale on the Pi5 pair
Self-hosted Tailscale control server on the UPS-protected Raspberry Pi 5
pair (they already own the network-critical jobs: pihole DNS/DHCP). Every
node joins a WireGuard overlay; peers connect direct on the LAN at local
speed. **The binding law, from day one: every AI service binds to the
overlay interface only, never the Wi-Fi/ethernet NIC.** A MacBook at a
coffee shop has the mesh up (outbound dial) and zero AI ports on the
coffee-shop network — the ports do not exist there. Node names ride the
tailnet (roaming-proof), replacing `*.local` mDNS. HA: Headscale active on
one Pi, CA/backup on the other; a home network's trust tier must be the
thing that doesn't die in the storm.

### 2.2 Trust — a private CA on the Pi5 pair
step-ca issuing **short-lived (24 h, auto-renew) mTLS certificates** per
node and per agent identity. Mesh connections are mutual TLS; identity =
cert, not IP. Consequences: static API keys retire (subagent_key.txt is
the first), per-IP firewall allowlists become the fallback, agent
capabilities ride cert extensions (scoped roles), revocation is a clock
away. The Pi pair (8 GB, NVMe, UPS) is overqualified for a CA.

### 2.3 Node daemon — meshd (one compiled binary per platform)
A single static executable per OS, unprivileged and user-scope on every
platform: systemd user unit (Linux), scheduled task at logon with a
Limited principal (Windows — the proven forge pattern, no admin, no
services), LaunchAgent (macOS). It never enters sudo/Administrator/TCC;
OS barriers are respected by never crossing them. Surfaces:

- **Human page**: deliberately dumb — state, services, leases, nodes,
  re-check. Read-no-fear, the Health-page law.
- **Agent API**: MCP streamable-HTTP, the uniform mesh language (the 8096
  and 8090 doors prove it). Core tool sketch:
  `mesh.nodes()`, `mesh.status(node)` (services, leases, free ram/vram/
  disk/thermal, queue depth), `mesh.lease(node, {vram, ram, ttl}, job)`
  → granted|queued|denied, `mesh.place(need)` (ranked nodes — control
  plane), `mesh.service(node, action, id)` (user-scope instant; act.node
  quarantined).
- **The lease ledger is the mechanism** behind "agents avoid hurting
  tasks": ask before you load, TTL-bounded so dead agents can't hold
  VRAM hostage, utilization + reservations in one read.
- **Supervisor**: starts/stops/restarts the node's engines (router,
  ComfyUI-class, harnesses) with the portal's job discipline (receipts,
  audit, no silent death).

Privilege tiers: 0 observe (any mesh member) · 1 lease (any agent,
capacity-bounded, expiring) · 2 act.user (scoped cert, user-scope
services) · 3 act.node / system-reach (quarantined, owner promotes — the
P1 law, ported).

### 2.4 Control plane — the portal, extended
halo keeps its jobs: registry (nodes, versions via heartbeat carrying build
sha — the healthz identity law), placement (`mesh.place` answers "who can
run a 21 GB chain right now?"), audit, fleet version table, quarantined
mutations. The Workroom/bench is already the social workspace; the mesh
makes it a workspace with machines, not just notes. **Model files: the
portal is the registry/inventory (bytes live on nodes; hub-cache fences
per memory 137; migration law governs the bytes),** so the portal never
becomes an engine host.

## 3. Path (owner's phasing, 6.14.1zz+)

- **P0 — forge-like kits, portal-anchored (now, near-zero new code).**
  Each destination system gets a local opencode harness + an onboarding
  kit: bench + door (MCP), portal as KB/repo/anchor, laws file (ASCII
  launchers on Windows, no-admin tasks, durable state dirs, prompts in
  repo not /tmp, size-verify after every fetch). The image-forge build is
  the template; its brittleness list §1 is the kit's changelog.
- **P1 — fabric + trust**: Headscale on the Pi pair, CA, clients on
  halo/RainbowBlock/MacBook; binding law enforced in service templates;
  WOL/MAC registry for storm recovery. Language-free; storm-proofing.
- **P2 — meshd v1**: status + leases + supervisor MCP surface; one Rust
  binary per OS; portal registry page; static keys start dying.
- **P3 — service transition**: forge-like user-space doors retire into
  meshd-managed services; the portal becomes monitor + repo + model
  registry + social workspace over the real mesh. Expect evolution, not a
  cutover.
- **P4 — MacBook as full node**: roaming law proves itself; the M4 Max
  (MACBOOK_AI_PLAN.md) is the candidate AI-processing MVP and doubles as
  the macOS build node for meshd.
- **Later**: mixer/voice on mesh primitives (voice = Rust mesh-family
  project: cpal + whisper-rs + ort, tailnet offload); placement scheduler
  refinement.

## 4. Language decision (meshd family)

Go: fastest first daemon; two-env-var cross-compile from halo; GC. Rust:
memory safety, no GC (real-time audio), the AI-infra crate family
(candle, burn, tract, ort, whisper-rs, wgpu = Vulkan/Metal inference on
consumer GPUs — one story across 2080 Ti, M4 Max, Strix iGPU), rustls.
Reality check: no Rust replaces PyTorch for development; engines stay
Python/CUDA; the mixer is orchestration + cost model, not a torch
rebuild. **Decision: Rust for the mesh family** (daemon = deliberately
tame on-ramp: tokio/axum/rustls + MCP SDK; voice = the anchor project
Rust genuinely wins), Python stays in its lane (portal, engines), Go kept
for throwaway binaries. Known friction: macOS cross-compile from Linux is
not clean — solved mesh-style by building on the Mac once it joins.

## 5. Open questions (park here, decide in P1/P2)

Lease TTL policy and preemption (who evicts whom, at what notice) · cert
distribution to opencode harnesses (agent identity per harness vs per
human) · audit volume/retention · Headscale HA pattern (single writer is
fine at home scale; the CA is the real HA requirement) · Pi-hole role:
LAN DNS stays; tailnet MagicDNS covers roaming; blocklist interplay ·
per-node storage budgets for model bytes (halo migration law as the
template) · storm ops: WOL registry, logon-task hardening, wake-on-LAN
fallback vs UPS-brownout policy.
