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

## meshd internal protocol (owner design, refined 2026-11-13)

**Platform seams + build relay.** meshd is Rust with a `platform/` seam:
one trait per OS-owned capability (service control, network binding, OS
error/signal surfaces, resource probes, user-session/input-idle detect),
cfg-selected implementations (`#[cfg(target_os=...)]`, feature flags for
optional deps like nvml/windows-service). The relay: build+test on halo
(Linux first), hand to the forge agent (Windows -- gnu route proven,
windows-service probed), then the MacBook (macOS, and the Mac builds its
own binary). The seam ships a **platform-conformance test suite**: the
same tests must pass on every platform, so a platform is "done" when the
suite is green, not when it compiles. Updates = another relay round.

**Two skins, one protocol.** First-party Rust services link the mesh
library. Third-party engines (ComfyUI, llama.cpp, opencode, anything)
cannot link Rust -- they get a **sidecar adapter** (a meshd `--service`
role: supervise + speak mesh for any executable over stdio/HTTP). The
abstraction is the protocol, not the linker; this is how any-language
services behave mesh-native.

**Registration handshake.** A service hello declares: identity, its
contract card (the caps schema above), flow-control capacity (message
types accepted, concurrency per type, value ceilings), port needs, and
resource rules. The node controller's registry auto-populates from hellos
-- services self-describe, the node reports measured, mesh.status
aggregates.

**Message model.** Classes control > health > telemetry; priority heap
at both ends with per-class FIFO and a documented starvation policy;
backpressure honors the declared capacity. Local wire = the same JSON
envelope as the mesh (no bespoke RPC; MCP at the edges).

**Port ledger.** Services declare port needs; the controller assigns
from a pool, records them in the registry, and directs binding to the
permitted interface (overlay-only law). Static pool first, DHCP-style
dynamic later if ever needed.

**Resource signals.** The controller monitors (sysinfo/nvml/input-idle
per platform) and emits `resource.busy{gpu,ram,...}` / `resource.safe`
events to subscribed services. Availability for the task router =
declarative per-service policy (min free VRAM/RAM, CPU ceiling,
user-session yield) AND the live signal. The personal-PC law becomes a
protocol: when the human arrives, AI yields.

**Launch/recovery discipline.** No launch under low-RAM; blackout delay
+ exponential backoff with a circuit breaker (crash-loop -> stop, mark
failed, report, human); no restart while the node controller is
unreachable. The storm is the founding test case.

**Store-and-forward.** Durable node outbox (SQLite) with per-node seq +
ack; the Pi pair relays/buffers, the portal is terminal. Retention is
classed: telemetry drop-oldest, control/audit never dropped. **Silence =
departed:** leases TTL-expire and the node leaves placement while
disconnected -- store-and-forward carries facts, never authority; a
reconnected node re-registers.

**Secrets through the secured pipe.** With P1 mTLS (Pi CA), the node
controller is the node's broker: secrets delivered at runtime, injected
into service launch, never on disk, redaction law; on-demand privileged
capabilities (e.g., a changes-allowed harness loaded only for a trusted
request, torn down at lease end). Honest limit: a bearer token is
visible to the process that uses it -- "only the requestor knows" is
enforceable when the requestor-side client uses the secret, not the
node; the mesh arbitrates custody, not invisibility.
