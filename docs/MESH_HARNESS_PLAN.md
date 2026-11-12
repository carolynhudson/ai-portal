# Mesh harness plan — shared harness config + heterogeneous compute (2026-11-11)

Goal (owner): (1) the network-visible opencode harnesses (halo, RainbowBlock,
later the MacBook) share a common config and behave like peers; (2) quantify
how clients like opencode can use a heterogeneous mesh for AI compute —
proxies, session stickiness, performance-aware balancing, sub-agent routing —
so the mesh shape can be planned against measured reality. Bounded
main-thread research: OpenCode V2 docs (providers, plugins/server) fetched
and quoted; router numbers measured live today. Part of the AI mesh arc
(AI_MESH_PROPOSAL.md); nothing here is a hard mesh requirement.

## 1. Forge router — live report (2026-11-11, first portal-side calls)

Endpoint RainbowBlock.local:8082, bearer auth with the subagent key file.

- **Auth + endpoint: works.** GET /models -> 200 with the bearer token.
- **Config drift found**: only `flint` registered post-storm-reboot;
  `nemotron` (the best-tested model, memory 137: 16.2 t/s via router, 21.1
  MTP direct, ThinkingCap) is gone from the registry. The MCP door validated
  honestly (forge_router_switch -> "unknown model id: nemotron (known:
  ['flint'])"). Filed to the forge; the mesh lesson: **a storm reboot is a
  config-drift event** — meshd must assert service state from templates at
  boot, and harnesses/registries must show drift, not silently degrade.
- **flint chat: works.** Load-on-chat confirmed (first call ~90 s includes
  cold load). Decode ~8-9 t/s (code 7.9, tool-call 9.4). flint is a thinking
  model: `reasoning_content` carries the thinking; `content` can be empty
  when max_tokens is tight — clients must budget or read reasoning_content.
- **Tool calls: work** (OpenAI `tools` round-trip verified through the
  router).
- CPU-only decode numbers: forge flint ~8-9 t/s. For contrast: nemotron
  class ~16-21 t/s (measured), MacBook MLX class 73.8 t/s @262K (plan doc),
  halo GPU router mid. **The mesh's speed spread is ~8x. That spread is the
  whole point of heterogeneous routing.**

## 2. Harness sync — peer harnesses from a shared brain (P0-buildable)

Facts from the V2 config docs: global `~/.config/opencode/opencode.json` <
ancestor project configs < `.opencode/` project configs, merged; config
watchers hot-reload. Facts from the plugin docs: server plugins can register
and live-reload **providers, models, MCP servers, skills, agents,
references, tools** via `ctx.*.transform` — config domains are programmable,
not just files.

Design — **base + overlay + sync plugin**:

- **Canonical base lives in the portal repo** (`harness/`): the shared
  opencode.json (mesh providers, MCP doors, plugins, agents, skills,
  instructions). The portal is the repo and knowledge store: it already owns
  this role for agents; harness config is agent knowledge.
- **Per-node overlay** = the node's global config: node id, local endpoints
  (127.0.0.1 vs LAN), paths, env names. Nothing secret in git; provider
  credentials use env references (`{env:MESH_KEY}` / integration
  connections), brokered in P1 (AI_MESH_PROPOSAL 2.2).
- **`mesh.sync` plugin** (the elegant mechanism): a server plugin per node
  that fetches the canonical harness manifest from the portal over the
  portal's door (HTTP), then applies it via `ctx.provider.transform` /
  `ctx.mcp.transform` / `ctx.skill.transform` / `ctx.agent.transform` and
  `reload()` on change. Peers stay in step over the wire; no file rsync
  ritual; drift is visible (the plugin reports version mismatch — same
  law as fleet versions on the control plane).
- Storm lesson applies: sync asserts desired state continuously (nemotron
  disappearing = exactly the class of drift a sync loop surfaces).

## 3. Heterogeneous compute for clients — quantified design

The owner's instinct (proxy + session stickiness + performance-based
balancing + sub-agent steering + a self-managing provider interface) is
feasible end to end; the V2 plugin/provider surface carries it. Two layers:

### Layer 1 — the mesh gateway (the brain every client can use)

One OpenAI-compatible endpoint (portal-side; P2 meshd can federate it).
Per the providers doc, opencode registers a gateway exactly like the
"company gateway" recipe: custom provider, `@opencode/ai/providers/openai-
compatible`, per-model `modelID` mapping. Any OpenAI client can use it —
opencode, portal jobs, MCP tools, harnesses — which is why the gateway, not
a per-client plugin, is the core. Responsibilities, each with a measured
rationale:

- **Capability registry** (model -> nodes that can serve it): not every
  node runs every model (IQ3_S needs llama.cpp; MLX quants are separate
  files; VRAM/RAM ceilings differ). The registry is the portal model
  inventory extended with node custody.
- **Session stickiness**: bind sessionID -> node. Two measured payoffs:
  (a) the llama.cpp routers run `--models-max 1` — swapping flint<->nemotron
  costs 8.6-40 s (measured, memory 137); stickiness prevents one session
  thrashing another's model; (b) llama.cpp's KV/prompt cache is node-local:
  stickiness turns multi-turn prefill into cache hits.
- **Performance-based balancing**: score nodes from meshd/door status
  (queue depth, free RAM/VRAM, EWMA t/s, thermal). The lease ledger
  (AI_MESH_PROPOSAL 2.3) is the admission gate; the gateway is its client.
- **Main-thread protection / sub-agent steering**: requests carry class
  headers (see Layer 2). Gateway rule: `X-Mesh-Class: subagent` prefers
  nodes NOT serving the requesting harness's main session (the gateway knows
  the binding from stickiness). This is the owner's "route the sub-agent
  call away from the system running the main thread" — one header plus one
  rule, once, for every client.
- **Swap-aware model pinning**: models-max-1 routers mean the model IS a
  mutex. Gateway arbitrates the mutex (lease per model-slot), queues
  requests behind it, and exposes queue state — the collision class
  (render-vs-router OOM, model thrash) becomes scheduling, not accidents.

### Layer 2 — thin opencode plugin (per harness, small)

A server plugin (the docs' hooks cover every need):

- Registers the gateway provider dynamically from the mesh registry
  (`ctx.provider.transform` + reload): models appear/disappear as nodes
  come and go — the fleet shows up in `/models`.
- `ctx.session.hook("context")` stamps outgoing requests: `X-Mesh-Session:
  <sessionID>`, `X-Mesh-Agent: <agent>`; child-session detection (sub-agents
  are child sessions; `ctx.session.get` shows parenthood) sets
  `X-Mesh-Class: subagent`. Aux requests (title/compaction) have their own
  hooks -> `X-Mesh-Class: aux`, routable to cheap nodes.
- Exposes `mesh_status` / `mesh_place` tools (`ctx.tool.transform`) so the
  AI can query and choose nodes/models itself — the self-managing interface
  the owner asked for, built from documented primitives.

### Why not plugin-only?

A plugin speaks for one harness only; the portal's jobs, MCP tools, and the
forge's own clients need the same routing. Gateway = the mesh's OpenAI door;
plugin = the per-harness voice that supplies context the gateway cannot see
(session, agent, class). Both, layered.

## 4. Quantified wins (measured today, to be extended per node)

- Swap thrash avoided: 8.6-40 s per unwanted model swap (models-max 1).
- Speed spread: sub-agent work moved from forge CPU (8-9 t/s) to a GPU/Metal
  node (16-74 t/s) = 2-9x on offloaded legs.
- KV-cache reuse: sticky sessions skip full-prompt re-prefill on
  multi-turn (llama.cpp cache; magnitude to be measured in P2).
- Collisions prevented: lease-admitted loads end the render-vs-router OOM
  class (the founding incident).
- Fleet drift surfaced: storm reboot lost nemotron; sync/assert loops catch
  the class mechanically.

## 5. Open questions (decide in P1/P2)

Auth at the gateway (P1 broker: short-lived tokens, no static keys) ·
streaming passthrough (SSE through the gateway; timeouts per the providers
doc: headerTimeout/chunkTimeout) · model-listing semantics (gateway
/v1/models synthesizes the registry) · stickiness table persistence across
gateway restarts · KV-cache locality vs. load-balance ideal (cache affinity
vs. least-load: weight by queue depth) · Mac node as gateway secondary
(roaming: gateway must live on an always-on box = halo/Pi, node agents dial
in).
