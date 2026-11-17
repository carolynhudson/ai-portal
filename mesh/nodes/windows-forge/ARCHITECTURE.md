# RainbowBlock AI node - architecture (2026-11-12)

Owner brief: bring coherence, stability, reliability to the forge services;
they are the compute services the future meshd controller (Rust, AI_MESH_
PROPOSAL.md) will manage. Personal-PC law: the machine belongs to its user -
idle means GIVING MEMORY BACK, not hovering.

## 1. What runs (measured 2026-11-12, audit_forge.ps1)

| service | port | what | auth (measured) | gate |
|---|---|---|---|---|
| comfyui | 8188 | ComfyUI 0.43 engine, 2080 Ti | none | firewall: portal IP |
| forge-mcp | 8096 | MCP gateway + supervisor (python) | none | firewall: portal IP |
| llama-router | 8082 | llama.cpp router, 7-model library | bearer (no key = 401) | firewall: portal IP |
| opencode-agent | 24096 | remote harness (opencode serve) | NONE ENFORCED (see 3c) | firewall: portal IP |
| llama-prose | 8081 | RETIRED 2026-11-12 | bearer | was portal IP |

Idle cost after this pass: llama-server procs 0 GB resident (router cold,
models unloaded), python ~1.5 GB (engine, models freed on idle), opencode
~0.4 GB. 48+ GB RAM free at rest. VRAM: engine keeps the process, models
return to the user via /free after the idle window.

## 2. Root causes of the portal-side pain (measured, not guessed)

a. **opencode "access key issues"**: two layers. (1) The launcher scraped a
random per-boot password from the server log - the portal could never hold a
stable key. (2) MEASURED: opencode-cli 2.2.7 `serve` enforces NO auth on ANY
endpoint (all 200 with no credentials and with a wrong one). Keys were
theater; the firewall is the real gate. Fixed: launcher now pins
OPENCODE_SERVER_PASSWORD to the shared subagent token (stable credential,
forward-compatible with builds that do enforce), agent_server.json records
the stable form, and the honest contract is documented.
b. **"router shows no models"**: the router requires the bearer key; an
unkeyed GET /v1/models is 401 (not an empty list). A portal probe without
the token reports exactly the observed complaint. Now: one token for every
surface (C:\forge\llama\subagent_key.txt), 7 ids listed keyed.
c. **RAM pressure during renders**: the router preloaded nemotron (~25 GB)
at every boot AND the prose lane held ~20 GB 24/7. Fixed: cold start,
native llama.cpp idle (sleep 900 s + 16 GiB memory margin LRU), prose lane
retired (router id Qwen3-30b-A3B-Instruct replaces it).

## 3. Target architecture

### 3a. Layout (code-sprawl law: one tree, known corners)
```
C:\forge\
  etc\services.json     the registry (single source of truth, meshd-ready)
  kit\                  ops scripts, forge-mcp, workflows, out\
  llama\                llama binaries, router\(presets.ini, models\ library),
                        subagent_key.txt (THE token)
  ComfyUI\ venv\        engine + python
  logs\                 every service log, stamped
  dev\                  meshd Rust workspace (RUST_ENV.md)
  share\                shelf (STOPped filesets, never in the library)
```

### 3b. Roles
- **forge-mcp = the node's transaction gateway + supervisor.** Routes a
  request to: a pure function (plate/scale/verify), the engine (render
  contracts), the router (LLM steps in a chain, LOCAL only), or file
  pickup. It owns idle policy: ComfyUI /free after comfy_idle_s (supervisor
  .json), reports supervisor state in forge_health. It reports WHAT WENT
  WRONG with measured values (engine exception payloads, queue state,
  timeouts -> interrupt + purge).
- **llama-router = the one LLM surface.** Cold start; loads on
  chat(model=<id>); sleeps 15 min idle; LRU-unloads under a 16 GiB RAM
  margin; presets tune each model (ctx, threads, MTP, mmproj). Forge LLM
  calls stay on 127.0.0.1; only the portal reaches :8082.
- **opencode-agent = remote management.** Runs in the kit workspace with
  the generated portal-synced config: sessions compute on HALO's resident
  model (zero local llama load), tools act on this box. Stable credentials.
- **comfyui = the engine.** Keep-process, free-models lifecycle.

### 3c. Auth reality (measured) + direction
Firewall (per-port, portal-IP-only) is today's gate; bearer token is real
only on the llama router. opencode enforces nothing (2.2.7). DIRECTION:
meshd (Rust) becomes the front door - one token checked in one place,
per-service proxying, lease ledger, and idle governance (a render lease can
force model sleep; a user-session signal can quiesce everything). Until
then: single shared token + firewall + docs honest about the gap.

### 3d. meshd-ready contract (already true today)
services.json rows carry: id, launcher, scheduled-task name, port, bind,
auth, health URL, idle policy, logs, status. A controller can enumerate,
probe, start (task), stop (task/pid), and enforce idle from this file.
The verbs meshd needs map: mesh.status -> health probes; mesh.nodes -> one
node row; leases -> supervisor policy hooks; quarantine law -> mesh-side.
SHAREABILITY across heterogeneous nodes (wire / advertisement / assembly
planes, service cards, contract packs, placement): C:\forge\dev\meshd\
SERVICE_MESH.md. forge_caps advertises this node's image services per
contract (measured), the way the router lists models.

## 4. Update/manage recipes (day-to-day)
- status: `tools\audit_forge.ps1` (everything above, one shot)
- forge-mcp: `restart_forge_mcp.ps1` | router: MCP forge_router_start/stop/
  switch, or task ai-forge-llama-subagent | agent: task ai-forge-opencode-agent
- models: drop a folder under C:\forge\llama\router\models\<id>\ (+ optional
  [id] section in presets.ini) - router lists it; restart to pick up dirs.
- prose restore: `retire_prose.ps1 -Restore`
- ALL .ps1 edits -> run parse_check.ps1 (must print PARSE OK x N).

## 5. Known gaps / next steps
1. Portal-side: register the ONE token for 8082 (keyed /v1/models), retire
   any 8081 client config, retest the MCP paths that errored (interop was
   fixed 2026-11-11, addendum j).
2. Ternary-Bonsai PQ2_0 is unloadable on stock llama.cpp (owner's
   leave-vs-shelf call).
3. meshd phase 1: Rust gateway implementing 3c direction, registry-driven,
   the hello-svc probe already proves the SCM/toolchain path.
4. ComfyUI process restart (model-free idle keeps ~1.5 GB python; engine
   full-stop on deep idle is possible but logon task + start cost ~1 min).
