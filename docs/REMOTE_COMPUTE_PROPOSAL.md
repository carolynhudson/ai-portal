# Remote compute — borrowed machines as protected model backends (proposal, 2026-10-27)

Status: **future feature, design-refined, not scheduled.** Owner's framing:
"like the image forge MCP server, but a protected llama.cpp router" — for
sub-agents, extension of the portal's abilities, and model testing the Halo
box cannot host. The Windows box (RainbowBlock.local, 2080 Ti 11 GB) is the
same family; the partner's personal MacBook Pro (M4 Max class, 128 GB unified
memory) is the new machine in scope. Neither borrow is a server, and the
design treats that as a hard requirement, not a caveat.

## 1. Measured ground

- **Halo box (the portal host).** Resident coding agent consumes tens of GB;
  `MemAvailable` swings 10→35 G across the day; the fit gate refuses loads
  whose arithmetic (weights + KV + overhead vs `MemAvailable` + swap) doesn't
  close. The router's largest resident family — Swift/ISTA Flash-Next IQ3_S —
  is an **83.74 GB two-shard MoE** (arch `qwen4exp`, size_label `512x56B`,
  ctx 262144, mmproj CLIP 449M vision). A second big model to *test* beside
  a resident agent is exactly the arithmetic that does not close at home.
- **MacBook Pro, M4 Max, 128 GB.** Unified memory with ~400-500 GB/s
  bandwidth. An 84 GB IQ3_S model is *comfortable* there — resident, with
  30-40 GB of headroom for KV cache, the OS, and the partner's apps. Large
  MoE models run well on Apple silicon bandwidth; dense 70B-class 4-bit is
  viable (slower per token, roomier than anything on this box).
- **Engine note (measured on this box, applicable there):** the portal's
  OpenAI-compatible surface is llama.cpp's server. On macOS, **llama.cpp
  Metal builds (brew) serve GGUF** and speak the same API the portal already
  consumes (the relay is a sibling of the router — zero new client code).
  **MLX serves MLX-format models** (best local-chat performance, LM Studio /
  mlx-lm) but not the OpenAI API; an MLX instance joins the family behind a
  thin shim, not as the relay's core. GGUF-first keeps one protocol.
- **Windows box (planned, IMAGE_FORGE_PROPOSAL.md "1c"):** ComfyUI over HTTP,
  `--listen` bound to LAN, firewall scoped to the portal IP, idle-stop,
  memory flags. It is the **image instance of the same contract** this
  document generalizes.

## 2. The contract: "relay" — any borrowed machine, one shape

A relay is a machine the portal may *borrow compute from* under five binding
requirements. ComfyUI-on-Windows and llama-relay-on-MacBook are both relays.

1. **Authed model endpoint.** OpenAI-compatible (llama.cpp `llama-server
   --api-key`, same key-file discipline as the portal router: per-relay key,
   0600, never in a URL). ComfyUI has no auth and is firewalled to the portal
   IP alone; the llama relay adds a key *and* the firewall scope. LAN bind,
   TLS optional (same trust domain; keys make it defense-in-depth).
2. **Control plane.** Small status/load/unload endpoints the portal drives:
   `status` (model, slots, memory headroom, presence state), `load(name)`,
   `unload`, and a `/free`-analogue mirroring the forge's
   unload-after-every-job contract. The portal's `model_swap` discipline
   applies remotely: **no swap while a session on that relay is mid-turn.**
3. **State probe.** The fit gate must do its arithmetic **on the relay,
   remotely**: memory headroom (`vm_stat`, `sysctl hw.memsize`, swap usage,
   `memory_pressure` on macOS; the MemAvailable analogue), not `used`. A
   relay-side refusal names its arithmetic, same as at home.
4. **Idle contract.** Models unload after a job, on idle timeout, and under
   pressure (auto-preempt: batch jobs pause, big models unload, *always*
   yielding to the human). Load happens on demand, never resident-by-default.
5. **Etiquette (the "not a server" clause).** See §5. This is what makes
   borrowing acceptable to the person who owns the machine.

## 3. Security

Portal-IP-scoped inbound (macOS application firewall / `pf`; Windows rule as
shipped in the forge kit). Per-relay bearer keys for the model plane
(llama-server enforces `--api-key`); the Comfy relay stays keyless+firewalled
(its own limitation, recorded). **The relay exposes models and status only —
zero file access, in both directions.** The control agent on the Mac is
outbound (it reports state to the portal; no inbound SSH, no screen, no
filesystem). Every relay load and every job on it is an audit row on the
portal.

## 4. "Two AI connections for one prompt" — the honest answer

No mainstream harness fuses two backends into a *single* completion; opencode
binds one `{providerID, id}` per session (model bound at session creation —
measured, 2.0.29). The useful shapes are above the protocol, not inside it:

- **Model-as-tool.** Wrap the relay in one MCP tool
  (`consult(role, prompt, model)`): the resident agent keeps the prompt,
  the relay answers sub-questions (translation, roleplay, judge scores). One
  prompt, two brains; the tool result is content.
- **Sibling sessions.** Launch sessions bound to relay models (the
  projects-launch model binding already exists): the tournament runs
  *character sessions on the MacBook* while the *judge/analyst session runs
  on Halo's resident model*. This is the ai-voice-chat case exactly, and it
  needs no harness change.
- **Router federation.** A LiteLLM-proxy or llama-swap style gateway in
  front; the portal consumes one endpoint that fans out. Adds a component;
  the first two shapes are cheaper.

## 5. Etiquette spec (the relay's distinguishing tier)

- **Presence tiers, measured on macOS:** *green* = screen locked / display
  asleep / HID idle > N minutes (`ioreg` HIDIdleTime, loginwindow state) →
  big loads allowed. *Amber* = unlocked, user active in a terminal/IDE →
  small models + light jobs only. *Red* = display shared / call in progress /
  on battery → no loads; running jobs pause. Tiers are read by the relay
  agent and gate every `load`; the portal's fit gate refuses with the tier
  named ("MacBook is in a call").
- **Headroom gate, remote arithmetic:** load is allowed only if weights +
  KV(requested ctx) + overhead < headroom (free + compressible + swap
  margin) minus a **reserved floor for the human** (owner-set, e.g. 24 GB).
  Pressure during a job (`memory_pressure` warnings, swap spikes) →
  auto-preempt: pause batches, unload big models, notify, audit.
- **Sleep/energy:** `caffeinate` scoped to the job window, released with it;
  battery ⇒ no big loads at all; "not plugged in" reads as amber floor.
- **Kill switch + voice:** a one-click menu-bar pause on the Mac (a tiny
  local control: flips a flag the relay gate reads), plus an osascript
  notification at job start: "Portal loaded Flash-Next (84 GB) on this Mac,
  ETA ~40 min — pause: click". The owner of the machine can always see what
  is loaded, when it will unload, and stop it, in one place.
- **Quiet hours** (owner-set windows where the relay is off, period).

## 6. First use case, spelled out: the roleplay tournament (ai-voice-chat)

The problem it solves: testing candidate roleplay models (e.g. Flash-Next
variants, Qwen3.5-9B-RolePlay, the 27B family, the resident MoE) requires
both the harness/analyst **and** a chain of evaluated models; Halo cannot
host both the coding agent and the test chain. With the relay:

1. Relay loads candidate #1 (`load`, gate prints the arithmetic, presence
   green).
2. The portal's tournament kit launches character sessions bound to
   `macrelay/<candidate>` (sibling sessions, §4); scripted scenes probe
   character fidelity, coherence, instruction-following, safety.
3. The **judge** runs on Halo's resident model (locality: it grades against
   the project's rubric, in-repo); scores + transcripts land in the model
   index's capability rows (the `modelmeta`/INDEX pipeline already owns
   per-model facts — capability-by-measurement joins provenance-by-sniff).
4. Candidates swap on the relay between rounds (closed-session swap rule,
   remotely); the voice-chat engine's tuning (sampling rows, prompt
   templates per personality) is edited between rounds by the *local*
   harness — the rapid co-evolution the owner described.
5. End: `unload`, audit, relay idle. The partner's machine returns to them.

## 7. Phases (when it's scheduled)

- **P0** — relay bring-up on the Mac: brew llama.cpp, `llama-server
  --api-key`, launchd-managed (manual-start, portal-keyed), portal service
  row (SERVICE_CONF mode, probes, Settings row, dashboard tab — the opencode
  tab pattern), fit-gate remote arithmetic.
- **P1** — etiquette: relay agent (state + presence tiers), menu-bar kill
  switch, notifications, quiet hours, pressure auto-preempt.
- **P2** — tournament kit: scene scripts, judge rubric, capability rows in
  the model index; ai-voice-chat tuning loop.
- **P3** — Windows box joins as the image + CUDA-text relay under the same
  contract (one abstraction, two machines); relay federation revisited if
  two+ relays make the sibling-session routing worth a gateway.

## 8. Decisions the owner owes (with the partner, before P0)

1. **The consent ritual.** The partner reads this document; they set:
   reserved floor GB, big-load tier rules, quiet hours, battery policy,
   kill-switch expectations. These are *config values in the portal*, not
   etiquette by memory.
2. Key custody + who can start relay jobs (the portal is single-operator;
   the relay serves the portal, not the household — say so in one line).
3. Model set allowed on the Mac (privacy: models the partner recognizes;
   GGUF-first; MLX shim later, if at all).
4. Max single load and max job duration defaults.
5. Whether the relay agent is a login item (recommended) and what it may
   *not* see (answer: files, screen, inputs — nothing; it reports memory,
   lock state, HID idle, and model state, period).

## 9. Honest limits

- A laptop is not a server: it sleeps, updates, travels. The design's answer
  is the relay contract (state probes, refusals that name their reason), not
  a promise of availability.
- Bandwidth: M4 Max is fast per-token on Apple silicon classes, not
  datacenter-fast; big dense models will feel like 10-20 tok/s. MoE models
  (the Flash-Next family) are the strength case.
- MLX models do not speak the relay's protocol without a shim; GGUF-first is
  a deliberate constraint that buys one uniform API.
- Presence detection is heuristic (HID idle, lock state, app signals) —
  tiers fail **toward the human**: unknown state reads as amber, not green.
- Two connections per prompt is not a thing; model-as-tool and sibling
  sessions are the real shapes. Do not build federation before two relays
  make it necessary.
