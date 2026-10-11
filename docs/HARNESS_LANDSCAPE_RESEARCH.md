# Harness landscape: what a web-native harness means, and what the portal should build on

Written 2026-10-11, after the PI WEB spike (`docs/PI_HARNESS_RESEARCH.md`), to answer the operator's
question before the portal commits to a harness: *"extensions advertise alternate harnesses — so pi must
sit on some shared interface. Is there a web-native harness or harness library we can integrate with the
portal more easily, that also accepts this npm extension ecosystem, so the web interface is the harness
and not a portal into pi?"*

Short answer: **the shared interface exists, it is four layers, and the top one (ACP) is the portal's
answer.** The portal can be an agent client in its own language (Python, measured on this box tonight),
with harnesses — pi, opencode, Claude Agent, Codex, Cline, OpenHands, Goose — plugged in behind it.
Nothing here requires the portal to become a Node app, and no harness owns the portal's UI.

## 1. The four shared interfaces

The "copy the OpenAI interface" intuition is correct, and it is only the first layer.

| layer | interface | what it makes portable | who speaks it here |
| --- | --- | --- | --- |
| model | **OpenAI-compatible chat completions** | any model server into any harness | the portal's llama.cpp router (`:8080`) speaks it; opencode's `@ai-sdk/openai-compatible`, LangChain, gptme, most harnesses |
| tools | **MCP (Model Context Protocol)** | tools/servers across harnesses | pi (`pi-mcp-adapter`), opencode (`opencode mcp`, `POST /mcp`), ACP (MCP servers in `session/new`), Claude/Codex/Cline/Gemini |
| instructions | **Agent Skills (`SKILL.md`)** | skills across harnesses | open standard, 40+ platforms; one skill runs unchanged in Claude Code, Codex, Cursor, OpenCode, OpenClaw, Hermes, pi. Aug 2026: **Agent Plugins** (AWS, Cursor, Microsoft, OpenAI, Vercel, Google) merges MCP + Skills into one portable extension format |
| client | **ACP (Agent Client Protocol)** | *the harness itself becomes pluggable under one UI* | 41 agents listed (OpenCode, Codex CLI, Claude Agent, Gemini CLI, Cursor, Cline, OpenHands, Goose, Kimi, Qwen Code, Copilot preview, **Pi via `pi-acp`**); clients: Zed, JetBrains, Neovim, Emacs, marimo, Acp UI (browser), Casper (browser) |

ACP is the one that answers the question as asked: it is JSON-RPC 2.0 with a `session/new` /
`session/prompt` / `session/update` lifecycle, **permission requests**, **config options** (model, mode,
thinking level), **usage reporting**, **session load/resume/fork**, elicitation (form + URL, adopted from
the MCP 2026-07-28 RC), and **Streamable HTTP + WebSocket transports** (experimental RFD; stdio is the
stable one). SDKs: TypeScript `@agentclientprotocol/sdk` (Apache-2.0, 0 deps, latest 1.8.0), **Python
`agent-client-protocol`** (0.12.1, module `acp`, `requires-python >=3.10,<3.15` — this box is 3.14).

## 2. What is web-native, and how native

| candidate | shape | measured here? |
| --- | --- | --- |
| **ACP + harness adapter** | protocol; the portal is the client, harnesses are subprocesses or WS peers | **yes, in Python**: drove pi via `pi-acp` (initialize → session/new → model switch → prompt → tool calls → `session/load` after the process died) |
| **opencode** (`opencode serve`, `opencode web`, `opencode acp`) | MIT. Server-first harness: TUI is a client of an HTTP server with an OpenAPI 3.1 spec at `/doc`; sessions, messages, providers, MCP, LSP, file/VCS endpoints, SSE events, basic auth, `--cors` for browser origins; plugins (JS), agents, native `skill` tool | **yes**: ran a session on the portal's router, killed the server, restarted, and resumed the session |
| **PI WEB** (`@jmfederico/pi-web`) | MIT. Web app + `pi-web-sessiond` daemon; the agent lives in the daemon | yes (earlier spike, `docs/PI_HARNESS_RESEARCH.md`) |
| **LangGraph** (+ Agent Server, langhost, skein-js) | MIT. Orchestration library with **durable execution**: threads, checkpoints, runs resumable from exactly where they stopped; interrupts (human-in-the-loop); Python + JS SDKs; `langgraph dev` in-memory server; the full Agent Server needs Postgres + Redis | registry only (1.2.14, `>=3.10`); not run |
| **Letta** | server for stateful agents with persistent memory + REST API + self-host + an ACP adapter (`@letta-ai/letta-acp`) | registry only (0.34.10) |
| **pydantic-ai**, Inngest AgentKit, Vercel AI SDK, AG-UI/CopilotKit | agent libraries and web-UI protocols; not coding harnesses (no shell/edit/session semantics) | registry only (pydantic-ai 2.55.0) |

Not "web-native" in the coding-harness sense, and not needed: Aider, gptme, CrewAII/AutoGen (chat UIs
for demos, not project-scoped coding sessions with tool permissions).

## 3. Measurements from this box (2026-10-11, night)

### 3.1 ACP in the portal's own language, against the portal's router

Installed `pi-acp` 0.0.34 (MIT, 3 deps, node ≥20, 23 releases since 2025-12-20) — it bridges ACP over
stdio to `pi --mode rpc` — and `agent-client-protocol` 0.12.1 in a uv venv. A ~90-line Python client:

- `initialize` → `session/new(cwd=/tmp/spike-proj)` returned **config options including a model option
  with 92 models, 40 of them llama.cpp-shaped**, and thinking levels; `list_sessions` returns sessions
  with titles and `updated_at`.
- `set_config_option(config_id="model", value="llama.cpp/ukisai/Swift-1.5-…:IQ3_XXS")` → accepted,
  `current_value` changed. **No `/login` ritual, no "last item in the list", no reload-and-hope.**
- `prompt` → `stop_reason: end_turn`; update stream observed: `AgentMessageChunk`, `ToolCallStart`,
  `ToolCallProgress` ×4, `AvailableCommandsUpdate`, `CurrentModeUpdate`, `ConfigOptionUpdate`,
  `SessionInfoUpdate`, **`UsageUpdate {used: 19403, size: 128000}`**.
- Kill the adapter (the harness process dies) → a new client **`session/load`** replays the transcript
  (`UserMessageChunk`, `ToolCallStart`, `ToolCallProgress`, `AgentMessageChunk`) and the agent answers
  the recall question exactly: *"You asked me to run exactly `echo SPIKE_ACP_OK`."* pi's own session
  files plus `~/.pi/pi-acp/session-map.json` are what make that work.
- `pi-acp` limits, stated by its README: no `fs/*`/`terminal/*` delegation (pi reads and runs locally —
  exactly what we want), MCP params are accepted but not wired through the adapter (pi's own MCP
  extension covers that), assistant stream has no separate thought channel, "expect some minor breaking
  changes", development centred on Zed.

`UsageUpdate` matters on its own: the portal gets a **context meter** per turn. The operator's "context
compaction bug" currently forces a new session with hand-written documentation; a context meter lets the
portal say *when*, and lets the handoff be a logged checkpoint at a chosen moment rather than an
accident. The harness's own compaction is the same in every harness — this is a local-model,
128k-context problem, not a pi-only one.

### 3.2 opencode as a server-native harness

Installed `opencode-ai` 1.19.0 (MIT, npm wrapper with 0 runtime deps, 12,241 versions since 2025-05-31,
latest the same day as this writing). A 12-line `~/.config/opencode/opencode.json` with
`@ai-sdk/openai-compatible`, `baseURL: http://127.0.0.1:8080/v1`, and the router's 16 ids:

- `GET /global/health` → `{"healthy":true,"version":"1.19.0"}`; `GET /provider` →
  `connected: [huggingface, opencode, llamacpp]`, our provider with **16 models** (opencode also ships
  its own `llama` provider with 7, and community llama.cpp plugins auto-detect servers on common ports
  and query `/v1/models`).
- `POST /session {title}` → `ses_…` carrying its `directory`; `POST /session/:id/prompt_async` → `204`;
  the agent ran the bash tool, answered **`DONE`**, and `step-finish` reported
  `tokens {total: 8253, input: 21, output: 27, cache read: 8205}, cost: 0` — token accounting on the
  local router, for free.
- Killed the server (the portal-restart analogue) and restarted it in a *different* directory:
  `GET /session?directory=/tmp/spike-proj` lists the session, `GET /session/:id/message` returns the
  **3 restored messages** with their text, and a new prompt on it was accepted. Sessions are durable;
  a run in flight is not (it dies with the server).
- `opencode web` (server + browser UI), `opencode acp` (ACP server), `opencode mcp`, `opencode attach`.
  Server auth is HTTP basic (`OPENCODE_SERVER_PASSWORD`), browser origins need `--cors`.

### 3.3 PI WEB, for comparison (from `docs/PI_HARNESS_RESEARCH.md`)

Daemon survives a portal restart **with a run in flight** (measured); Q&A tool reaches the browser as a
form with previews and wakes the agent; sessions are pi session ids; model binding needed a generated
`models.json` plus a non-empty stored key; the web terminal pins `@xterm/xterm` 6.0.0, which has no
Kitty keyboard protocol, so `Shift+Enter` arrives as `Enter` (measured); pi's `/model` is not implemented
in that UI.

## 4. What each shape gives the portal, and what it asks for

|  | run survives a portal restart | session resumable after the harness dies | portal reads the state directly | model list from our router | questions reach the browser | context meter | what we must build |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **ACP client + harness units** | no — harness is a child; a portal restart ends runs (sessions survive) | **yes, measured** (`session/load`) | yes — every update is a protocol message the portal already parses | **yes, measured** (40 models, switch accepted) | permission requests + elicitation are protocol; `pi-acp` does not implement elicitation today, so pi's ask-tool arrives as a tool call we render | **yes, measured** | the ACP client, per-project harness units, the session registry, the page |
| **opencode serve/web** | no — sessions survive, runs do not (measured) | **yes, measured** | yes — full HTTP API incl. diff, VCS, LSP, MCP, file search | **yes, measured** (16 models, `connected`) | `request_permission` over ACP; `POST /session/:id/permissions` over HTTP | yes (token accounting) | a proxy layer in the portal (auth, LAN) + the page; harness UI stays optional |
| **PI WEB** | **yes, measured — the only one** | yes | yes (HTTP API) | needs generated `models.json` + stored key | **yes, measured, with previews** | not measured | thin portal integration + `projects_root`; nothing for the terminal |
| **LangGraph (+ Agent Server)** | durable execution: a *run* resumes from its checkpoint — the strongest durability here, by design | yes | yes (threads/runs API) | via LangChain's OpenAI-compatible client | **interrupts** (Agent Inbox pattern) | n/a (we own the graph) | the graphs — it is an orchestration library, not a coding harness; full self-host needs Postgres + Redis |

Nothing here is free, and two of the properties are in tension: **run persistence** favours a daemon that
is not the portal (PI WEB's sessiond, or our own harness unit with the portal as a *reconnecting*
client), while **protocol breadth** favours ACP. There is no measured option that has both today.

## 5. Recommendation

1. **Build the portal's harness interface as an ACP client (Python).** One client in
   `app/core/` (stdio transport, which is the stable one; the HTTP/WS transports are an experimental RFD
   and the portal should not couple to them yet), one session registry (harness, session id, project,
   cwd, exit, log path), one page. It is ~150 lines of adapter code plus the page, it is in the portal's
   language, and it is the interface the rest of the ecosystem is standardising on — so we stop writing
   harness-specific glue for each new toy.
2. **Host pi as the primary harness via `pi-acp`, opencode as the second via `opencode acp` /
   `opencode serve`.** Two measured, MIT-licensed harnesses, one interface. The portal generates **both**
   model configs from the router census — `~/.pi/agent/models.json` for pi (measured, queue item 18) and
   `~/.config/opencode/opencode.json` for opencode (measured, 16 models, connected) — which retires the
   `/login llama.cpp=http://127.0.0.1:8080` + blank-key + hope-the-last-item ritual for good.
3. **Keep PI WEB as the long-run runtime, not the interface.** It is the only measured thing on this box
   where a run survives a portal restart. If the operator wants "start it, close the laptop, come back
   tomorrow", that is the property PI WEB buys, and it is worth a narrow integration (projects, session
   ids, hand-off link) rather than the whole interface.
4. **The terminal tab is out of scope for the interface.** xterm 6.0.0 has no Kitty protocol
   (measured); `Shift+Enter`/`Alt+Enter` are lost. The page renders the session — transcript, tool
   calls, plan, permission prompts, context meter — not a terminal emulator.
5. **Durable portal jobs are a separate question, and LangGraph is the honest answer to it** (checkpoints,
   resume-from-step, interrupts, MIT, Python, `langgraph_sdk`). It is not a coding harness and should not
   be used as one; it is what the portal's own long jobs (index rebuild, download, deploy) want when they
   need to survive a crash mid-step. Queue item, not this slice.
6. **Skills and MCP, not harness-specific plugin APIs, are where the portal's own extensions should
   live.** The portal exposes an MCP server (its inventory, personalities, router, jobs, settings) and
   skills (`SKILL.md`) for portal conventions; every harness in the table then uses them unchanged,
   including the pi session this repo is developed in today.

## 6. What this changes in the plan

- Queue item **(18)** — portal-side model binding — stays, and widens: generate `models.json` **and**
  `opencode.json` from `routerctl`'s census.
- New **(21)**: portal ACP client (`app/core/agentd.py`) + session registry + the Projects page's
  operational state, with harness adapters (`pi-acp`, `opencode acp`) as managed units;
  `projects_root` as a portal setting.
- New **(22)**: harness registry rows on Services (version, unit, adapter, models bound) beside the
  llama.cpp/ComfyUI update tools, per the operator's instruction that harness health lives with them.
- New **(23)**: a portal MCP server + portal skills, so harnesses operate on the portal by protocol
  rather than by scraping pages.
- The self-built PTY host in `docs/PORTAL_LAYOUT.md` is **withdrawn**: ACP removes the need for it, and
  the terminal-tab evidence (xterm 6.0.0, no Kitty) says a browser terminal is the wrong goal.

## 7. Directness: what the browser talks to (the operator's second requirement)

The requirement, restated: the layers a person sees must be as close as possible to the service the
browser is connected to — not a harness wrapped in a web translation layer so it can be used in a
browser — and the harness must be light, extensible, and treat a local llama.cpp router as a
first-class citizen, with a tool and session suite at least as capable as pi's.

That rules shapes in and out:

| shape | layers a person sees | verdict |
|---|---|---|
| **PI WEB** | browser → its Lit UI → its daemon → pi | Rules *out* on this requirement, in the operator's own words: it is a web UI for a terminal harness. Measured useful (runs survive portal restarts, Q&A forms), and it is a second front door with its own auth, on its own port, with its own design language. Keep it as an optional long-run runtime, reached by a link, not as the interface. |
| **`opencode serve`** | browser → **portal page** → harness HTTP API | Rules *in*. The harness has no opinion about the browser; the portal renders the API the harness already exposes. No translation layer, no second front door. |
| **ACP (`pi-acp`, `opencode acp`)** | browser → **portal page** → portal's ACP client → harness | Rules *in*. Same directness, protocol-breadth, and one client serving several harnesses. |
| Claude Agent SDK, Codex SDK | browser → portal → vendor SDK → vendor cloud | Rules out on the local-first requirement. |
| pydantic-ai / LangGraph | browser → portal → a harness we write | The only way to get a suite that is *exactly* ours — at the cost of writing the tool suite, session semantics and compaction that pi and opencode spent years on. A library, not a harness. |

### 7.1 The suite question, measured (2026-10-11)

`GET /experimental/tool?provider=llamacpp&model=…` on the live server listed **12 tools**: `bash`,
`read`, `write`, `edit`, `glob`, `grep`, `task` (subagents), `todowrite`, `webfetch`, `skill`,
`question`, plus an `invalid` guard. `GET /agent` listed the agent types `build`, `plan`, `general`,
`explore`, and — notably — **`compaction`, `summary`, `title`**: context compaction is a built-in agent
with its own endpoint, not a side effect the user discovers when the window overflows.
`GET /command` listed `/init`, `/review`, `customize-opencode` **and `hf-cli`** — a skill from
`~/.agents/skills` appearing as a harness command, which is the Agent Skills standard working in
practice on this box.

A real coding task, prompted over HTTP to the router model:

> Create fib.py with a fib(n) function, then create test_fib.py with three pytest tests, then run pytest
> and report the result.

Result: `write` fib.py → `write` test_fib.py → `bash` pytest → **"All 3 tests passed:
`fib(0)==0`, `fib(10)==55`, `fib(30)==832040`"**, with per-step accounting
(`tokens {total: 8731, input: 174, output: 49, cache read: 8508}`, `cost: 0`). The suite is
comparable to pi's on the surface that matters — read/write/edit/bash/grep/glob/subagents/todos/skills —
and the ask-user tool exists (`question`), surfacing as a **pending tool part** in the session API
(`{"tool":"question","state":{"status":"pending"}}`), which is precisely what a portal page needs to
render its own form: the portal is not told "here is a dialog", it reads a session object.

Two measured integration details that shape the design:

1. **An opencode instance is bound to one directory.** The files landed in the server's instance
   directory (`/home/cortana`), not the session's `?directory=` value, and `GET /session/:id/diff`
   reported `[]` because the diff is VCS-relative to the instance. So the portal runs **one harness
   instance per project**, rooted at that project directory, as a unit — which is exactly the
   per-project shape the Projects page already implies, and it is what keeps an agent's writes inside
   the project it was pointed at.
2. **Compaction is portal-triggerable.** `POST /session/:id/summarize` returned `True`, and the
   summary arrived as a new message (`tokens {total: 1595, …}`) capturing the objective and the
   details. Combined with per-step token accounting, the page can show a context meter, warn before
   the window overflows, and offer "compact now" as a verb — the operator's compaction-bug workaround
   (start a new session and tell the agent to document everything) becomes a supported action with a
   logged checkpoint.

### 7.2 Local-first, compared honestly

|  | llama.cpp router support, as measured here |
|---|---|
| **opencode** | 16 models from a 12-line config, `connected` in `GET /provider`; a built-in `llama` provider (7 models); community plugins that auto-detect llama.cpp servers on common ports and query `/v1/models`; sampling/reasoning options are provider-aware. Local is the *default* posture of the project. |
| **pi** | Router mode is documented (`docs/llama-cpp.md`: `--no-models-autoload`, `/llama` load management, `/model` selection). Needs an explicit model on this box, and the credential quirk we measured; via ACP the portal sees 40 router models as config options and switching works. The `pi-llama-cpp` extension adds router management (and virtual models, which some hosts reject). |
| **Cloud-first harnesses** (Claude Agent SDK, Codex SDK, Gemini CLI) | Local endpoints are reachable (OpenAI-compatible), but the product's centre is the vendor: session features, model metadata, tool definitions and pricing are written for their own models. The operator's complaint, and it is a real property of that ecosystem. |

## 8. Recommendation, revised by the directness requirement

1. **The interface is the portal's own page**, rendered from a harness API. No harness UI in an iframe,
   no terminal in a tab, no second auth.
2. **Primary harness for the family-facing agent: opencode, one instance per project as a systemd user
   unit.** Server-native, MIT, local-first, 12-tool suite with subagents, skills, plugins, MCP, durable
   sessions, compaction and permission endpoints, token accounting. The portal generates its config
   (models from the router census, permission rules, the project directory) and owns the front door.
3. **Secondary harness: pi over ACP**, for the work we do in pi today and for the pi package ecosystem.
   Same portal page, one session registry, harness named per session.
4. **The portal generates both configs from `routerctl`'s census** (`docs/PORTAL_PLAN.md` queue 18) and
   exposes its own MCP server + skills (queue 23) so harnesses act on the portal by protocol.
5. **PI WEB stays on the box as an optional long-run runtime** — the only measured thing that keeps a
   run alive across a portal restart — reached by a link from the Projects page, in words, not as the
   interface.

## 8.5 The cheap ladder: config, MCP, skills, then a thin plugin — in that order

The operator's framing: keep our own build surface small, and keep an escape path if opencode turns out
to be the problem. The ladder below is ordered by what we have to write, and each rung was checked
against this box, not against hopes.

| rung | what it can change | what we write | measured / documented |
|---|---|---|---|
| **1. Config** | providers and models, permissions, compaction behaviour, agents, commands, MCP servers | JSON, generated by the portal | `GET /config` returns the live shape; documented `permission` (per-tool, per-pattern, per-agent, `external_directory`, `doom_loop`, `.env` denied by default) and `compaction {auto, prune, reserved}` |
| **2. MCP servers** | the tool set the harness sees — including a portal-owned memory/RAG service, portal inventory, router, jobs, settings | Python, in the portal's own language | opencode takes MCP from config (`mcp` section, local and remote) **and** over the API: `POST /mcp {name, config}` adds a server at runtime (measured endpoint, `GET /mcp` reports status). No TypeScript at all |
| **3. Skills** | how the model *uses* those tools — the handoff ritual, when to recall, when to checkpoint | `SKILL.md`, portable across harnesses | measured: a skill in `~/.agents/skills` appeared in `GET /command` as `hf-cli`; skills load through the harness `skill` tool, which is permission-keyable |
| **4. Thin plugin** | inject into the message a user sends (`chat.message`), rewrite sampling (`chat.params`), decide permissions (`permission.ask`), intercept/observe tool calls (`tool.execute.before/after`), **customize compaction (`experimental.session.compacting`)**, register tools (`tool`) | 50–150 lines of TypeScript, MIT, version pinned | the published types: `@opencode-ai/plugin` 1.19.0, `Hooks` with `event`, `config`, `tool`, `auth`, `provider`, `chat.message`, `chat.params`, `chat.headers`, `permission.ask`, `command.execute.before`, `tool.execute.before/after`, `experimental.session.compacting`; plugins load from `.opencode/plugins/`, `~/.config/opencode/plugins/`, or the npm `plugin` list |
| **5. Pinned fork** | everything | a fork of a 37 MB TypeScript tree, upstream pushed the same day this was written, 212,719 stars, 28,463 forks, MIT | **only with a pinned version, never a pruned fork.** Pruning a tree that ships releases daily converts a dependency into a product we maintain |
| **6. pydantic-ai** | a harness of our own | the tool suite, session semantics, compaction, permissions, LSP, git, skills, MCP, UI | not a fix path. It is a reimplementation of the thing we are evaluating, and the reason we are evaluating |

### 8.5.1 The memory service is a portal service, not a harness feature

The RAG-shaped idea fits the portal exactly, and none of it lives in the harness:

- The portal runs an **MCP server** (`app/core/mcpserver.py`, FastAPI + the MCP SDK) exposing
  portal-domain tools — model inventory, router census, personalities, jobs, settings, project registry —
  plus memory tools: `memory_put` (typed note, project, tags), `memory_search` (lexical today,
  vector when we want it), `memory_recall` (the last checkpoint for a project).
- Storage is the portal's SQLite (`~/ai/state/portal/portal.db`), so the memory is inspectable,
  backed up with the rest of the portal, and queryable by the pages — not an opaque blob inside a
  harness directory.
- Embeddings come from the router. Measured: the router answers `POST /v1/embeddings` with
  `{"error":{"message":"model is not loaded"}}` for `ggml-org/embeddinggemma-300M-GGUF:Q8_0`, and the
  portal is the component that loads models — so the memory service loads the embedder when it needs
  it, embeds through `/v1/embeddings`, and keeps vectors in the same SQLite. Lexical search (FTS5)
  works today; vectors are an additive step, not a prerequisite.
- Skills teach the harness to use it, in prose, portably: when to checkpoint, what a checkpoint
  contains, when to search before editing. The same `SKILL.md` trains pi, opencode, and the next thing.
- **Every harness in `docs/PORTAL_LAYOUT.md` consumes it through config, not code.** Adding the MCP
  server to a project's generated `opencode.json` is one key; registering it in pi is one line in
  `settings.json` plus the MCP adapter we already run.

### 8.5.2 Compaction, in the same order

1. **Config** — `compaction: {auto: true, prune: true, reserved: N}`. Zero code. `prune` (dropping old
   tool outputs) is the knob that most helps a small-context local model, and it is the one nobody
   mentions.
2. **MCP + skill, no code** — a `session_checkpoint` tool the model calls at the end of a working
   stretch, writing a structured record (objective · what changed · files touched · decisions · open
   threads · next step) into the portal. The portal's page shows the last checkpoint per project and
   starts a new session from it. This is the operator's manual workaround for the compaction bug
   — *"start a new session and carefully document your work so we can recover"* — turned into a tool
   call with a schema and a log line.
3. **Portal verb** — `POST /session/:id/summarize` (measured `True`), next to a context meter built from
   per-step token accounting. The page can warn before the window overflows, and "compact now" is a
   confirm-gated button with an event, not a surprise.
4. **Thin plugin** — `experimental.session.compacting` to inject memory recall into the compaction
   prompt, so a compacted session re-reads its own checkpoint and the project's notes. Only build this
   after 1–3 prove insufficient; it is the only rung with TypeScript in it.

## 9. Sources

- ACP — agents list (`agentclientprotocol.com/get-started/agents`), clients list, Streamable HTTP &
  WebSocket transport RFD (`agentclientprotocol.com/rfds/streamable-http-websocket-transport`),
  elicitation (`/protocol/v2/elicitation`, `/rfds/elicitation`), Python SDK quickstart and web transport
  (`agentclientprotocol.github.io/python-sdk`), JetBrains and Zed ACP pages, `acp-ui.github.io` (browser
  client), Casper (browser client for kiro-cli).
- pi-acp — `github.com/svkozak/pi-acp` (README, features, limitations, MIT), npm metadata (0.0.34).
- opencode — `opencode.ai/docs/server` (API table, auth, CORS, `/doc`, `/tui`), `/docs/sdk`, `/docs/plugins`,
  `/docs/skills`, custom providers and llama.cpp plugins (`llessi06/opencode-llama.cpp`,
  `theffect/opencode-modelmanager-llama.cpp`), npm metadata (1.19.0, MIT).
- LangGraph — `github.com/langchain-ai/langgraph` (durable execution), LangChain docs (CLI/Agent Server,
  local server, Studio), `langhost` (MIT drop-in), `skein-js`, `langgraph_sdk` reference; PyPI metadata.
- Letta — `docs.letta.com/self-hosting`, `@letta-ai/letta-acp` (npm), PyPI metadata.
- Skills and Agent Plugins — `opencode.ai/docs/skills`, Agent Skills standard adoption write-ups
  (atlan.com, parallel.ai, versionman.com), Agent Plugins coalition article (Aug 2026),
  `pi-claude-marketplace` (pi package bridging Claude plugin marketplaces).
- This repo — `docs/PI_HARNESS_RESEARCH.md` (the PI WEB spike and its gates), `docs/PORTAL_LAYOUT.md`,
  `docs/PORTAL_UI.md`.
