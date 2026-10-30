# Portal Plan — AI Tool Management Portal

Dated: 2026-08-31, after the cleanup cutover (PLAN.md Phases 1–5 complete).
This is the detailed environment plan: what is running now (verified), the
findings that constrain the design, the target architecture, the subsystems
the portal needs, and a phased build-out with the decision points we still
need to settle.

Companions:

- `ENVIRONMENT.md` — pre-build audit, findings F1–F11
- `UPDATES.md` — update matrix, health-pane inputs, download UX, layout rules
- `PLAN.md` — the cleanup plan this portal builds on (done 2026-08-31)
- `tools/*.py` — reference implementation of the deploy/cutover mechanics the
  portal will productionize

---

## 0. Current status — 2026-09-13 (resumption point)

> **Superseded as the resumption point by §0.1 (2026-10-12).** Kept as the snapshot it is: the
> test count, the phase list and the page set below describe the portal a month ago, and the
> queue numbering below is the historical one §0.1 tombstones.

**Committed & verified:** Phases A–E + router hero + Phase I (Settings) +
**network exposure** + **Phase J steps 1, 4, 5, 6, 7** + **Phase L (UI
integration tests)**.
Green: 185 tests (`./scripts/check-ui.sh`), of which 9 drive the real pages in a
DOM (see §6.6), plus the tab/panel geometry contract (§6.7), the stored-build
controls (§6.8) and the router pulse (§6.9).

**Phase L — UI integration tests (2026-09-13).** `tests/ui/` drives every
interactable element of every page in jsdom against a sandboxed portal: 96
controls across 5 pages, ~57 intercepted mutations, every tab in every group.
The point is that a no-build-step UI fails *silently* — a browser skips a
`<script>` block that does not parse and every button in it becomes inert — and
nothing in the old suite could see that. It found three real bugs, one of which
was live and breaking four of the dashboard's five update tabs (§6.6).
`./scripts/check-ui.sh --mutations` injects fifteen known regressions and requires
all fifteen to be caught, so the suite cannot quietly stop testing.

**Running right now — none of them loopback-only any more:**

| Service | Unit | Bind | Verified |
| --- | --- | --- | --- |
| Portal | `ai-portal` (installed + enabled, runs from `~/ai/ai-portal`) | `0.0.0.0:8090` | `curl http://192.168.1.228:8090/healthz` → 200 |
| ComfyUI | `ai-comfyui` | `0.0.0.0:8188` | `curl http://192.168.1.228:8188/system_stats` → 200 |
| llama.cpp router | `ai-llama-router` | wants `0.0.0.0:8080`, still on `127.0.0.1` **until restarted** | `pending_restart: true` in `/api/exposure` |

**⚠ One action remains for the router: restart it.** Everything else is prepared
and verified (below). The restart is deliberately not taken by the tooling that
made the change: it drops every agent session riding on the router, including
the one that made these changes. Settings → Network access → *restart to apply*,
or `systemctl --user restart ai-llama-router` (model reload ≈ 30–60 s).

**Network exposure (2026-09-13).** `app/core/network.py` (interfaces + default
route, bind-mode classification, `ss` bound-address checks where a wildcard bind
satisfies any target, TCP reachability probe, ufw state read without root,
scoped `ufw allow from <net> to any port N` hints — the portal has no root and
never changes the firewall) + `serviceconf.set_bind/validate_bind/exposure` +
Settings → Network access, which shows the *wanted* bind next to what actually
answers on the LAN address (that probe is what distinguishes "open" from
"loopback-only"; `pending_restart` is the difference between written and live).
`external=True` stays as a shorthand for `all`. Security stance is stated, not
hidden: ComfyUI has **no** auth and runs arbitrary Python via `/prompt`; the
router can take a shared `--api-key-file` key from Settings
(`app/core/llamakey.py`: secret in a 0600 file, only the path in the env file,
`live_state()` probes `/props` anonymously and with the key to report
configured-vs-enforced, and every portal probe sends the key so enabling auth
cannot lock out the dashboard). Login attempts are throttled per source
(`app/core/ratelimit.py`) because the app password is now the only gate.
All three services now launch through one generated pattern (env file → wrapper
→ static unit, `app/core/units.py`), which is what makes bind / port / API key /
extra args configurable with no unit edits; `install_unit` backs up any unit it
replaces into `state/rollback/units/`.

**Exposure and authentication are one fact, not two (2026-10-04).** `/api/exposure` and
`/api/network` now carry `auth` per service — `serviceconf.auth_state(service, key_configured=…)` —
so an entry says both that the port answers on the LAN and who can use it. `validate_bind` already
warned about authentication, but only at the moment you *changed* the bind; the state drifts on its
own (env file still `0.0.0.0`, key file revoked later), and a page that shows "reachable: yes" beside
an unrelated "no key" pill never becomes a decision. ComfyUI is warned about always (`auth_kind:
none`, arbitrary Python through `/prompt`), the router only when `llamakey.configured()` is false,
the portal never (password + cookie). `llamakey` imports `serviceconf`, so the key boolean is passed
in rather than read there.

**Verified, not assumed, about the managed router (2026-10-04).** `AI_PORTAL_PRINT_ARGV=1
~/ai/bin/ai-llamacpp.sh` with the two env files loaded prints:

```
/home/cortana/ai/bin/llama-server --host 0.0.0.0 --port 8080 --metrics --models-max 1
  --models-dir /home/cortana/ai/models/llama
  --models-preset /home/cortana/ai/config/llama/presets.ini
```

The unit is fine and complete; it is simply inactive, and the router actually answering on :8080 is
hand-started as `llama-server --lazy-mode on` — no `--models-dir`, no `--models-preset`, no
`--metrics`. That single difference is why personalities never reach the router in use, and why
`/metrics` was empty during the MTP measurement (§6.12.17). Note `--models-max 1` in
`llamacpp.env`: the managed default is one model at a time, matching the safe/default/large-model
personalities (§6.12.13). Also note `--host 0.0.0.0` there: switching to the unit would open the LAN
with no key set, which the exposure report now says out loud.

**Phase J status:**

1. ✅ `app/core/logsetup.py` — rotating `~/ai/logs/portal.log` (5 MB × 3), level
   via `AI_PORTAL_LOG_LEVEL` (`trace` allowed), `db.event()` mirrored into it.
2. ⏸ *not done* — split `app/main.py` (~950 lines, 50 routes) into `app/api/`
   modules. Still worth doing; now the top code-quality item.
3. ⏸ *not done* — `tools/phase[1-5]_*.py` → `tools/migrations/`, root
   `02-install-llama-cpp.sh` → `archive/legacy/`. The "units need a source of
   truth" half is resolved differently: units are **generated**, rendered by
   `python -m app.deploy --print-units`, with `deploy/GENERATED_UNITS.txt` as
   the committed reference; the hand-written `deploy/*.service` files are gone.
4. ✅ `app/deploy.py` + thin `deploy/install.sh` — deploys a **git ref** (never
   the working tree, so what runs is reproducible from a sha) into
   `~/ai/ai-portal` via scratch-dir + rename, uv-syncs the venv there, writes
   `portal.env` + `ai-portal.sh` + the unit, enables and **restarts**, stamps
   `BUILD.json`, then verifies `/healthz` reports the sha it just deployed.
   `--dry-run` / `--status` / `--ref` / `--rollback` (history in
   `state/portal/deployment.json`). `uv.lock` had to be committed: `--frozen`
   needs it and the first real deploy hit exactly that.
5. ✅ `scripts/dev-run.sh` — `:8091`, debug logging, pins `AI_PORTAL_HOME` to the
   real home and shouts if that is not the case (the sandbox-HOME footgun).
6. ✅ `docs/DEBUGGING.md` — what runs where, log locations, verbosity without
   code edits, curl recipes, dry-run table, network diagnosis order, rollbacks.
7. ✅ Cutover — the manual uvicorn on :8090 is gone; the service answers `/`,
   `/models`, `/metrics`, `/settings` and `/api/*` from `~/ai/ai-portal`.

Two bugs the live cutover caught, both now regression-tested: `POST
/api/services/llamacpp/ensure` was **shadowed** by the generic
`/api/services/{key}/{action}` route (FastAPI first-match + required body = a
misleading 422; the ComfyUI ensure endpoint only worked by accident of ordering),
and `systemctl --user enable --now` on an already-active unit is a no-op, so a
deploy did not restart the service it had just replaced.

**Env-file layout (all under `~/ai/config/`, managed by the Settings page):**

- `ai-env.env` — read by **all three** units (ROCm `LD_LIBRARY_PATH` etc.) —
  `app/core/serviceenv.py`
- `llamacpp.env` / `comfyui.env` / `portal.env` — per-service host/port
  (+ extra args for ComfyUI) — `app/core/serviceconf.py` (validate + persist)
  and `app/core/units.py` (per-service env/wrapper/unit generation)
- ComfyUI uses **discrete** `COMFY_HOST`/`COMFY_PORT` vars; the wrapper passes
  `--listen "$COMFY_HOST" --port "$COMFY_PORT" $COMFY_ARGS` (word-split).

## 0.1 State of the portal — coherence pass, 2026-10-12

The operator asked for this after catching a drift: a queue item written from pi-web's measurements
survived the decision to build on opencode, and the plan carried both. The audit found the cause is
structural, not personal: **the plan has two queue lists with overlapping numbers, sections get
appended with their own queue labels, and nothing marks an item superseded when a decision changes.**
This section is the single canonical queue. The older lists stay as history, each labelled, and every
item below states the evidence it rests on.

**Numbering rule, going forward.** The canonical queue is this section. Items are append-only and
numbered from 30. When a decision supersedes an item, it is tombstoned *with the reason and the
measurement that superseded it* — never deleted, never silently re-used. A section that needs work
adds a queue number here; it does not start its own list.

### Measured inventory (this box, 2026-10-12)

| Component | Measured | Note |
| --- | --- | --- |
| Portal routes | 80 | `@app.get/post/put/delete` in `app/main.py` |
| Core modules | 27, 11,471 lines | largest: `personality.py` 2,156, `modelindex.py` 1,842 |
| Templates | 9, 4,701 lines | `dashboard.html` 1,674, `personalities.html` 1,258 |
| Pages that exist | `/` `/models` `/health` `/personalities` `/settings` `/help` `/projects` `/login` | 7 plus login |
| Pages the layout doc names | `/service` `/service/router` `/machine` `/machine/updates` `/portal` `/models/detail` `/pi` | **target architecture, not built** |
| Tests | 48 files, 982 collected | suite: 1,001 passed, 3 skipped |
| UI harness | 11 page specs, control sweep + mutation check | `tests/ui/`, `scripts/check-ui.sh` |
| systemd user units | `ai-llama-router` `ai-comfyui` `ai-portal` `pi-web` `pi-web-sessiond` | the last two are queue 30's removal |
| Harness installed | opencode 1.19.0 (`~/.npm-global`), configs generated by `agentconfig.py` | **`opencode serve` has no unit: the host is unmanaged** |

The row that matters most for the operator's complaint: the layout doc (`docs/PORTAL_LAYOUT.md` L2)
describes a page map of `/service`, `/machine`, `/portal`… and the portal ships seven pages with
features packed into them. `/` carries dashboard, build controls, updates, health, and the job bar;
`/personalities` carries the deck, the settings editor, the model-personality cards and the presets
preview. **That is the shape of the "features smashed together" complaint, measured:** the
implementation grew by feature, the layout doc describes the target by duty, and no queue item said
"move the portal from one shape to the other". Queue 34 is that item.

### Decision record — the harness is opencode (2026-10-12)

opencode only, portal-side. pi-web is out of the portal's code path: the two units are stopped and
disabled, the `pi-web-sessiond` provider drop-in is removed, and `~/.pi/agent/models.json` keeps
being generated for the operator's `pi` CLI use (it is a config file, not a harness dependency).
`docs/PI_HARNESS_RESEARCH.md` stays as the evidence for what was measured and why; it is research,
not a build plan.

The condition for reopening it, stated by the operator: **only a major opencode gap that we cannot
fix.** The candidates for that gap, each with its gate in queue 30: the session host surviving a
portal restart; the question tool rendering as a form; model binding from the router census. If a
gate fails and the fix is beyond us, the fallback evidence is in that research doc — and it gets a
new queue number, not a silent revival of an old one.

### Superseded (tombstoned with reasons)

- **(15) `projects_root` as a portal setting key — DONE.** `app/core/portalsettings.py`, live on
  `/settings`, validated (absolute, no NUL, bounded traversal, creatable, writable) and read by every
  project path operation.
- **(16) Spike PI WEB on this box — DONE, and superseded by the harness decision.** All four gates
  were run (`docs/PI_HARNESS_RESEARCH.md`): survived a web restart, Q&A form as a browser form,
  `Shift+Enter` in the terminal tab **failed with the version named**, model binding worked from the
  census. It answered the question the operator asked; it is not a build plan any more.
- **(19) Terminal-tab key fidelity — SUPERSEDED.** The finding (`@xterm/xterm` 6.0.0 has no Kitty
  keyboard protocol, so `Shift+Enter` arrives as plain `Enter`) is true and measured, and it is about
  a tab the portal will not ship. The requirement it stood for — a newline that is not a send — is
  now queue 32's textarea, where the browser gives us `Shift+Enter` natively.
- **(22) Harness rows on Services — REDUCED.** It was written as rows for llama.cpp, ComfyUI *and*
  the harness. With pi-web dropped it is one row: the opencode host unit, with the same start/stop/
  journal affordances the router row has.
- **(23) + (24) + (25) — MERGED into 35.** They were written as three items (MCP server, MCP memory
  tools, skills) by three different sessions. One item, one file list.
- **(26) Compaction ladder — MERGED into 32.** The config half is already written by
  `agentconfig.py` (`compaction {auto, prune, reserved}`); the visible half is the context meter from
  opencode's per-step token accounting, which is a page feature, not a separate ladder.
- **Queue 29 as written — RENAMED.** It said "the pi harness page", inherited from the pi-web spike.
  The requirement is unchanged (session outlives the portal service, resumable after a reboot, the
  page shows the session and nothing else while one runs); the host is opencode. It is queue 32.

### Canonical queue (30 →), ordered by what blocks what

- **(30) DONE 2026-10-12 — the opencode host, managed and gated, measured on this box.**
  `app/core/harnessctl.py` + `GET /api/harness/status` + `POST /api/harness/start`. The unit
  `ai-harness.service` runs `opencode serve` on 127.0.0.1:4096; the password is generated once to
  `~/ai/state/portal/harness.key` (0600) and reaches the unit through `~/ai/config/harness.env`.
  Measured: host healthy, `version 1.19.0`, unit MainPID == the port's listener. **Gate 1 (portal
  restart):** `systemctl --user restart ai-portal` left MainPID 320472 untouched, the session
  `ses_ecd8ddd4fffe…` was listed for its cwd, its 2 messages survived, and the follow-up prompt was
  answered (4 messages). **Gate 2 (browser close):** nothing to survive — the host holds no browser
  state; every page fact is a read. **Gate 3 (reboot analogue):** `systemctl --user restart
  ai-harness` (process dies, only `opencode.db` survives) — the id was resumable from disk, the
  session titled "GATE1" answered again (6 messages). A physical box reboot remains unmeasured; the
  mechanism is the same file. **Gate 4 (no shadow):** a second start refused —
  `port 4096 is in use by pid 320818; the harness host refuses to shadow it`.
  Session facts the page must copy: the directory is a **query parameter** (`?directory=`; a body
  key of the same name is ignored — measured: it created the session in the server's own cwd), the
  prompt body is `{parts:[{type:"text",text:…}]}`, and sessions carry a title opencode derives from
  the first prompt.

- **(30) The opencode host, managed and gated.** `opencode serve` as its own systemd user unit
  (`ai-harness`), fixed port, **refusing to start on a busy port** — measured artifact: a second
  `opencode serve` on a busy port is answered by the first server, so a shadowed host is invisible.
  Gates, measured on this box before any page is written: session alive after `systemctl --user
  restart ai-portal`; session alive after the browser closes; session resumable by id after a full
  box reboot; health/version answers the portal can read. Removal of `pi-web.service` +
  `pi-web-sessiond.service` and the provider drop-in lands in this slice.
- **(31) DONE 2026-10-12 — both interfaces measured, HTTP kept.** `docs/HARNESS_LANDSCAPE_RESEARCH.md`
  §10.5 has the side-by-side. The decisive measurement: `opencode acp` makes the agent a child of the
  client's stdio pipe, so the session dies with the portal — the requirement forbids that; the
  standalone HTTP server survives it (queue 30's gate). Secondary: ACP's `new_session` returned
  `modes: []` and `models offered: 0` on 1.19.0, so census model binding has no ACP surface, and a
  streaming client must implement `session/update` (measured: `Method not found`). `harnessctl.py`
  (HTTP) is the client; ACP stays useful for external editors and for a future bridge, additive.

- **(31) Interface spike, both ways, then one client.** Launch-in-a-cwd, stream-a-reply, answer-a-pending-question,
  implemented twice: opencode's HTTP API and ACP (`acp` python SDK, measured installable). Report the
  friction (auth, streaming, question round-trip, restart semantics, what each cannot express), pick
  one, and write `app/core/harnessctl.py` against the winner. No portal page before this is decided.
- **(32) `/pi`, the two states the operator specified.** Inactive = the Projects page's content;
  operational = the session and nothing else. `Shift+Enter` newline natively, the question tool as a
  form, the model badge from the census, the context meter from per-step tokens, `/quit` returning to
  the inactive state with the session id left visible for resume. Projects' Launch button is the
  handoff point. **PARTLY DONE 2026-10-15 (§6.14.1v, §6.14.1w):** the host verbs, the launch-and-record
  call site, the session facts endpoint, and the Projects page's in-place switch are landed and
  measured on the operator's tree. The `/pi` route itself is not built — the operational state lives
  in the Projects page, which is what the two-state shape asks for.
- **(33) Polish wave 1 — the personalities page workflow.** The operator enumerated the list on
  2026-10-12; it is kept with measurements in `docs/PORTAL_POLISH.md`. Their item, confirmed: the page
  is **6,800 px** of scroll and the settings editor is **5,298 px** in one block — 6.6 viewports, the
  layout brief's limit is three. The proposed four-state shape (Now / Deck / Editor-opened-by-a-verb /
  Reference in Help), plus the personality↔project connection, is the brief they asked to discuss.
  **Gate: their agreement on the four states before code.**
- **(34) Polish wave 2 — page map coherence.** Move the portal from feature-shaped pages to the
  layout doc's duty-shaped map, or change the map: what `/` keeps, what moves to `/service`,
  `/machine`, `/portal`, and which named pages never get built. Nav, breadcrumbs, the attention
  ribbon, empty and degraded states per page. This is the "features smashed together" fix, and it is
  a decision before code.
  The operator's page-by-page list is the input: `docs/PORTAL_POLISH.md` §1, §7, §8.
- **(35) The portal exposes MCP and skills.** `app/core/mcpserver.py`: router census, model inventory,
  projects registry, agents sync, presets state, and `inspect`/`lint` reads — plus `SKILL.md` files
  that teach the handoff (checkpoint format, where the plan/queue live, how to read the census).
  Queue 25 of the old list, merged with 23/24.
- **(36) `tools/layout_measure.py` as a plugin.** opencode tool plugin first (the operator's stated
  preference: plugins over services), MCP fallback. Requirements unchanged: input validation, error
  trapping and recovery, output legible to the agent **including connection failures**.
- **(37) Memory.** The research is in `docs/MEMORY_PLUGIN_RESEARCH.md`; the portal's own memory
  service exposed as MCP is the shape that serves both the harness and the portal. Not before 33/34.
- **(38) LAN and TLS (old 17).** Local CA + certificates for `portal.local`/`pihole.local`, nginx
  terminating TLS at a sub-path with WebSocket forwarding, portal password gate plus a harness token.
  Not required for proof-of-concept, required before the family uses it from other machines.
- **(39) Knowledgebase as a documentation site (§6.14.2).** Not started.

The operator's design-debt enumeration (2026-10-12) is kept as `docs/PORTAL_POLISH.md`, each item
paired with a measurement from this box and a proposed shape. It feeds 33 and 34, and adds:

- **(40) Status page: a vitals strip, not a CPU chart. — IN PROGRESS 2026-10-13.** Four tiles
  (GPU pool, VRAM, system RAM, disk) with `ok`/`watch`/`danger` states from thresholds, a top-RSS line naming which llama-server
  slot is the user, the router hero split into Serving / Census / Load sections, and **server-side
  metrics history** so the Machine detail chart is populated the moment it opens. Measured cause of
  the operator's complaint: `startMachinePoll()` polls only `if (d && d.open …)`, and the history
  array lives in the browser — every page load starts at zero samples. Measured on this box: CPU
  3.9%, VRAM 523,329,536/536,870,912 = **97%**, GTT pool 61.1/115.9 GB = 53%, RAM 58.2%, disk 35%.
- **(40b) Harness row in the Tool Repository Update Status**, beside llama.cpp / ComfyUI /
  huggingface-cli / Python / GPU drivers. The section itself is kept as designed — the operator's own
  verdict, recorded so a later session does not "improve" it.
- **(41) Settings page as control.** Network access becomes one mini-table per service with footnote
  markers (measured: 104 characters in an 84 px service cell, 358 characters in a 250 px reachability
  cell, 247 px given to an empty input, and 1 intersecting box pair at 1280×800). Controls for the
  rates that already exist (`metrics_poll_ms`, `router_poll_ms`, `router_metrics`), the queue 40
  thresholds, which units start at login, presets publish, harness config sync. Standing rule for
  every new feature: does its value belong here.
- **(42) Help page as a documentation site.** Measured: `.kb-toc` is 1,024 px of client width against
  **13,749 px of content** — 13.4 viewports of horizontal scrolling, 208 nodes — and the 41 document
  links go nowhere. Left tree, right viewer, search ribbon that stays out of the way. This is where
  33's key reference moves to.
- **(43) Models datagrid identity.** Measured: the sticky model-id column is 352 px holding 487–542
  px of content, so the frozen column paints over the scrolling columns — structural, not a glitch.
  Short model name as the row identity, source (`hub cache` / `library` / `path`) as its own column
  beside the live light, one capability vocabulary with `paired` stating only what the pairing adds
  (`+ embed:embeddinggemma`), and a grid contract assertion `scrollWidth ≤ clientWidth` on sticky
  cells so this cannot come back silently.
- **(44) Health page refresh.** The model store card reads `model directories 0` next to `files in
  index 20 (274.1G)` — it counts the pre-hub-cache library layout, which is why it reads as empty.
  Count by source, add the harness row, re-read every card against the subsystems the portal has now.
- **(45) Model inventory management, design brief first.** Their flow bug is confirmed: choosing a
  llama.cpp model in HF search lands in the ComfyUI safetensors download mode. Rules before features,
  from the operator's policy after the library loss: deletion is allowed **only** with a confirmation
  naming the resolved path and stating it cannot be undone; **no unconfirmed removals, no bulk
  removals, nothing that targets a directory**; the operator is asked before any mass-delete or folder
  removal, and a report-only dry run precedes it. Mutations are planned, previewed, then applied;
  `presets.ini` keeps `*.portal-prev`; a destination is named before any download starts. The cache's
  opacity is the operator's stated reason for the work — inventory must answer "what is there and what
  is usable" by looking, with a copy-to-library verb that is additive.
- **(47) Pinned as research, not scheduled: personality-scoped model visibility** (the symlink models
  library that limits which models a personality may load, so several resident models cannot create an
  OOM condition). The operator is not pushing it back in; it becomes live if the Projects page needs
  per-project model visibility. The research must name the mechanism, the failure mode that wiped
  `~/ai/models/llama/`, and the guard that makes it unrepeatable.

**Interleave decision (operator, 2026-10-12):** queue **43** (models datagrid identity), **41**
(settings as control) and **42** (help page) run interleaved with 30–32. Queue **33** is approved as
the four-state design (Now / Deck / Editor-opened-by-a-verb / Reference in Help) and **40** as the
vitals strip with the proposed thresholds (pool ≥85% danger, ≥70% watch; VRAM ≥90%; RAM ≥90%/≥75%;
disk ≥90%) and the CPU chart out of the hero.
- **(46) ComfyUI models tree.** Columnar tree over ComfyUI's own folder groups with per-file detail
  (format/quant, scheduler-visible, used-by), so it stops being sizes-only next to the llama.cpp grid.

- **(48) Finalization, for after feature-complete — the operator's request, recorded, explicitly
  not to be worked on or planned now.** A real audit of what this project requires (tools, libraries,
  system binaries, versions), with `docs/` and the requirements files made to say exactly that, and an
  install script that stands up the portal on a clean system. Later extensions named by the operator:
  other operating systems, and llama.cpp builds chosen for other GPU/NPU targets, with the same
  hardware tuning applied to parts like PyTorch. This is the gate for publishing the repo, not a
  development item.

- **(49) The opencode engine's own update status.** The operator's ask, verbatim: *"adding opencode
  to the status page's 'tool repository update status' section with checks for the various opencode,
  opencode extensions, and internal page service/extension available updates with some scripting to
  apply those updated parts to the portal's opencode engine."* The active project session (queue 32)
  is meant to replace the operator's desktop pi session for updating and deploying the portal, so
  the engine that runs it has to be as inspectable as the portal's own repos are today: the version
  and origin of the `opencode` binary the harness serves, its plugins/extensions (the `opencode-*`
  packages the config names), what an update would change, and a verb that applies a chosen part.
  Not scheduled: 32 must land and be used first, because the shape of this section is decided by
  what the engine is observed to need in practice.

Priority, per the operator: 30–32 stay first. 33 and 45 are briefs the operator reviews before code.
40, 41, 42, 43, 44 are in-place fixes that can interleave with the harness slices.

**What is deliberately not in the queue:** the layout doc's `/models/detail`, `/machine/updates`
split, and the L0/L1/L2 drill pages. They are architecture notes with no feature attached; when 34
decides the map, they get numbers or they get struck.

## 1. Verified current state (post-cleanup)

| Component | Location | State |
| --- | --- | --- |
| Runtime root | `~/ai/` | bin, build, comfyui, config, llama.cpp, logs, models, releases, sdk, state |
| Router service | `ai-llama-router` (systemd **user**) | active; `127.0.0.1:8080`; 14 models (13 local dirs + 1 HF-cache duplicate) |
| Deployed binary | `~/ai/releases/llama.cpp-b10641` | v0.3.0-dev @ `539f24529`, ROCm + Vulkan, self-contained RPATH; symlink farm `~/ai/bin` (in `PATH`) |
| Model store | `~/ai/models/llama/` | 13 model subdirs, 275 G (hardlinks); router contract §2 below |
| Presets | `~/ai/config/llama/presets.ini` | 14 sections (`[*]` + 13 models), keys validated against router source; actually loaded by the service now |
| Source / build | `~/ai/llama.cpp` (clean clone) / `~/ai/build/llama.cpp-<commit>` | repo stays source-only; builds are disposable |
| ComfyUI | `~/ai/comfyui/` | clone + venv (Python 3.14.4, torch 2.12.0a0+rocm7.13) + full model store (incl. the 2 files merged from the stale workspace clone); **not running, no service yet** |
| Vulkan SDK | `~/ai/sdk/vulkan/1.4.341.0` (1.6 G) | build tools auto-select newest version dir (`aiportal.py::_latest_vulkan_sdk`); no more manual `setup-env.sh` for scripted builds; compat symlink `~/vulkan` kept |
| HF cache | `~/.cache/huggingface/` (31 G) | kept in place (F11); portal manages via `hf` / `huggingface_hub` |
| State | `~/ai/state/` | `llama-releases.json` (phase-3 release registry), `rollback/` |
| Compat symlinks | `~/llama.cpp`, `~/ComfyUI`, `~/comfyui`, `~/vulkan` | old paths still work |
| Disk | `/` (nvme0n1p2) | 1.8 T, ~990 G free after cleanup |

**Pending user action:** `~/ai/state/rollback/lmstudio-models` still holds the
hash-verified 244 G LM Studio originals. When you're confident, free it with
`rm -rf ~/ai/state/rollback/lmstudio-models`. (The one file it had that was
*not* in the canonical store — `qwen-image-2512-Q8_0.gguf` — was deliberately
excluded per your decision.)

## 2. Findings → design constraints

Short form of ENVIRONMENT.md F1–F11; each one pins a design rule:

| Finding | Constraint the portal must respect |
| --- | --- |
| F1/F2 router layout (source-verified) | Model store contract: `--models-dir` scanned **one level deep**; subdir = model id; inside it, flat files only — `mmproj*` → vision sidecar, `mtp-*`/`dspark-*`/`dflash-*` → draft (first wins); loose top-level `.gguf` = standalone model; **sub-subfolders are never scanned** |
| F4 presets never applied | Per-model tuning *must* flow through `--models-preset`; section names must equal the router-produced model ids; only router-validated keys (`device`, `ctx-size`, `fit*`, `stop-timeout`, `load-on-startup`, `dedup-cache-models`, `model-draft`, `mmproj`, …) |
| F3/F7 staging baked into RUNPATH | Deploys are atomic: build → stage to `~/ai/releases/<id>` → verify (`--version`, `ldd`, smoke) → swap `~/ai/bin` symlinks → keep previous release; never build in-place or dump into a shared bin dir |
| F6 244 G duplicate weights | Canonical store lives in `~/ai/models/llama`; reorgs use hardlinks (same NVMe, O(ms), 0 space) |
| F8 ComfyUI split-brain | ComfyUI root is `~/ai/comfyui` (repo + venv + models together); updates = git + venv sync, **no build** |
| F9 no service mgmt | llama router (done) and later ComfyUI run as systemd **user** services; portal owns lifecycle + logs |
| F10 home clutter | Archived to `archive/legacy/`; nothing load-bearing outside `~/ai/` |
| F11 HF cache | `~/.cache/huggingface` stays; all consumers default there — portal manages it, doesn't move it |
| UPDATES.md matrix | apt (ROCm/kernel/mesa) is self-updating; portal surfaces it, doesn't drive it. Only the **LunarG Vulkan SDK** (manual tarball → `~/ai/sdk/vulkan/<ver>`) needs portal/user action; new version = advisory rebuild offer |
| Session dependency | **The agent session itself rides the router.** Any router stop/cutover is a confirmed, user-visible operation; the portal writes rollback info (phase-4 pattern) and never assumes it can restart itself |
| Storage | `nvme1n1` is never touched |

## 3. Target architecture

```
~/ai-projects/ai-portal/          # code repo (git) — NO weights, NO builds
├── app/
│   ├── main.py                   # FastAPI entry (uvicorn, 127.0.0.1)
│   ├── core/                     # paths.py (AI_ROOT constants), config, db
│   ├── providers/                # llamacpp.py · comfyui.py · huggingface.py
│   ├── models/                   # store scan, gguf metadata, presets, downloads
│   ├── services/                 # systemd user-unit lifecycle + journal tail
│   ├── updates/                  # release pipeline (build/stage/verify/swap)
│   └── api/                      # REST routes + SSE/WebSocket for logs & jobs
├── web/                          # frontend (D1: HTMX+Jinja, chart-friendly)
├── tools/                        # existing phase scripts = reference impl
└── tests/

~/ai/                             # runtime root — single source of truth
└── state/portal/                 # portal.db (SQLite), job logs, download tmp
```

Rules:

- Every path derives from one constant (`AI_ROOT = ~/ai`); the constants in
  `tools/aiportal.py` move to `app/core/paths.py`.
- Portal runs as a systemd **user** service (`ai-portal`) in its own
  uv-managed venv inside the repo (gitignored).
- **Access (D2, decided):** binds to the LAN (`0.0.0.0`, configurable via
  `AI_PORTAL_HOST`) and requires a **simple app password** — no cloud / no
  external IdP. Single shared credential, hashed (scrypt) at rest, session via
  a signed `HttpOnly; SameSite=Strict` cookie. LAN-trusted only; the box is
  not exposed to the internet.
- Repo holds code; `~/ai` holds runtime data. `git status` stays clean.

## 4. Subsystems

### 4.1 Tool providers (the core abstraction)

Each tool (llama.cpp, ComfyUI, Hugging Face) implements one interface:

```
status()   → installed version, deployed version, service state, behind-by
start/stop/restart(), tail_logs()
update(target=None) → Job            # tracked in state, cancellable
config()   → read/propose validated config (e.g. presets.ini)
```

- **llamacpp** — richest: source repo, releases registry, router service,
  model store.
- **comfyui** — repo + venv sync; service added in Phase E.
- **huggingface** — no service; download jobs + cache pruning
  (`huggingface_hub` scan/delete APIs), cache size reporting.

### 4.2 Model manager (llama)

- **Inventory**: scan `~/ai/models/llama` per the §2 contract; record every
  file with path + size + content hash (incremental); flag orphans (files not
  referenced by any model dir), broken hardlinks, and near-duplicates.
- **Metadata**: GGUF headers via the `gguf` Python lib (port the working code
  from `~/ai-projects/gguf-indexer/` — D6).
- **Presets**: generate `presets.ini` from inventory + user tuning; validate
  keys against the router's accepted set *before* writing; keep a history
  snapshot in state before every apply; section names always = router model ids.
- **Downloads** (the HF-command UX from UPDATES.md): user pastes
  `hf download <repo> --include …`; portal parses it, downloads through
  `huggingface_hub`, places files by role (main / `mmproj*` / `mtp-*` /
  `dspark-*` / `dflash-*`) into the right model subdir, skips identical
  size+hash files, refreshes inventory + presets, then offers router
  restart/hot-reload.
  - *v1 (done 2026-09-04, `app/core/modeladd.py`)*: one paste box on the
    models page accepts an `huggingface-cli`/`hf download` command, an
    `org/repo` name, a direct file URL, or a local path; the portal fetches
    (hf_hub / streaming / copy), sniffs the header (`modelmeta`: GGUF KV
    walk + safetensors JSON header — no full-file parse), and files it in
    the canonical store: GGUF → `~/ai/models/llama/<dir>/` (dir = filename
    stem, the tree's convention; `mmproj-*`/`mtp-*`/`draft-*` nest under
    their parent model's dir when unambiguous, else explicit `model_dir`),
    safetensors/ckpt/bin → `~/ai/comfyui/models/<category>/` (auto-
    classified from tensor keys, overridable). Placement is indexed into
    `portal.db` (`models` + `model_files` with sha256) as a background job
    (`POST /api/models/add` → `/api/jobs/{id}` progress). Remaining:
    `--include` globs, size+hash skip, preset refresh, router restart offer.
- **GC/dedup**: delete-or-link operations only with dry-run + confirm +
  rollback snapshot in `~/ai/state/rollback/`.

### 4.3 Service manager

- Owns user units: `ai-llama-router` (exists), `ai-portal`, later `ai-comfyui`
  under `~/.config/systemd/user/`.
- start / stop / restart / enable / tail (`journalctl --user -u … -f` streamed
  to the UI over SSE/WS).
- **Self-awareness**: router restarts drop the agent session — such actions are
  explicit confirmations with a written rollback note (phase-4 pattern), and
  the portal itself must not be stopped by its own actions.
- **ComfyUI (D4, done 2026-09-04):** user service `ai-comfyui`, same
  user-slice pattern as the router. Installed, verified serving on
  `127.0.0.1:8188`, start/stop/restart from the UI via the generic
  service controls. Log following reuses the same journal feed (raw for now —
  ComfyUI's log format differs; structured parsing later only if it proves
  useful).

#### 4.3.1 Launch config via generated env file (pattern, proven on ComfyUI)

The unit stays **static**; every launch parameter lives in a generated env
file the portal owns:

- `~/ai/config/comfyui.env` — `KEY=VALUE` (values quoted so systemd keeps
  spaces; written from `~/ai/config/comfyui.json`)
- `~/ai/bin/ai-comfyui.sh` — thin `sh` wrapper; the wrapper's shell re-splits
  the intentionally-unquoted `$COMFY_ARGS` into real argv words
- `~/.config/systemd/user/ai-comfyui.service` — `EnvironmentFile=` +
  `ExecStart=<wrapper>` only

Re-parameterizing = rewrite env file + `systemctl --user restart` — no unit
edits, no daemon-reload, every knob visible in one small file. Proven first
on ComfyUI (no live traffic) **before** porting to `ai-llama-router`.
Division once the router adopts it: the env file governs *launch* parameters
(host, port, `--models-max`, …); `presets.ini` keeps governing *model slots*.
Implementation: `app/core/serviceenv.py` + `ComfyUI.ensure_service()` /
`set_config()` (API: `POST /api/services/comfyui/ensure`,
`GET|POST /api/services/comfyui/config`).

### 4.4 Update pipeline (productionizes phase-3/4)

- **llama.cpp**: `git fetch` → build at a *pinned* ref in
  `~/ai/build/llama.cpp-<id>` (CMake args + Vulkan SDK auto-select already in
  `aiportal.py`) → `cmake --install` into `~/ai/releases/llama.cpp-<id>`
  (self-contained RPATH) → verify (`--version`, `ldd` clean, optional smoke
  worker load) → swap `~/ai/bin` symlinks → restart router → record in
  `~/ai/state/llama-releases.json` + portal DB. Previous release kept for one
  click rollback.
- **llama.cpp — implemented (Phase C):** `app/core/updater.py` runs each update
  as a background job (`jobs` table, log at `~/ai/logs/update-<kind>-<id>.log`),
  so a 10-20 min build never blocks a request; the UI tails `/api/jobs/{id}`.
  Pipeline (`LlamaCpp.update`): fetch (offline-tolerant) -> resolve ref
  (`HEAD` = remote default branch, else local) -> refuse if repo dirty ->
  **no-op** if target == deployed commit -> checkout ->
  `cmake -B ~/ai/build/llama.cpp-<sha12>` (`paths.LLAMA_CMAKE_ARGS`, newest
  SDK) -> build -> stage flat self-contained release -> verify (`--version`,
  clean `ldd`, smoke worker loading the smallest model) -> rollback note ->
  repoint `~/ai/bin` symlink farm -> `llama-releases.json` + DB (`releases`
  table) -> `systemctl --user restart ai-llama-router` + health poll;
  **auto-rollback** if the router doesn't come up. `dry=True` stops after
  staging+verify (production untouched). Rollback is a fast synchronous
  repoint+restart (`LlamaCpp.rollback`). **Busy is checked before the preflight
  git probe**: whether the machine can take the job at all is the more useful
  answer, and the one that must not wait on the network.
  Endpoints: `GET /api/updates/llamacpp`,
  `POST /api/updates/llamacpp` (`target`/`restart`/`smoke`/`dry`),
  `POST /api/updates/llamacpp/rollback` (confirm required), `GET /api/jobs/{id}`.
  The ComfyUI updater (Phase E) reuses `updater.start_job` with its own pipeline.
- **ComfyUI — implemented 2026-09-14** (`ComfyUI.update`, §6.11): `git status`
  (refuse on local changes, naming them) -> `fetch` -> **no-op** when the
  checkout is already current -> `pull --ff-only` -> generate a pip constraint
  file from this venv's machine-specific builds -> `pip install -r
  requirements.txt -c <constraints>` (plus `--upgrade` only when the operator
  ticks it, plus `manager_requirements.txt`) -> **verify torch still imports
  with ROCm** -> restart + health poll, version diff in the job log.
- **Hugging Face**: downloads per 4.2; cache pruning as a managed job.
- **One at a time** (2026-09-14): `updater.DISRUPTIVE` = llama.cpp build, ComfyUI
  update, huggingface-cli, portal python. A job in that set requires an otherwise
  idle machine — no second disruptive job, no service stop/restart, no rollback,
  no release deletion — refused with HTTP 409 naming the job and its progress
  (`busy — ComfyUI update master (job 77, since 10:14:02) — syncing venv`).
  Downloads (`model-add`, `comfyui-download`) move files and are allowed to share
  the machine, but never with a job of their own kind, and never with an update.
  The DB `jobs` table is the source of truth, so a second browser tab, a second
  operator, and a page that was closed all see the same answer; rows still
  `running` after 6 h are reclaimed as `failed` (a portal killed mid-build must
  not lock the machine for ever).
- **Advisory only (never auto)**: apt deltas touching `amdrocm*|amdgpu*|
  mesa-amdgpu*` + kernel (reboot-pending flag), Vulkan SDK newer-version check
  (lunarg URL, known pattern), ROCm/torch version drift → offers
  "rebuild llama.cpp" / "sync ComfyUI venv".

### 4.5 Health & status pane (done 2026-09-14)

Inputs (UPDATES.md §"health pane"): apt pending set, reboot-pending, ROCm
version vs llama.cpp build vs ComfyUI torch, Vulkan SDK installed + latest,
llama.cpp local vs `origin` (git ls-remote), deployed release id, router
uptime, ComfyUI commit/venv versions, disk sizes (`~/ai`, HF cache,
rollback), model-index freshness, orphan files, broken hardlinks.

**Built as `/health`** (`app/core/health.py` + `web/templates/health.html`). Two
rules hold it together, and they are the reason it is useful:

- **Read-only.** Nothing on the page installs, reboots, deletes or restarts; the
  only control is a re-check (`?refresh=1`). Each advisory *links* to the control
  that acts, on the Status page or in Settings, behind its own confirm. The UI
  contract asserts the pane makes no request to an update endpoint.
- **Every probe degrades alone**, on its own TTL, because they cost wildly
  different amounts: `apt -s upgrade` 0.7 s (600 s cache), `du -sxb` over 580 GB
  0.5 s (600 s), dpkg 50 ms (90 s), the upstream version check 5 s (24 h, and a
  failure is held an hour so an offline box does not pay it per page load). A
  missing binary or unreadable directory becomes `{"error": …}` in its own
  section, never a 500 and never a blank page.

What the machine taught while building it:

- **Three ROCm versions, one of which is the real one.** apt says 10.0.0,
  `hipcc` says 7.15, and the ROCm that actually loads is whatever
  `LD_LIBRARY_PATH` names in the shared service env — RPATH does not decide it.
  The pane shows all three and calls out a launch directory that no longer
  exists (that is a router that will not start) as `bad`.
- **torch's ROCm is not a bug.** ComfyUI's wheel is built for ROCm 7.13 on a
  10.0 system and works, because the wheel ships its own ROCm libraries; that is
  an *info* advisory with the trade-off, not an alarm (UPDATES.md's rule).
- **LunarXchange cannot be scraped** — `vulkan.lunarg.com/sdk/home` is a JS
  portal whose HTML contains no version (verified). The tags on
  `KhronosGroup/Vulkan-Headers` are the SDK versions, are public, and one cached
  call a day stays inside GitHub's rate limit.
- **The build must stamp its own ROCm.** `llama-releases.json` recorded the
  Vulkan SDK but not ROCm, so "rebuild for the new ROCm?" was unanswerable after
  an apt upgrade. Builds now write `"rocm": <installed version>` into the
  registry (`providers/llamacpp.py`); the pane says when the deployed entry
  predates that.
- **Orphans are real here**: model directories holding `.gguf` files one level
  too deep (invisible to `--models-dir`, which scans a single level), HF cache
  directories sitting inside the models dir, and an index 13 days stale. The pane
  found all three the first time it ran.

Not done on purpose: a dashboard badge with the attention count (it would put an
apt simulation and a 580 GB `du` on the dashboard's hot path — §6.10); the count
lives in `/api/health/detail` until the dashboard's polling is reworked.

### 4.6 State & history (SQLite)

`~/ai/state/portal/portal.db` (WAL mode):
`tools`, `releases` (tool, id, path, commit, sdk, created, active),
`jobs` (kind, target, status, log), `downloads` (repo, files, placement),
`models` (id, dir, role files), `model_files` (path, size, sha256),
`presets_history`, `events` (audit log of every mutation).
`llama-releases.json` stays as the machine-readable release registry the
phase-3 tooling already writes; the DB indexes it.

### 4.7 Personalities — provisioning, not just presets (user direction 2026-08-31, reframed 2026-09-15)

A **personality** is a task-shaped allocation of the box: which models are
loadable at all, how many at once, what each is configured with, and what the
worst case costs. `presets.ini` is one output of that decision, not the decision
itself.

A personality is:

| field | meaning |
| --- | --- |
| `models` | the **available set** — a symlink farm of model dirs the router may see at all (§2 layout). Everything not in it cannot be loaded, cannot be OOM'd by, and does not appear in clients. **(corrected 2026-09-29: that holds for the directory source only — on b11146 hub-cache models stay discoverable and load by name no matter what the farm contains, measured in §4.9.1; `HF_HUB_CACHE` in the unit is the lever that closes it.)** |
| `models_max` | how many may be resident simultaneously — a hard router limit, not a hint |
| per-model preset | backend, context policy, extras (§6.12's rubric) |
| `budget` | the memory plan: the **heaviest `models_max` combination** in the set must fit with headroom. This is enforced by *nothing* but the portal: loaded weights are anonymous and unreclaimable (§6.12), `fit` touches only arguments left unset, decides per load, and omits the compute buffers it allocates afterwards (§6.12.7), and a model's footprint runs above its file size by the runtime's own allocations (§6.12.2, §6.12.7). Measured, not assumed. |
| `ttl` | idle seconds after which the portal unloads a model the router would otherwise keep forever |

Two worked shapes, from the user:

- **Coding IDE** — a large model for planning/agentic work, a second for RAG, a
  third for autocomplete: `models_max = 3`, and the budget is the sum of those
  three, not of the three biggest files in the library. Autocomplete wants a
  small model on the fastest-reacting backend; the planner wants context.
- **Agentic OS management** — one big model, `models_max = 1`, nothing else
  resident, so the entire budget goes to a single model's context.

**Two personalities always exist and neither is editable in place:**

- **`safe`** — minimal, nearly empty: the smallest known-good model,
  `models_max = 1`. The thing to fall back to when a personality is misbehaving
  or a load just failed. If `safe` cannot start, the box is broken and the portal
  says so.
- **`default`** — every llama model in the library, `models_max = 1`. Behaviour
  is inherited from **global** preset values only; the per-model overrides a user
  can reach are the ones the portal deliberately exposes. Editing a personality
  must not be possible without going through a personality, because an unlisted
  edit to `presets.ini` is how the next person debugging an OOM loses their
  afternoon.

Why this belongs in the portal and not in llama.cpp: the router evicts under its
own queue pressure and otherwise keeps loaded models indefinitely (§6.12), and
nothing in it knows about the desktop, the GPU consumer, or the next model
someone is about to download. Capacity planning that ignores that is a server
that dies at 3 AM.

### 4.8 RAG service + MCP interface (planned, later phase H)

A document Q&A service exposed over MCP. Architecturally it is *just another
provider* for the portal — that is the validation of the §4.1/§4.3
abstraction. The three open concerns, and the working answers:

- **Database**: do not grow the portal DB into a DBMS. The portal keeps
  SQLite (ops state only — there is no MySQL in this stack to migrate from).
  RAG gets its **own** store under `~/ai/rag/`; start embedded (sqlite-vec or
  LanceDB — zero new servers, one index file per project) and treat
  Postgres+pgvector as the scale-up path only if multi-user or very large
  corpora appear (D10).
- **Model lifecycle**: no separate inference process. The embedding model
  (e.g. the already-installed `qwen3-emb-8b`) + a generator live in the
  router store; a RAG deployment is a *personality* (§4.7) with its memory
  budget planned up front, and Phase B start/stop is its lifecycle.
- **Documents & isolation**: one directory + one index per project under
  `~/ai/rag/<project>/`; ingestion is a portal job (`jobs` table) that
  re-chunks + re-embeds on change; every search is filtered by project id,
  so cross-project leakage is structurally impossible, not policy.
- **MCP**: the RAG service speaks MCP (search / ingest tools, D11); the
  portal manages it as a managed unit like any other.

Sequencing: after B (service control) + D (gguf memory math).

### 4.9 The model table — finishing D6 (proposed Phase N)

Two live bugs in one week had the same root cause: nothing in the portal knew
what a model **is** or what it **costs**.

- The smoke test picked `…-00001-of-00003.gguf` because it was the smallest
  *file* (11 MB) and failed a healthy build by asking for 37 GB — a shard is a
  fraction of a model, and the header describes the whole thing.
- The models page lists names and sizes. It cannot answer "will this run here,
  at what context, on which backend, and what is it paired with?"

`app/core/modelmeta.py` is already a partial port of `~/ai-projects/gguf-indexer`
(D6): header-only GGUF KV walk, ~34 ms on a 94 GB file. The indexer's
`FileEntry` carries ~70 derived fields; the portal keeps 8, and the `models` /
`model_files` tables have **zero rows**. This is finishing a port, not starting
one.

**Schema** (sqlite, one row per *loadable model*, not per file):

```sql
CREATE TABLE model_index (
  id TEXT PRIMARY KEY,            -- model dir name = router model id
  path TEXT,                      -- what llama.cpp is pointed at (shard 1 if split)
  bytes INTEGER,                  -- the WHOLE model: all shards summed
  shard_total INTEGER, mmproj_bytes INTEGER, draft_bytes INTEGER,
  arch TEXT, quant TEXT, params_total_b REAL, params_active_b REAL,
  ctx_trained INTEGER, layers INTEGER, kv_heads INTEGER,
  kv_key_len INTEGER, kv_value_len INTEGER, full_attention_interval INTEGER,
  is_moe INTEGER, is_ssm INTEGER, is_vision INTEGER, is_embedding INTEGER,
  is_draft INTEGER, has_reasoning INTEGER,
  est_weights INTEGER, est_kv INTEGER, est_runtime INTEGER,  -- memory math
  est_ctx INTEGER,              -- context that fits this box, 0 = never
  est_inputs TEXT,              -- JSON: which fields produced the estimate
  scanned_at REAL, header_hash TEXT
);
```

`est_inputs` is not decoration. A derived number without the inputs that made it
cannot be re-derived when the formula improves, and cannot be defended when it is
wrong — which it will be at least sometimes (see the interval trap below).
`header_hash` makes re-scanning incremental without trusting mtimes.

**The memory math** (the part that was unfinished). Per token, K and V separately:

```
kv_layers  = ceil(layers / full_attention_interval)      -- hybrid!
kv_bytes   = kv_layers * kv_heads * (key_len + value_len) * dtype * ctx
weights    = bytes (all shards) + mmproj + draft
total(ctx) = weights + kv_bytes + runtime_headroom(compute buffers, graph)
```

The trap, measured on this machine's own models: `full_attention_interval: 4`
means only every 4th layer keeps a KV cache. Counting all layers gives —

| model | interval | KV at trained ctx (262K) | naive, all layers |
| --- | --- | --- | --- |
| Qwen3.5-0.8B Q4_K_M (0.50 GiB) | 4 | **1.75 GiB** | 6.25 GiB (3.6× over) |
| Qwen3.8-Flash-Next (94 GB, 48 L) | 4 | **2.48 GiB** | 9.94 GiB (4.0× over) |

**Corrected 2026-09-16; the ratios always were right, the GiB were not.** This
table first read 3.50 / 6.00 GiB (and 12.50 / 24.00 naive). Those were computed
at **f32** KV, while llama.cpp's runtime default is **f16** — exactly 2×, which is
why the mistake survived: every number in the column was wrong by the same factor,
so nothing looked inconsistent next to anything else. Recomputed from the headers
this time (`block_count`, `attention.head_count`, `attention.head_count_kv`,
`embedding_length`, `full_attention_interval`) at f16, which is what the estimator
in `app/core/modelindex.py` implements and tests.

The lesson is the one §4.9 already states and this is a second instance of: an
estimate must carry its **inputs and its assumptions**, not just its answer. A
dtype buried in a formula is invisible; a dtype named in the row would have been
caught on sight. So `kv_bytes()` takes the dtype as an argument and the index
stores the dtype it assumed.

**The formula above is the easy case, and it is incomplete.** Building N1
exposed two header shapes the two-line version cannot express, both present on
this box, and both got wrong before they were got right:

* **Head counts are sometimes a per-layer array, not a scalar.** gemma4 writes
  `attention.head_count_kv` as 60 entries (16 on full-attention layers, 4 on the
  rest); laguna writes `head_count` as 48. Reading an array as a scalar yields
  `None`, which silently drops the model from the estimate and reports it as
  unknown — the failure is invisible because "unknown" is the honest-looking
  answer. Laguna is a mixed case: scalar kv heads, per-layer query heads.
* **Sliding-window layers cache their window, not the context.** gemma4 marks 50
  of 60 layers sliding with `sliding_window: 1024`. Per-layer cost is therefore
  `kv_heads(layer) × min(ctx, window(layer)) × head_dim × 2 × dtype`, summed.
  Treating every layer as full-context priced gemma4's cache at **137.81 GiB for
  a 16.4 GB model** — pessimistic enough to refuse a model that runs fine, which
  is the same class of harm as underestimating it. With the window applied: **7.08
  GiB**.

And the interval still applies *underneath* an array: 48 entries with
`full_attention_interval: 4` means 12 cached layers, not 48. Ignoring that is a
4× overstatement, and the test that pins it asserts that a uniform 48-entry array
prices identically to the scalar `2`.

**One of the fixes for this was wrong in the other direction, and looked like
progress.** Folding `ctx` out of the sum and dividing back — `kvh × window // ctx
× …` — integer-divides first, so every sliding term became **0** and deleted 50 of
60 layers from their own model's estimate. A 137 GiB overestimate became an
underestimate, both plausible on screen. The only reason it was caught is that the
expected number was worked out by hand before the code was trusted. Two lessons,
then: work an example out before believing a fix, and make the test assert the
number rather than a range, because a range will accommodate either mistake.

Naive math would have looked at this box's 12 GB free and refused the 0.8 GB
model — the very model that made today's smoke test pass. Estimates are only
useful if they are *calibrated*: seed the constants from observed worker RSS
(this box: a 94 GB MoE resident at 31.5 GB with `--fit on --fit-target 8192`),
re-measure per model on first load, and keep the discrepancy visible on the
models page rather than quietly clamping.

**What it unlocks**, in order of value per line:

1. **A smoke test that cannot fail for the wrong reason.** `_model_candidates()`
   becomes `SELECT path, bytes FROM model_index WHERE bytes + kv(128) + headroom
   <= ? ORDER BY bytes LIMIT 3` — with the refusal logic it grew today (§6.11)
   kept exactly as is.
2. **Models page**: one column set that answers *what is it* (arch, quant,
   params active/total, MoE, SSM-hybrid, trained ctx), *what is it paired with*
   (shards, mmproj, draft), and *can it run here* (fits / fits at reduced ctx /
   never), plus a search that matches on those facts instead of filenames.
3. **Presets with personalities (§4.7)**: the indexer's `router-presets.py`
   already contains a *measured* rubric for this box — MoE and small models to
   Vulkan, large dense and reasoning to ROCm, with the llama-bench numbers
   written in the comment — and the live `presets.ini` has a single `[*]`
   section, so none of it is applied. A personality is that rubric plus a model
   subset, generated, diffed, then applied.
4. **Health**: models that can never fit (advisory), shard sets that are
   incomplete (currently unloadable and silent), dangling model symlinks (3 of
   them right now — one of which crashes the indexer's own scanner).

**What a personality needs that a file listing cannot answer** (§4.7): to fill
"planning model / RAG model / autocomplete model" from a library of 16, the table
has to carry capability-shaped facts, not just sizes — trained context (a 32K
capable model is a poor RAG embedder), params *active* vs total (agentic work on
an MoE is a different latency story than on a dense model of the same size),
modality (vision, embedding), and whether it is a draft sidecar rather than a
model. Each of those comes out of the GGUF header, so they belong in the table;
what does *not* belong there is a verdict like "good for coding". The table states
facts and the personality says what it wants; a score baked into the index is a
hypothesis nobody will ever re-test.

**Phasing.** N1 reader + table + incremental scan (read-only, no UI).
N2 memory math + the smoke query + tests. N3 models page.
N4 presets personalities: generate → **show a diff** → apply through the existing
gated restart job. N4 is the only step that writes anything the router reads, so
it ships last and there is exactly one generator: `presets.ini`'s header says
which tool wrote it, and `tools/phase1_build_model_tree.py` retires at that
moment rather than continuing to disagree with it.

**N1 + N2 landed 2026-09-16/17** (`bc2d76d`, `117f5a7`): `app/core/modelindex.py`
with `model_index` in `portal.db`, `scan_library` (shards collapsed to whole
models, incomplete sets and dangling symlinks reported as problems rather than
silence), `kv_bytes_at` / `ctx_that_fits` / `est_total`, and `fits_query`.
**24 tests** in `tests/test_modelindex.py`; full gate 354 passed, 1 skipped.
Against the real library: **15 models, 0 problems**, Flash-Next correctly
reported as 3 shards / 87.2 GiB whole (not the 49.8 GB shard), gemma-4-31B at
27.5 GiB total instead of 158.3, and Laguna estimable for the first time.

Three things worth keeping from N1, because they were not the plan's hard parts:

* **`ctx_that_fits` is capped at the model's trained context.** Uncapped it said
  8388608 tokens for the 87 GB model — true arithmetic, useless answer, because
  past its trained context a model is not the model. `est_ctx` is now a power of
  two at or below the header's `context_length`, so 0 means "never" and cannot be
  confused with "unknown" (which is `est_total IS NULL`).
* **An estimate that cannot be computed stays `NULL`, never 0.** `fits_query`
  filters `est_total IS NOT NULL` precisely so that ignorance cannot win a
  selection. Zero would sort a model to the front of "what fits".
* **Only shard 1 carries the architecture.** Shards 2–3 of a split model parse as
  valid GGUF with `arch=None`. Any probe that takes `glob("*.gguf")[0]` reads the
  wrong file and concludes the reader is broken — which it did, twice, in this
  session, at the reader's expense. `model_index.path` pointing at part 1 is
  load-bearing, not a convention.

**Unlock #1 was reported as shipped before it could run** (`5d6e57b` corrects
this). N1 created the table and N2 created `fits_query` on top of it, and I called
that the smoke test being fixed. Nothing created or populated `model_index`: on the
deployed database it was absent from `sqlite_master`, so `_indexed_candidates`
threw on every call, hit its own `except`, and fell back to the file walk it was
meant to replace. The improvement was dead code behind a fallback that always
fired, and it passed every test I had — because those tests stubbed the index.

**A fallback that always fires is not a fallback, it is the implementation.**
Graceful degradation is precisely the mechanism that makes "silently does nothing"
indistinguishable from "working", so anything with a fallback needs a check on
*which branch ran*, not just that the call returned. Wiring the index into app
startup (a daemon thread, off the request path) immediately exposed a second bug
the old code would have swallowed: `rescan()` writes an `events` row, and the base
schema is created lazily by `_settings()`, so before the first request it failed
with `no such table: events`. Now: `journalctl --user -u ai-portal` shows
`model index: 15 model(s), 0 problem(s)`, and the production DB has 15 rows.

**Capability facts added for N3**, all read from headers rather than guessed from
arch or filename: `expert_count`, `expert_used_count`, `is_moe`, `has_ssm_state`,
`has_vision`, `has_mmproj`. Three of those carry a decision worth keeping:

* **`expert_used_count` is what makes MoE a latency fact instead of a size fact.**
  8 active experts in a 256-expert model is a different machine than 256 dense at
  the same footprint. This box: 6 MoE models, 256–512 experts, 8–10 active.
* **The SSM column is `has_ssm_state`, not `is_ssm`.** Every qwen35-family export
  here carries `ssm.conv_kernel` and `ssm.state_size` — *including the dense ones*
  — and none carries `ssm.count`. A column named `is_ssm` would be read as "is a
  Mamba model", which is false about half the qwen35 family. The field states what
  the header states and nothing more.
* **`has_vision` (the header's claim) and `has_mmproj` (the disk's) are separate
  columns.** "Claims vision, lost its projector" is a real state; collapsing the
  two would report it as one or the other and hide the mismatch.

Params are absent from every header on this box and stay `NULL`. Back-computing
them from file size and quant would bake a ~1.2× guess into a column that reads
like a fact — the exact category error §4.9 warns about. The page renders "—".

**N3 shipped 2026-09-18** (`31b6860`, `6e52d82`, `b9a0e05`): one library table that
answers *what is it / what is it paired with / can it run here*, a live light replacing
the separate router table, capability facts read from the chat template, and a peak-RAM
figure a loader can plan against.

**`ctx_that_fits` divided by a one-token slope and under-reported every window model.**
`avail // kv_bytes_at(meta, 1)` assumed KV grows linearly with context. It does not: on
a sliding-window model the layers under the window stop growing once ctx passes it, so
gemma-4 charges all 60 layers at ctx=1 and only 10 at ctx=262144 — the one-token slope
was ~2× the true marginal cost at scale. It printed "reduced max 128K" for a 31B model
with 84 GiB of its 108 GiB aperture free and a full 262144 cache costing 7 GiB. Now a
search of the power-of-two ladder using the real cost function, bounded by trained
context: ~21 evaluations of a closed-form sum, never off by more than one doubling, and
monotonic in budget by construction. *The error pointed the safe direction, which is
exactly why it survived review — an estimate that under-promises reads as caution. A
conservative estimate is not a correct one, and it costs capability silently.*

**Two numbers, two questions, and the gap must be pinned.** `verdict()` answers "is
weights+cache within the aperture" (a display fact). `peak_bytes()` answers "what is
resident if this loads" and adds `HEADROOM_BYTES`, because the aperture is not the only
consumer of RAM. A model can therefore be labelled *fits* at a budget `peak_bytes` says
is not enough to load it. `test_peak_is_stricter_than_the_displayed_verdict_by_exactly_
headroom` pins the difference at exactly `HEADROOM_BYTES` so a future change to either
side is a decision, not a drift. `peak_bytes` returns `None` rather than a guess: a
scheduler given 0 loads, one given `None` refuses.

**Capability facts from the chat template, not the vocabulary.** A GGUF carries its own
`tokenizer.chat_template` — kilobytes of Jinja, present on 15 of the 16 models here —
and the delimiters it must spell out literally are evidence of what the weights were
trained to speak: `<tool_call>`, `"role": "tool"`, `<|think|>`. Library counts:
chat 15, system 14, tool-results 14, tools 12, thinking 1.

Two traps in getting there, both worth the length of their write-up:

- **An alternation of delimiters must escape every pipe that is punctuation.**
  `<\|tool_call|>` was read by the regex engine as "`<|tool_call` OR the empty string",
  matched at offset 0 of every template, and reported tool support on 13 of 15 models.
  A pattern whose alternation can be empty does not fail — it reports true for every
  input, forever.
- **Substrings lie about Jinja.** `"tools" in template` matched the loop variable
  `{% for tool in tools %}`, which proves nothing about what the model emits. Matching
  is structural now. The correction in the other direction also mattered: anchoring the
  comparison operator right after the closing quote matched JSON `{"role": "tool"}` but
  missed `message["role"] == "tool"` — the closing bracket comes first in Jinja — so
  tool support and the system role went unreported on every Jinja-templated model.
  Under-reporting also reads as caution and also ships.

**What the tokenizer cannot tell us on this box, recorded so nobody re-derives it.**
`tokenizer.ggml.tokens` is a 262144-string array; when this reader parsed it, it
returned a 24-character *string* — garbage that looked exactly like data.
`tokenizer.ggml.token_type` is parallel and equally large. `tokenizer.ggml.added_tokens`,
the short list where special tokens would live, is present on **0 of 16** models. So
there is no token-derived role column, and the constraint is a comment rather than a
column that would always render blank and read as "this model declares nothing" when
the truth is "we have no way to ask".

**Only shard 1 carries the tokenizer.** Same rule as the architecture: shards 2–3 parse
as valid GGUF with `arch=None` *and* `has_chat_template=False`. A probe that globs
`*.gguf` and takes the first hit will report a sharded model as having no chat template
and no architecture. This trap has now cost this project measurements nine times.

**The live light replaced the router table.** "Live" is a property of a model, so it
belongs on the model's row. Matching runs through `modelindex.router_key()`, because the
router registers some models under a HuggingFace-style alias (`unsloth/Qwen3.8-27B-GGUF:
Q8_K_XL`) that no directory name resembles. A raw compare shows a serving model as idle,
and *that* is the one wrong answer this column can give: "not running" invites loading a
second copy of a model already taking traffic, which is how you double the memory cost of
a model that is up. Live models with no library row are listed in their own section — the
merge must not quietly become a filter, and "16 models, all accounted for" is a lie when
the router is serving a 17th.

**`_declared_type()`, and a helper that quietly did nothing.** The migration ALTERed
every missing column in as `INTEGER`, so `template_markers` (TEXT in SCHEMA) would have
arrived as INTEGER on an upgraded box and TEXT on a fresh one; SQLite stores a string in
an INTEGER column without complaining, so the only symptom is surprising sort order. The
first implementation then returned its TEXT fallback for *every* column: SCHEMA puts
several columns on a line, so the token after a name is `INTEGER,` — type plus separator —
and `INTEGER,` never matched the allowed set. A helper written to remove a second opinion
had become a no-op. Verify a new helper returns different answers for different inputs.



**N4 shipped 2026-09-20 (read-only half): `app/core/personality.py` generates, `/personalities`
shows, and nothing writes.** `render()` returns text, a unified diff, and the budget with its
inputs; `describe()` returns the card the page renders; `parse_current()` reads what is already
on disk so the diff is against reality rather than against an assumption. `safe` and `default`
exist and are not editable in place. Router-controlled keys are refused, not emitted.

**D7 is answered: show the diff, a human approves.** A settings-style editor for global and
per-model values is the agreed shape of the next step, and it is why the key definitions were
written as a registry first. `personality.SETTINGS` gives each writable key a `kind`, a
`section`, a `default`, `choices`, a `help` line, and an `accepts` predicate. The generator
validates against that table and the page renders the same table, so the text a person reads
and the rule a generated file is checked against cannot drift apart. `ctx-size` is in the
registry and deliberately **not** in `GLOBAL_DEFAULTS`: a hard-coded context cap is what the
old per-model sections on this box did, and it removed capability silently.

Two things the registry had to be corrected for, both found by comparing it against the live
router rather than against the README:

- **ini values are all strings, and a validator that forgets that validates nothing.** The
  first `check_setting` required `isinstance(v, int)`, and it rejected **34 of 34 values the
  router itself reports** — `threads = 16`, `stop-timeout = 15`. That reads as strictness and
  is the validator disagreeing with the file format it is validating. `coerce_setting` is where
  '16' becomes 16; it stays strict about the rest ('16.5' and 'sixteen' remain strings and are
  then refused). Cross-checked against `GET /models` → `status.preset` on all 17 models: 68
  values checked, 0 rejected.
- **`mmap` and `mlock` were removed from `GLOBAL_DEFAULTS`, not added to it.** They appear in
  no preset the live router resolves and in no `llama-server` argument list this box documents.
  A settings page that offers a key nobody implements is worse than one that omits it.

**`GET /models` exposes the resolved preset, but only in `status`, and only for a loaded
model.** `args` and `preset` are not top-level keys on a model entry — they live inside
`status`, the same dict that carries `value`. A probe that reads `m["args"]` gets `None` and
reports "the router exposes nothing", which is how this session initially concluded 0/17 models
had launch arguments. Fourth time this endpoint has produced a wrong answer through a
dict-vs-string or wrong-key mistake; the shape is now asserted in tests.

**Three bugs the new tests introduced, each found before committing:**

- **A read-only guard that only guards in the right test order.** The obvious version —
  snapshot `presets.ini`, make the requests, compare — **passed while `render()` was mutated to
  write the file**, because an earlier test in the same run had already written it: `before` and
  `after` were both the mutated output and equalled each other. The guard now plants a sentinel
  no personality could ever generate, so any write is observable from whatever state the run is
  in. A guard that only catches a change when nothing has changed yet is a guard against test
  order, not against the bug.
- **A Jinja typo that rendered a blank instead of an error.** The template read
  `b.get('worst_bytes')`; `Budget.as_dict()` calls it `worst_case_bytes`. Jinja answers a
  missing key with undefined, the `{% if %}` never fires, and the page renders both cards with
  no budget at all — 200 OK, correct markup, no number. Asserting a selector would have passed;
  the tests and the UI spec assert the formatted figure (`heaviest 4.1 GiB of 108.0 GiB (103.9
  spare)`) instead. Fixing the name exposed a second, subtler version of the same class: the
  figure was wrapped across three template lines, so `textContent` contained the newline and
  fourteen spaces of indentation and nothing matching it on one line could work. The figure is
  now built into one variable.
- **A fixture that could not be parsed.** The UI sandbox's "model" was 2048 null bytes: valid
  enough to list as a file, not valid enough to sniff, so `model_index` stayed empty after the
  startup rescan and every index-backed page rendered its empty state forever. It now writes a
  real GGUF through the indexer test's own writer — one fixture definition, so the reader cannot
  drift from the fixture without one of the two suites noticing — and seeds `safe`'s named model
  too, because a fallback the sandbox lacks makes the `safe` card legitimately budget-less, and
  an assertion that only ever describes `default` passes even if the fallback card stops
  computing one.

**Rewriting the job bar to nodes, and the two traps in doing it.** Removing `innerHTML` from
`base.html` sounds mechanical and produced two bugs of its own. Escaping a value *before*
putting it in a text node double-escapes: a job target of `a<b>c` renders to the user as the
literal characters `a&lt;b&gt;c`, because a text node is never parsed as markup and therefore
needs no escaping. And `Element.append()` takes nodes as separate arguments — `' ' + text(x)`
concatenates, which coerces the node and yields `" [object Text]"` in the bar. Both were caught
by running the real harness rather than a reproduction; the mutation check (replacing the step
text) confirms the harness reads the rendered string and not merely the element's existence.

**The apply path landed 2026-09-22** (`2eb70e1`, `46aa65c`). It has not changed what this box's
router reads: `presets.ini` is still the file it was, and nothing in the test suite can reach it.
What exists now is `app/core/presets_store.py` — snapshot from disk before every write, restore by
bytes, prune to the newest 20 — plus the gated write path and settings editor in §4.10.

**Lessons carried over, deliberately:**

- The indexer crashes on `os.stat()` of a dangling symlink — three exist in this
  library. Any scan of `~/ai/models` must treat "link that goes nowhere" as a
  first-class, reported state, not an exception.
- Its parser skips tensor descriptors on purpose: the `gguf` library's per-tensor
  Python loop costs ~3.5 s/file where this costs 34 ms. Keep that decision.
- Every rubric constant in it is annotated with *what was measured, when, and
  under what load*. Copy the habit, not just the numbers.
- Auxiliary files (`mmproj-`, `mtp-`, `draft-`, `imatrix-`) are not models and
  always lose a naive size contest. The portal now agrees via one shared regex.

#### 4.9.1 The hub cache is a second source of models — and the fence does not fence it (2026-09-29)

The library emptied itself on 2026-09-25 (§4.10.5) and every model pulled since
then went into the Hugging Face hub cache. The portal saw none of it —
`scan_library()` walks one directory — while the router saw all of it. That
asymmetry is the finding, and it was measured rather than assumed:

| probe (all on the deployed b11146) | result |
| --- | --- |
| router with `--models-dir /tmp/fence --no-models-autoload`, that dir holding no models at all | `Available models (5)`, every one of them `[cache] org/repo:QUANT` |
| `POST /models/load` for `lmstudio-community/codegemma-2b-GGUF:Q5_K_M` — a model **not** in `--models-dir` | `{"success": true}`; a worker started and the GTT aperture rose. A cache model loads on demand whether or not the portal’s directory fence contains it |
| b11146’s help for a way to switch cache discovery off | none. The model-management options are `--models-dir`, `--models-preset`, `--models-max`, `--models-autoload`; `--cache-list` only prints the cache, and `--offline` *forces* cache use |
| same router with `HF_HUB_CACHE=/tmp/empty-hub` | `Available models (0)` — the cache view disappears |
| same env, but with one entry under `--models-dir` | that entry stays: `[models_dir] codegemma-local`. The env fence takes the cache, not the directory |

**What this does to §4.7.** That section defines a personality’s `models` as “a
symlink farm of model dirs the router may see at all. Everything not in it cannot
be loaded, cannot be OOM’d by, and does not appear in clients.” On this build that
is true of the directory source and false of the cache: hub-cache models are
discoverable and loadable under their own ids no matter what the farm contains.
So the budget guarantee in §4.7 — *the heaviest `models_max` combination must
fit* — is conditional on the loadable set actually being closed, and today it is
not. `models_max` and the portal’s own refusal still bind the loads the portal
issues; they do not bind a client that names a cache model directly. Candidate
remedy, measured but **not applied**: set `HF_HUB_CACHE` in the router unit to a
directory the operator controls. That restores a closed set without touching the
cache contents.

**The symlink farm over the cache was tried and rejected.** Three shapes — a real
directory holding symlinked `.gguf`s, the snapshot directory itself symlinked, and
a preset section whose `model =` points at the cache blob — all produced the same
thing: the `[cache]` entry stayed, the farm entry appeared beside it, and
llama.cpp’s own dedup path (`hiding cache model name=%s (deduplicated by a
preset)`) never fired. Two ids for one set of bytes, each with its own preset
section, alias namespace and load. That is worse than the problem, because
presets key on the id the router uses: a section for the farm name configures a
model nobody loads while the operator keeps loading `unsloth/Qwen3.8-27B-GGUF:Q8_K_XL`.
Short names come from `alias =` in the model’s own section instead — measured to
work on a cache id, with nothing written into the cache.

**What landed.** `scan_cache()` mirrors the runtime’s rules rather than guessing
at them, because a preset section configures the model whose *name* it matches —
an id the router does not use is a settings block that silently does nothing:

| rule | source |
| --- | --- |
| shard part 1 only; non-empty tag; `mmproj`/`mtp-`/`eagle3-`/`dflash-`/`dspark-` prefixes are not models; `id = repo:TAG`, first one wins | `common/download.cpp:964` |
| shard suffix, then trailing `[-.]TAG` upper-cased; no tag ⇒ not offered | `common/download.cpp:506` |
| `refs/main` else the first readable ref, and **only that revision** is scanned — a stale snapshot directory is not a model | `common/hf-cache.cpp:374` |
| sidecar pairing: directory prefix must match, then deeper match, then `-TAG.`, then nearest quant — so a repo-root `mmproj-BF16.gguf` still attaches to a model filed under a quant subdirectory | `find_best_sibling()` |

Rows carry `source`, and the models page labels hub-cache rows so the operator can
tell what the portal manages from what llama.cpp found. A shared id goes to the
library (the router’s own cascade, `server-models.cpp:702`) — unreachable today,
since a cache id contains a slash and a library id is a directory name — kept
because `INSERT OR REPLACE` would turn an unguarded collision into a silent
overwrite. The naming rule has an adjudicator rather than a reviewer:
`llama-server --cache-list` reads the same fixture hub with the runtime’s own code,
and the two sets must be equal.

Open items:
- `HF_HUB_CACHE` is **not** set in the router’s unit, so the cache is open on this
  box now. Fencing it is a unit edit, and it changes which models the operator can
  load by name — worth deciding deliberately, not by accident.
- If the set stays open, the models page should say so next to the budget figure:
  “worst case” is computed over the personality’s set, which is not the loadable set.
- `--cache-list` on an older build does not exist; the oracle test skips there
  rather than failing the suite over the installed version.

#### 4.9.2 The cache name rule is the router's — and case is load-bearing (2026-09-30)

The operator downloaded `Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF` and said plainly: this
is a multi-token-prediction head, it belongs with the matching full model, and probing
it alone hangs. The router listed it as a loadable model. **The portal refused to name
it at all** — no index row, no preset section, absent from the Models page. Cause:

```python
if any(s in base.lower() for s in _HF_NOT_A_MODEL) or tag is None:
```

`_HF_NOT_A_MODEL` holds `mtp-`; lower-casing turns `…-Coder-MTP-Q8_0` into a match. A
model the router can load and the portal cannot see is a model that can never be given
per-model settings, and the failure is silent in both directions.

Rather than fix the one case, I asked the runtime what its rule actually is, over a
fixture hub, 20 name shapes (`llama-server --cache-list`):

| name shape | b11146 | portal before | portal now |
|---|---|---|---|
| `Qwopus3.5-4B-Coder-MTP-Q8_0.gguf` | `:Q8_0` | hidden | `:Q8_0` |
| `model-Q4_K_M-mtpr.gguf` | `:MTPR` | hidden | `:MTPR` |
| `model-Q4_K_M-mtp.gguf` | `:MTP` | `:MTP` | `:MTP` |
| `model-coder-mtp-Q8_0.gguf` | not a model | not a model | not a model |
| `model-Q4_K_M-eagle3-Q8_0.gguf` | not a model | not a model | not a model |
| `model-Q4_K_M-imatrix-Q8_0.gguf` | `:Q8_0` | hidden | hidden (deliberate) |
| `Q6_K/model.gguf` | not a model | not a model | not a model |
| `deep/nest-Q6_K.gguf` | `:Q6_K` | `:Q6_K` | `:Q6_K` |

So the markers are **case-sensitive substrings of the basename** — `mtpr` survives,
`mtp-` does not — and `imatrix` was missing from the portal's list entirely.
`gguf_filename_is_model()` (`common/download.cpp:661`) is the transcription source;
the fix is `_hf_path_probe()` plus the corrected list.

Two differences from the runtime are kept on purpose, and both are asserted:

- **imatrix.** b11146 lists `model-Q4_K_M-imatrix-Q8_0.gguf` as a model. An imatrix
  file is calibration metadata with no weights to speak of; pricing it as a model
  would misstate the fit budget, so the portal withholds it and the oracle fixture
  leaves that one shape out.
- **Half-downloaded shard sets.** The runtime names them (part 1 carries the header,
  so there is something to name); the portal refuses to price them, because the byte
  total is a fraction of the real one and the load will fail. The `incomplete-shards`
  problem now carries the **router's own id**, so the two systems are describing the
  same thing in the same words.

One shape I could not explain and did not guess: a lone
`sub-eagle3-x/model-Q8_0.gguf` is not listed by this build, yet the same basename wins
a tag in a repo holding several candidates. It is left out of both tables with a note
rather than encoded as a rule.

Tests: `_HF_NAME_TABLE` pins the portal's opinion with no binary involved;
`test_the_portal_and_the_runtime_agree_on_what_the_cache_holds` asks b11146 about the
same shapes; `test_an_incomplete_shard_set_is_named_the_way_the_router_names_it` covers
the deliberate difference. Breaking case-folding or dropping `imatrix` fails two tests
(check that the tests still fail when the fix is reverted).

#### 4.9.3 Capability columns: autocomplete, self-drafting, ranking (2026-10-02)

The index used to answer one question — can it run here. A personality has to
answer a second one before it picks: what is this file *good for*. Three
capabilities are readable out of the header for ~105 ms per model and never
appear in a repository name, so they are now columns, and the reading of them is
one function.

**The defect that made the tokenizer column pointless.** `modelmeta` summarised
any string array longer than 64 entries as the placeholder `"<250002 strings
skipped>"`. Two silent consequences: `vocab_size` was `len()` of that string —
24 — and searching a 24-character placeholder for the fill-in-the-middle specials
finds nothing. Every generative model on this box has a vocabulary above the
threshold (248 320 on the qwen35 family), so the FIM rule would have been dead on
arrival and *looked* like it worked, because the reader never raised. Fix: the skip
stays (a header is not the place to materialise 250k strings), but the two arrays
the capability rules need are named — `_KEEP_ARRAYS` = `tokenizer.ggml.tokens`,
`tokenizer.ggml.added_tokens` — and read whole, then dropped; the vocabulary
itself is never stored.
`test_a_real_sized_vocabulary_is_read_whole_by_the_reader` counts 151 936 strings
through the real reader, asserting the count rather than the absence of a
plausible-looking placeholder.

**Four new columns**, each the file's own statement, none derived:
`vocab_size`, `has_fim` (three-valued — read-and-present / read-and-absent /
unaskable), `nextn_layers` + `draft_tensors` (kept apart because they can
disagree: a quantiser can drop a head the header still promises, and then
`--spec-type draft-mtp` starts and drafts nothing), and `pooling_type` (4 = the
runtime answers `/rerank`, measured 52 ms for 2 results; 1 = embedder, measured
501). Tensor names are walked, not read: `blk.N.nextn.*` costs 31 ms on the
51.05 GiB shard.

**One derived rule, not four.** `modelindex.capabilities(row)` turns those columns
into `{label, tone, note}` — `autocomplete` / `no FIM` / `fim?`, `draft x1` /
`head missing`, `reranker` / `embedder` — and it is the only place allowed to
combine them. The models page iterates it; a future slot-picker will call the same
function instead of re-deriving. Hover text is not decoration: `no FIM` has to say
why it is not the same as `cannot run /infill`, which is false (`/infill` splices
prefix/suffix into any model) and would cost a working slot.

Two traps this surfaced, both pinned:

* `has_fim is True` — SQLite hands back `1`, so every row fetched from the table
  read as "vocabulary unreadable" while every in-memory scanner row read
  correctly. Wrong on the page, right in the unit test: the worst possible split.
  Truthiness for the two known states, `is None` for the third, with a test that
  feeds the stored representation rather than the computed one.
* The FIM specials arrive with the base vocabulary. `qwen3-reranker` declares 4 of
  them and is not an autocomplete model; `pooling_type 4` is what says so. So the
  badge reads "the tokenizer can express the holes", never "use this one".

Also fixed on the way: `find_draft` in the bench could hand a model its own file
as `--spec-draft-model` (lower-cased marker test matched "MTP" inside the
target's name — the hang the operator hit). The naming rule is now shared with the
index via `NOT_A_MODEL_MARKERS`, case-sensitive.

The rubric, the measurements and the anti-signals are written up in
**docs/MODEL_SIGNALS.md**; that file is what a future indexer or hub scraper
should be checked against, not this section. Tests: `tests/test_model_signals.py`
pins every shape including both disagreements, cross-checks the index's tensor
walk against `slot_bench.nextn_tensors()` on every cached model, and
`test_the_models_page_says_what_a_model_can_be_used_for` fetches `/models` so the
derivation cannot stop reaching the template (deleting the one line in the route
fails it).

### 4.10 Writing presets.ini — the apply path and the settings editor (landed 2026-09-22)

N4 is the first step in this project that writes a file the router reads at load time, so the
interesting part is not the write, it is everything arranged around it.

**Order is the safety property.** `personality.apply()` refuses at four points before anything
touches disk — plan does not fit, plan cannot be priced, a router-controlled override, a key with no
registry definition — then snapshots from disk, writes, **re-reads the file** and re-checks it. If
what landed is not what was validated it rolls back. A partial write discovered by the router at
load time is the worst possible place to find out.

**A snapshot is taken from disk, never from memory.** If `apply()` snapshotted the text it was about
to write, a bug in the writer would corrupt the backup too and rollback would restore the damage.

**A rollback that is only attempted is not a rollback.** The first version called
`presets_store.restore()` and then reported "restored snapshot `<stamp>`" without looking at the
file. The same I/O fault that corrupts an apply can defeat the restore, and a message that says
"restored" while the file is still broken is worse than no message: the operator stops reading.
`_rollback()` now compares the restored bytes against what was there and says so either way. It was
mutation-proven by reverting both call sites to the unverified form, which fails with *"apply must
prove the rollback, not assume it"*.

**Gating, three layers.** `confirm` must be true (same rule as a router stop or a release delete);
the job kind `presets-apply` is in `updater.DISRUPTIVE` because its last step restarts
`ai-llama-router`, so it will not start on top of a build; and `apply()` refuses on its own when the
budget says no. A refusal surfaces as a failed job with the reason in its log, never as a 500.

**`check_generated()` validates the emitted text, not the sections dict.** It re-parses the file the
router would read. The retired generator emitted a file `configparser` rejected; that is now a
refusal here rather than a router that starts with half a configuration.

**`lazy-mode` was missing from the registry, and it was found by reading live state.** The running
router resolved `lazy-mode = on` for every model and `--lazy-mode` was in the worker argv; the
registry had no definition for it, so a perfectly legal `lazy-mode = on` would have been refused as
"no definition in the settings registry". It is registered but deliberately **absent from
`GLOBAL_DEFAULTS`**: registering it is what lets the editor offer it and the validator accept it;
keeping it out of the defaults is what stops `apply()` emitting a value a hand-started router
already sets on a command line that outranks `presets.ini` anyway.

**The editor's controls come from the registry.** Fields are rendered from `SETTINGS`, so the page
cannot offer a control for a key the validator would refuse — the form and the rule are one table.
`normalize_proposed()` coerces before validating, because a form submits strings and `threads = 16`
arrives as `"16"`; a validator that demands an int rejected every value the live router itself
reports.

**A silent no-op found while building it.** A per-model edit for a model the personality does not
cover returned 200 and was discarded: `render()` builds sections by iterating `resolve_models()`, so
the editor showed the value, preview approved it, and the generated file did not contain it. Refused
now, with the two causes kept distinct — no library entry, versus in the library but outside this
personality's model set — because "the router will ignore this section" and "this personality does
not cover that model" need different fixes.

**Tests that asserted fictions, corrected rather than worked around:**

- `apply(personality, None)` was expected to refuse. It does not: `personality.py:437` treats
  `None` as *go measure it* and calls `modelindex._pool_bytes()`, which answers 108 GiB here.
  `FITS_UNKNOWN` is reached by an unpriceable model, which is what the test now constructs, and a
  second test pins the `None` behaviour so the mistake is not re-made.
- A test for "post-write revalidation restores the file" poisoned `render()`, which the **pre**-write
  check catches — so it was a test of the pre-write gate wearing the name of a test about the
  post-write one. It now poisons `check_generated()` on its second call.
- `budget_for()` was monkeypatched to return a dict; `render()` calls `budget_for(...).as_dict()`, so
  the job died with `AttributeError` — a failure either way, but for a reason the fixture invented.
- A fixture whose injected budget was a `Budget` object while `apply()` indexes a dict.

**The invariant test changed with the code.** `test_page_offers_no_write_control` forbade the string
`/api/personalities/apply` anywhere in the page — correct for the read-only half of N4, wrong once
the route shipped. `test_the_pages_write_control_cannot_fire_by_itself` asserts what actually has to
hold: Apply starts disabled, no control is `type=submit`, and no `action=` or `href=` points at a
write route.

**Harness findings.** `tests/ui/spec.json` field counts were first written as 10 and 13; a mutation
that dropped one key **passed**. Pinned to the registry (12 global-eligible, 14 per-model), the same
mutation fails with *"13 element(s), expected at least 14"*. And the harness caught a dead control I
had written: "clear this model's edits" did nothing observable when the model had no edits — exactly
when an operator presses it, because they believe they changed something and cannot remember what.

**Two suite-level defects this exposed, both about the machine running the tests:**

- `test_a_quiet_machine_has_nothing_to_report` was reading **this box's tmpfs occupancy**.
  `health.disk()` calls `shutil.disk_usage(paths.AI_ROOT)` and `AI_ROOT` lives under `/tmp`, a 62 GiB
  tmpfs; the test passed while `/tmp` was roomy and failed once enough test runs had filled it. The
  `tree` fixture now pins the probe. Mutating the pin to 20 GiB makes the test fail with the real
  advisory, which is what proves the pin is doing work rather than hiding a bug.
- `/tmp` was at 49 GiB because repeated `AI_PORTAL_HOME=$(mktemp -d)` runs left four 13 GiB homes
  behind, and a fixture reached model sizes by writing gigabytes of zeros. Homes were removed and the
  fixture now reaches size by `truncate()`: a 4 GiB model fixture allocates 4 KiB.

**Still deferred:** persisting custom personalities (the editor proposes, previews and applies; it
does not save), the §4.7 `models` symlink farm, and using `presets_history` — the table exists in
`portal.db` with zero rows, while snapshots live on disk under `state/rollback/`.

#### 4.10.0 A line that says nothing is not a harmless line (2026-09-23)

The failure mode to avoid is a presets.ini that looks configured and is not: keys written with
values equal to what llama-server would have chosen anyway. Those lines are not documentation.
They are a copy of one release's number, and the day upstream changes that number the pinned copy
becomes a setting nobody agreed to — which is how `mmap` and `fit` reappear in a file someone
thought they had cleaned.

So `SERVER_DEFAULTS` records what the binary on this box actually defaults to — read from
`llama-server --help` (b10964), with the two router-only keys from source: `DEFAULT_STOP_TIMEOUT 10`
(`server-models.cpp:43`) and `models-max` 4 — and `render()` drops any key whose value equals it.
Omission is **reported**, in `omitted` with a reason, on the same response that carries the diff:
an operator who set a value and finds it absent has to be able to learn why it is not there.

`ctx-size` has two no-op shapes, and the second is the one an editor reaches for by reflex:

- `ctx-size = 0`, which already means *loaded from the model*;
- `ctx-size = <trained maximum>`, which is where the model caps whether or not the file says so.

A model section emptied by omission is deleted rather than left as a header with nothing under it:
the router would list that model as carrying a custom preset that sets nothing.

**Capacity is a refusal, not an advisory**, because presets.ini is read at load time by something
that will not explain itself. Two ceilings, checked independently — clearing one and failing the
other is a real value: a context above the model's trained maximum, and a context whose cache
cannot be paid for in the aperture. Verified against the live library rather than fixtures:
`524288` refused for a `262144`-trained model, `32768` accepted, `262144` omitted with its reason,
and the shipped `default` personality still renders byte-identical to the file on disk.

**The capacity check exposed two defects in the index it was built on, and one claim in this
document.** `peak_bytes()` had a branch for "price this model at another context" that had never
once run — it passed the scan row to an estimator speaking a different vocabulary, and returned
`None` for all 16 models on this box. Repricing is exactly what an explicit `ctx-size` needs, so
§6.12.5 is about making it real, and about the direction a fallback is allowed to be wrong in.

#### 4.10.1 Context is the model's to decide, not the portal's (2026-09-24)

The rule: **nothing the portal writes sets `ctx-size` unless the person composing the personality
wrote it.** A model runs at the context its own header declares. The portal does not choose a "safe"
context, does not pre-fill one in the editor, and does not carry one in a builtin.

This is a rule rather than an observation because of what this box started from: the migrated tree
capped **every** model at `ctx-size = 65536` — 262144-trained models and a 1048576-trained one alike
(`tools/phase1_build_model_tree.py`, with a comment saying so). A cap that low is invisible
capability loss: model, page and user all agree the model is shorter than it is, and the file never
says who decided. The live `presets.ini` today has no model sections and no `ctx-size` in it at all.

Two things can still shrink a context, and neither is the portal deciding for the operator:

| who | when | where you can see it |
|---|---|---|
| llama.cpp `fit` (default `on`) | at load, for arguments left unset only | its own log: `context size reduced from 1048576 to 957184` (§6.12.7) |
| the operator | by writing `ctx-size` in that model's section | the line itself — and a refusal if it asks for more than the model or the aperture can pay for |

So "no other choice in order to fit" is handled by `fit`, which measures free device memory and
reduces what it must. The portal's job is the reporting: the models page says which models do not fit
at their trained context and states that it set nothing, and the `fit` description says it may pick a
smaller context than the model can address. The portal's own estimate of the largest fitting context
stays an estimate — shown as one, never written into the file the router reads.

Guarded by `test_no_personality_limits_a_context_the_author_did_not_choose` (no builtin override, no
global default, no line for a personality that names models) and, on the other side of the rule,
`test_an_explicit_context_choice_survives_everything_else` — because a generator that dropped
`ctx-size` on the floor would pass the first guard while breaking the operator's decision.

#### 4.10.2 What presets.ini can carry, and what startup loading actually means (2026-09-24)

The question worth answering before adding a settings page for it: are there llama.cpp options that
`presets.ini` **cannot** carry, which would then have to live in the unit's command line and be kept
in step with the preset file? Measured key by key against the binary on this box, with `mmap` as the
positive control:

| | |
|---|---|
| keys tried | 29, including every key this portal can write |
| rejected as not-a-preset-key | **`mmap`** and nothing else — `option 'mmap' not recognized in preset '*'` |
| accepted | threads, lazy-mode, ctx-size, parallel, flash-attn, n-gpu-layers, fit, fit-target, keep, cache-ram, ctx-checkpoints, checkpoint-min-step, cache-idle-slots, jinja, reasoning-budget, reasoning-effort, stop-timeout, dedup-cache-models, load-on-startup, models-max, models-autoload, api-key, no-webui, metrics, override-kv, mmproj, model, alias |

`mmap` is why the router died once: one unrecognised key in `[*]` makes the router reject the **whole
file**, silently at load time. "Accepted" here means the file parses; whether the value then does
what the operator meant is a separate question — `models-autoload` was accepted and its effect was not
separately confirmed.

**Startup loading is not autoload.** `models_autoload = true` means models load *on demand*, not at
startup. Three runs of the same router:

| preset | startup loads |
|---|---|
| `[*]` sets nothing | 0 |
| `[model] load-on-startup = off` | 0 |
| `[model] load-on-startup = on` | 1 — `load_startup: (startup) loading model Qwen3.5-0.8B.Q4_K_M` |

So a personality **can** decide what is resident before the first request from `presets.ini` alone:
no unit edit, no command-line change, no keeping two files in step. Consequences that were implemented:

- `load-on-startup = off` is the unset state, so it is suppressed as a no-op like every other
  restatement;
- `load-on-startup` in `[*]` is refused, and the refusal is general — **any** setting registered
  `section: "model"` is rejected in `[*]`. Measured why: `[*] load-on-startup = on` started loading a
  local 0.8B model **and** `unsloth/Qwen3.8-27B-GGUF:Q8_K_XL` discovered through the shared HF cache,
  a model the preset never named, and `models-max = 1` in the same file did not stop either load. `[*]`
  applies to what the router *discovers*, not to what the personality *lists*. The registry has always
  described these keys as model-scoped; nothing enforced it until now.
- the same rule closes the `ctx-size` version of the mistake — one line in `[*]` capping every model in
  the library, which is exactly what §4.10.1 forbids.

Probes: `tools/preset_eligibility_probe.py` and `tools/load_on_startup_probe.py`. Both point
`--models-dir` at a probe directory holding one small model rather than the real library: with autoload
left at its default, `--models-dir` *is* the blast radius, and the F run of the eligibility probe is
what a real library would have autoloaded — beside a router already holding 54.5 GiB.

#### 4.10.3 Every setting states what it does sideways (2026-09-24)

A settings page that names a knob without naming its consequence lets the operator read the name as the
effect. `fit-target` sounds like a safety margin; it is "shrink the context sooner, and reserve aperture
while the prompt cache and checkpoints live in host RAM". `n-gpu-layers` sounds like placement; it is
"turn reclaimable weight pages into anonymous, unreclaimable RAM". `ctx-size` sounds like a cap you can
lift later; nothing lifts it. `load-on-startup` sounds like it obeys `models-max`; it does not.

So every entry in the registry carries `side_effects` next to `help`, rendered under the description in
the table and inline in the editor. A test demands it of every setting except an exemption list that
must state its reason — `threads`, which sizes a CPU pool and from which nothing in a plan derives —
and that list fails if it keeps an entry which no longer needs exempting.

The same commit found `ctx-checkpoints` defined **three times** in `SETTINGS`. Python keeps the last,
and the last was the tersest: "Costs memory per slot", which had quietly replaced a paragraph carrying
the measured per-checkpoint sizes and the host-RAM-not-aperture fact. Nothing broke, because a dict
with a duplicate key is still a valid dict — so the registry literals are now parsed and duplicates are
refused.

#### 4.10.4 A model config: one file, a name of the operator's choosing, settings per personality (2026-09-25)

A model file is not one thing. The same `Alpha-7B.Q4_K_M.gguf` may want 2048 tokens and two slots for
work and the whole trained context for a long document, and the operator may want to call it something
other than the name its author gave the file. Before this, both were impossible without editing the
same personality that `safe` and `default` are built from — and those two are supposed to be fixed by
construction.

A **model config** is `(name, base_model, settings, note)`: a stored set of registry-legal keys for one
library file, addressed by a name the operator chose. A personality's model list can now contain
library ids (bare: that file, under its own name, llama.cpp's own defaults, no config at all) or config
names (that file, under the config's name, with the config's settings). `safe` and `default` are made
entirely of bare entries, which is what keeps them unbreakable by editing a config.

**The router behaviour this is built on**, measured on this box (b10964) and still true of the deployed
b11146 — both refusal strings are present in `releases/llama.cpp-b11146/libllama-server-impl.so`, and
in `tools/server/server-models.cpp:616`, `:651`, with the `(aliases: …)` listing at `:789`:

- `[<directory>] alias = <name>` makes the model answer to `<name>` as well; the router logs
  `* <dir> (aliases: <name>)`. End-to-end through the portal's own generated file: a request for
  `tiny-chat` returned 200 with `model="tiny-chat"`.
- A name that collides is **not a warning**: `model name '…' conflicts with alias of model '…'` and the
  router **exits 1 having loaded nothing**. So every collision shape is refused at save time, where
  there is a form to answer to, rather than at the next restart: name already a library model, name
  already another config (aliases share one namespace across all models), name in `RESERVED`
  (`*`, `safe`, `default`), and the charset/length rule — the router splits aliases on commas and an
  ini value ends at a newline.
- Two configs on one model, for one personality, are refused rather than resolved: a preset has one
  section per model, so "which won" would be answered by dictionary order.
- A config does not change which model it names. Retargeting would keep the name and the settings while
  pointing at another file, so every personality assigning it would start meaning something else.
- A config in use cannot be deleted through the API ("assigned by work"); if it is removed under a
  personality by other means, `render()` raises ("no stored config") instead of quietly writing a
  smaller file than the operator believes they are editing.

**Layering is one function**, `_layered_settings()`: `[*]` is the floor (and after 1c4836c it can only
hold global keys, §4.10.2), the config sits on top of it, and a personality-level override sits on top
of the config. `render()` writes that result and `budget_for()` prices *the same* result.

**The defect the omission produced**, and the reason the sharing is not a refactor nicety: `budget_for()`
priced each model at the index's stored estimate, so a config setting `ctx-size = 1024` never moved
`worst_case_bytes`. A card could read "worst case fits" above a file that would have asked for the
model's trained context instead — the exact shape of the OOMs §6.12 exists to prevent. Mutation-checked:
replacing `peak_bytes(row, ctx)` with `peak_bytes(row)` fails exactly
`test_config_settings_are_what_reprice_the_budget` and no other test. The host-cache side (`parallel`,
`ctx-checkpoints`, `cache-ram`) reads the same layered dict, so the two ceilings now answer about one
file rather than two.

**Which personality is running is derived, and now visible.** Nothing stores it. `main.py` sets
`active = not changed` from `render()`, and the card carries a `pers-active` badge — so a personality is
active exactly when its own text is the file on disk, and a hand-edited `presets.ini` shows as *nothing
active* rather than as a personality the router is not running.

Not done: no UI for the override layer (deliberate — two layers writing one key is what this design
exists to avoid, and it survives only for hand-assembled payloads), and configs carry no per-config
draft/mmproj choice beyond what the registry already names.

#### 4.10.5 The suite deleted the model library (2026-09-25) — and what now refuses to

Recorded here because the plan is where this project keeps the things it learned the hard way.

A test module added on this date set `AI_PORTAL_PASSWORD` at import and **not** `AI_PORTAL_HOME`.
`app/core/paths` resolves the home **when it is imported**, not when a fixture runs, so
`paths.LLAMA_MODELS` was the live `~/ai/models/llama`, and that module's fixture — which clears the
library so `rescan()` sees only its own models — cleared 13 model directories and 274 GiB of GGUFs.
Nothing in the test looked wrong. Every other module survived this only because it happened to set the
variable at its own top: a convention nothing enforced. The data was not recoverable — ext4, no
snapshots, no trash (rmtree bypasses it), no root, `ptrace_scope=1` blocking the copy still mapped in a
live worker, and no undelete tooling installed.

Three things are in the repo now instead of discipline:

1. `tests/conftest.py` sets the suite home **at conftest import**, earlier than any test module can
   import `app.core.paths`, and uses a **fresh `mkdtemp` per run** — a fixed name carried `model_index`
   rows and snapshots across runs, which showed up as tests passing over state they never created.
2. A session fixture **aborts the run** if `paths` resolved the real home or anything outside the temp
   directory. Verified by pointing it at `$HOME`: pytest exits with the alarm and changes nothing.
3. `tests/sandbox.py` is the only sanctioned way for a test to clear a tree (`clear_library()`,
   `clear_snapshots()`), and it refuses any target not under the temp directory.

Standing rule for every script in this project, not only tests: **a destructive operation runs
report-only first** — resolved absolute targets, sizes, and the root it descends from — and the
executing form is opt-in. A dry run that prints the path the code will actually use is the difference
between "I meant the temp directory" and "the code was in the temp directory".

## 5. Libraries & stack (Python 3.14, uv-managed)

| Concern | Choice | Why |
| --- | --- | --- |
| Backend | FastAPI + uvicorn | async (long jobs, SSE log tail), typed, small |
| Validation | pydantic v2 | request/config models, preset-key validation |
| HF | `huggingface_hub` | download + cache prune APIs (already on system: 1.2.2 / 1.29.0) |
| GGUF metadata | ~~`gguf`~~ → stdlib `mmap` + `struct` in `app/core/modelmeta.py` | the plan assumed the package; the shipped reader parses headers itself, and `gguf` was removed 2026-09-24 for never being imported (§5.1) |
| DB | stdlib `sqlite3` | zero-dep, single file in `~/ai/state/` |
| HTTP probes | `httpx` | lunarg SDK check, `git ls-remote` alternative |
| Presets | `configparser` | the INI format the router reads |
| Frontend | **D1 — open** | see §8; recommendation below |
| Reuse | `tools/aiportal.py` → `app/core/paths.py`; gguf-indexer code → `app/models/` | don't rewrite proven logic |

### 5.1 The dependency list, audited against what actually runs (2026-09-24)

`gguf` had been in `dependencies` since the plan above was written. It was resolved on every
`uv lock` and installed on every deploy, and **nothing in the project imported it** —
`app/core/modelmeta.py` reads GGUF headers with `mmap` and `struct`. Its transitive payload was worse
than its own size: `numpy`, `requests`, `urllib3` and `charset-normalizer` were installed on the
machine serving the portal. Nothing ever failed, because an unused dependency never does.

| | |
|---|---|
| runtime (`dependencies`) | fastapi, uvicorn[standard], pydantic, jinja2, python-multipart, httpx, huggingface_hub |
| dev (`[dependency-groups].dev`) | pytest |
| deployed venv | runtime only — `app/deploy.py` runs `uv sync --frozen --no-dev` |
| repo venv | `uv sync`, dev group included by uv's default |
| `requirements.txt` | generated: `uv export --no-editable --no-dev --no-hashes --frozen -o requirements.txt` |

`requirements.txt` is a **derived** artifact and not a second source of truth: its header names the
exact command, and a test refuses it if the runtime set drifts or the dev group leaks into it. It
exists for tools that speak pip; `uv.lock` still decides versions.

`tests/test_dependencies.py` is what makes the audit durable — three claims, each mutation-checked:

1. **no declared runtime package may be unused**, except one sitting on an explicit list that has to
   explain itself: `python-multipart` is load-bearing for FastAPI's `Form(...)`/`File(...)` and no
   import line names it. The exemption list itself fails if it holds a package that *is* imported, so
   it cannot collect fossils.
2. **every third-party module `app/` imports must be declared** — the direction that catches a runtime
   need hiding in a test-only venv.
3. **`uv sync` installs the dev group by default**, so every uv invocation in the deploy path is
   asserted to carry `--no-dev`. Forget the flag and the harness lands in production, silently.

Re-resolving after removing `gguf` dropped five packages from the venv and left the gate at **568
passed, 1 skipped** — which is the measurement that nothing needed it.

## 6. Safety model

- Destructive ops (delete, cutover, release swap) = dry-run preview + explicit
  confirm + rollback artifact in `~/ai/state/rollback/`.
- Everything mutating is journaled in the `events` table (who/what/when/why).
- `nvme1n1` is not referenced by any code path.
- HF token lives in the standard HF location; portal never copies secrets into
  its own state.

### 6.6 UI regression protection (Phase L, 2026-09-13)

A portal whose whole front end is Jinja templates plus inline JS has no
compile step to notice that a page's wiring is gone: the browser skips a broken
`<script>` block and the buttons stay on the page, doing nothing. Text-level
tests cannot see this, so the suite drives the pages.

- `tests/ui/lib.mjs` — jsdom page driver: real page from a real HTTP server,
  scripts executed, `fetch` recorded (and mutating requests intercepted per
  `spec.json`, answered with a synthetic success so pages take their success
  paths), `confirm/alert` captured and answered on demand, element-id misses
  recorded, canvas and `document.evaluate` stubbed where jsdom differs from a
  browser, pollers stopped before the sweep so every effect belongs to the
  control that caused it.
- `tests/ui/run.mjs` — per-page invariants: no uncaught errors / console.error /
  endpoint 4xx-5xx / missing ids; expected requests happened; expected elements
  present and not stuck disabled; tab bijection; sweep of every visible control
  twice (confirmations refused, then accepted) with a dead-control check; a
  confirm-gate must be a `confirm()` **before** the request; and a final
  coverage pass that fails if any visible control was never driven.
- `tests/ui/spec.json` — the declarative half: pages, expected fetches,
  tab groups, fixtures that make the deploy/rollback buttons live so their
  requests can be asserted, `requireConfirmFor`.
- `tests/ui_sandbox.py` — the sandbox (temp `AI_PORTAL_HOME`, `tests/ui/bin`
  shims, dead outbound proxy) and a `__main__` for driving one page by hand.
- `tests/ui/mutation_check.py` — injects twenty-three realistic regressions (renamed
  id, deleted function, unparseable block, renamed endpoint, removed panel,
  missing confirmation ×2, wrong request field, emptied handler, six tab geometry
  breakages §6.7, the two build-lockout breakages §6.8, and six pulse breakages
  §6.9 — frozen lane states, an empty board, a stalled readout, parser rot on a
  log spelling, and lost truncation/attention text) and requires every one
  to fail the suite with a message naming the right thing. This is what keeps the
  harness honest: it has caught itself four times — two of the first nine missed,
  then a settings mutation and a dashboard one, each a hole in the tests rather
  than in the app. It edits real files temporarily (templates, stylesheet, and
  `app/` for an endpoint-level bug), refuses to start unless those files are
  clean, and restores after every mutation.
- **The harness checks its own shape.** Extracting the per-control rules out of
  the sweep into `judgeControl()` moved the confirm-gate and dead-control checks
  and silently left behind the four before them — uncaught errors,
  `console.error`, element-id misses, endpoint errors. Nothing failed: the
  surviving checks were loud enough that 16/17 mutations still got caught while a
  page passed with six 400s in it. `run.mjs` now asserts that `judgeControl`
  still contains every rule it is supposed to apply, and endpoint errors that no
  control claimed fail the page on their own. An assertion that vanished with a
  refactor is the failure mode no app-side mutation can see, so the harness
  watches itself.
- `scripts/check-ui.sh` — the single entry point (`--ui-only`, `--mutations`),
  wired into the pre-commit hook installed by `scripts/install-hooks.sh`.

Three real bugs found, all live before this change:

1. **Four of the five dashboard update tabs were dead.** An apostrophe in
   `CLI's` inside a single-quoted `confirm()` string ended the literal, so the
   `<script>` block defining `switchUpdateTab`, `loadComfyUpd`, `loadHfUpd`,
   `loadGpuInfo` and friends never ran: tabs did not switch, no data loaded,
   every button in them threw `ReferenceError`.
2. **llama.cpp build progress rendered into the ComfyUI tab**: `updDeploy()`
   parked the job in `COMFY_UPD` instead of `UPD`, so the tab that started a
   10–20 minute build showed nothing.
3. **A button labelled "check for updates" started an upgrade.** The
   Python/venv tab POSTed `/api/updates/hf`, which upgrades huggingface-cli in a
   shared venv, with no confirmation. It now re-probes and reports; the upgrade
   stays on its own tab, behind a confirm.
Also hardened: tabs receive the clicked element instead of reading the global
`event` (an icon inside a tab would silently break the active state),
`selectModel()` finds its tab by id rather than by index, and a stray brace in a
`settings.html` style attribute.

### 6.7 Tab and panel geometry (2026-09-13)

Reported: both tab groups drew their bottom frame edge 10–20px above the panel
they control, so the strip looked like it was floating off the card. Cause was
`margin-bottom:16px` on the strip, plus the active tab being painted in the page
colour rather than the panel colour, so nothing visually joined the two.

Tab geometry now lives once in `web/static/tabs.css` (the two page-local copies
had already drifted), loaded from `base.html`, and states four rules:

1. the strip has no bottom margin and draws the full-width baseline;
2. every tab draws its own bottom edge and overlaps the baseline with
   `margin-bottom:-1px`, so its left and right rails land on the panel's top
   edge — inactive tabs draw that edge in `var(--line)`, keeping the line whole;
3. the active tab paints itself and that bottom edge in `var(--panel)`, opening
   the baseline exactly where tab meets panel, so they read as one surface;
4. the panel card sits flush on the baseline (`margin-top:0`), carries no top
   border (the junction would draw twice) and has square top corners.

Two traps worth writing down, both of which look right in a stylesheet and wrong
on screen: pulling the *card* up with a negative margin instead of the tab — the
card's background is opaque, so it paints over the baseline and erases it under
the inactive tabs too — and giving the card a top border as well as the strip's.
The checks reject both.

Pinned at two speeds: `test_ui_contract.py` reads the served CSS (milliseconds,
no node), and `run.mjs` measures it — `getComputedStyle` for the margins, and a
hand-written cascade walk (`el.matches()` over every parsed rule, falling back to
shorthands) for what jsdom will not expand. That cascade walk is a test-found fix
in its own right: jsdom reports an empty `border-top-left-radius` for a card whose
rounding came from `border-radius:10px`, which is how a real round corner sat
there unnoticed while the first version of the check "passed". Six of the fifteen
mutations are these geometries, one per way each rule can silently regress.

### 6.8 Stored builds, and what does not belong on a hero card (2026-09-13)

Two dashboard complaints, one about noise and one about a control that could not
survive its own success:

- A Vulkan SDK path and a *host metrics* link sat stranded under the ComfyUI
  hero card, outside its border. The nav already links Metrics, and the SDK path
  is inventory. The line now carries only the reboot badge, and only when a
  reboot is pending; the SDK inventory (Vulkan, ROCm, HIP) lives in the GPU
  Drivers / SDKs tab, where the Vulkan explanation already was. The same SDK path
  was also a row in the llama.cpp panel — it appears once now.
  `test_sdk_inventory_lives_in_the_gpu_tab` asserts it (against the *template*:
  a running ComfyUI log line can legitimately contain the word "vulkan").
- The llama.cpp panel listed every build as its own row with its own rollback
  button — one more row per build, forever, each doing exactly one thing: be
  picked — while a dropdown next to it already did that job.

Builds are one dropdown now: newest first, each option labelled
`name · commit · built · size` (or `current`), a line saying how many are stored
and what they weigh, and two actions — **Switch build** and **Delete build**.

Both actions are locked while the running build is selected: switching to it is a
no-op, and deleting it would pull the binaries out from under a live router.
A locked button says why in the hint line instead of just refusing, and the
lockout is asserted, not assumed (`spec.json` → `selectCases`: select the current
build → both locked; select any other → both usable, and *clicked there*, against
a known selection, because the generic sweep only reaches such a button in
whatever state the `<select>` happened to be left in).

Deleting is the one irreversible thing the portal does to disk, so the
destructive-request rules stack (§6):

1. the confirm dialog shows the build's facts — build, commit, built date, size,
   location — and says it cannot be undone and that rebuilding costs 10–20 min;
2. the endpoint refuses without `confirm: true`;
3. `delete_release()` then refuses, in order: an update job running; a name not
   in the registry (the registry is the only source of paths, so a caller cannot
   aim `rmtree`); the current build; a path outside `~/ai/releases`;
   and — because the farm, not the registry, is what is really executing — any
   release `~/ai/bin` still points *into* (containment, not equality: links point
   at `…/llama.cpp-b1/llama-server`);
4. the registry is copied to `~/ai/state/rollback/llamacpp-delete-<stamp>/` with
   a note naming what was deleted, and the audit log records bytes freed and
   builds remaining.

Harness changes this needed: `expectDisabled` (locked on load), `selectCases`
(select → assert enabled/locked), `then_click` (drive the control a selection
unlocked, judged by the same rules as everything else), and `judgeControl()` —
the rule set that used to live inline in the sweep, so a click is judged the same
however it was caused. It paid for itself immediately: the first version of the
two new buttons shipped **without `onclick` attributes**, and the dead-control
rule reported them instead of a green page.

### 6.9 The router pulse: reading a log the way a person needs it (2026-09-13)

The Status page's router card used to be a scrolling wall of lines that differ
from each other only in numbers:

```
[55601] 905.38.610.130 I slot print_timing: id  1 | task 282178 | n_gen =  512, tg =   5.97 t/s, tg_3s =   6.02 t/s
[55601] 905.41.614.660 I slot print_timing: id  1 | task 282178 | n_gen =  530, tg =   5.97 t/s, tg_3s =   5.99 t/s
```

A census of six hours of the live journal — 4,432 lines — said why that fails:
**95% of the volume is three line shapes** (generation ticks every ~3 s, prompt
read progress, and task summaries), and the lines that matter (a model being
swapped in, a truncated answer, a cancelled request, cache eviction) were 0.3%
of what was on screen. It also surfaced the fact that matters most on this box:
the router runs `--models-max 1` over 16 models, so **a request for a different
model stalls for a minute while weights load** — which looks exactly like a hang.

What replaced it, and the rules behind it:

1. **`app/core/rlog.py` parses, the browser draws.** One pure function turns the
   journal tail into slots / throughput bins / model state / attention. It has
   no I/O and no clock dependency, so the same log always yields the same
   picture — which is why the chart shows history the moment the page opens
   instead of after it has been sitting there.
2. **llama.cpp's own timestamp is the monotonic clock, the journal is the wall
   clock.** `common/log.cpp` stamps each line as `min.sec.ms.us` since process
   start; `systemd.journal_tail_timed()` asks journalctl (`-o short-iso`) for a
   real time per entry and hands the parser `(epoch, message)` pairs. Without
   the epoch, "that slot went quiet 4 minutes ago" is uncomputable, because the
   newest line of an idle router is *always* "just now" relative to itself.
3. **Progress is only shown when llama.cpp states it.** A prompt read has a real
   `progress = 0.42`; generation has no known target, so a running slot gets a
   sweep, a token count and a speed — never a made-up percentage bar. (`/slots`
   was tried: this build exposes `is_processing` and prompt counts but no
   generated-token count, and its `n_predict` is the leftover of the last task.)
4. **A model switch is the one thing shouted about.** `model name=X is not
   loaded, loading...` / `waiting until model name=X is fully loaded...` /
   `evicting idle LRU name=X` set `models.loading`, and it clears on the first
   *slot activity* — deliberately **not** on `proxying request to model X`, since
   that line is what *triggers* the load and clearing on it would hide the switch
   exactly while the user is waiting for it.
5. **A busy slot that stops talking is called out.** A running slot prints every
   ~3 s; silence for >15 s while still marked busy means it is waiting on the
   client, so the lane says `quiet 3m` with a hollow bar instead of a bar frozen
   mid-flight.
6. **Repeated notices fold into one line with a count** (`prompt cache pressure
   — evicting the oldest cached context ×23`). Counters (`errors`, `warns`,
   requests) are scoped to the same window the chart is drawn over, so a number
   and the picture next to it always describe the same span of time.
7. **The browser is the renderer; the template's lane loop is first paint.** The
   jsdom harness cannot see the Jinja loop being removed (the script redraws the
   board from the fetched pulse a moment later), which is why the mutation for a
   missing board breaks the container node instead — that is the thing whose
   absence actually kills the pane.
8. **Service state and controls are untouchable.** The card head — state pill,
   unit name, bind mode, Web UI/API links, start/stop/restart — is unchanged and
   asserted by `test_hero.py::test_service_state_and_controls_survive_the_redesign`.
   The log pane shrank (240px → 112px) and shows attention only; `all lines`
   switches it back to the raw tail for debugging.

The throughput chart is the one part jsdom cannot see (no canvas), so
`test_hero.py` pulls `drawPulse()` out of the template and runs it against a stub
2D context — asserting it draws the areas, labels the peak, and names a silent
tail (`last sample 35s ago`) instead of drawing it as a drop to zero. It failed
loudly when checked against the previous version of the function, so it is not
decoration.

Tests: 22 parser tests over the real captured journal (`tests/fixtures/
router_journal.log`) plus source-accurate rare lines; 10 page tests that render
the template from a canned snapshot; `expectText` in the jsdom harness (new) so
live regions must show *data*, not merely exist — the board must contain a
`generating` row with `120 tok · 6.0 t/s`, and the attention pane must contain
the cache-pressure line. `tests/ui/bin/journalctl` now emits a realistic router
journal in both `-o cat` and `-o short-iso`, so the hero renders real shapes in
a sandbox that cannot touch this machine.

### 6.10 What a read-only pane may cost (Phase F, 2026-09-14)

A health pane is free of risk but not of cost, and the two are easy to confuse
while the page still feels instant. Three things came out of measuring this one:

1. **A view must never be able to block for minutes.** The obvious way to fill
   the "deployed build" card was `update_status()` — which refreshes the upstream
   probe when its cache is stale, and that is a `git fetch` with a 300 s timeout.
   Opening a page should never be able to do that, so the provider grew
   `provenance()`: the same facts, read from the registry and the last saved
   probe, with no network path at all. The check button on the Status page is
   what pays for a fetch. `expectNoRequests` in the UI spec keeps it that way.
2. **Cost belongs to the page that needs it.** `attention_count()` is one number
   the dashboard could show, and getting it runs an apt simulation and a `du` of
   a 580 GB tree. Those live behind `/health` and its own TTLs instead; the
   dashboard's polling stays as it was until that polling is reworked (J).
3. **Cache failures too.** A 5 s network timeout that is also a cache miss means
   every page load on an offline machine pays it. A failed upstream check is held
   for an hour and shown as "unknown", which is the honest answer anyway.
4. **A lock needs proof of life, not a clock** (found live, 2026-09-15). Once the
   UI refuses work "because a job is running", the cost of *believing a dead job*
   is no longer cosmetic: the machine looks wedged. A reboot during a build left
   `status='running'` behind and the bar enforced it for the whole 6 h stale
   window — a job that could not exist, on a machine rebooted an hour ago. So a
   `jobs` row carries `pid` and a boot token, and is reclaimed the moment its
   writer is provably gone (earlier boot, or no process behind the pid). Age is
   only a backstop: it can answer "this row is old", never "this job is dead".
   Related trap: making a probe "degrade alone" (`except Exception: return None`)
   also hides the probe's own bugs — a missing `import Path` disabled boot
   detection silently while every boot-based test still passed via another
   branch. A probe that cannot fail loudly must at least be observed working.

#### 6.10.1 — a draft head is worth 2.2x; a fast model with no tool protocol is not an agent (2026-10-02)

*(numbered 6.12.13 when it was written, which collided with §6.12.13 below; the content is unchanged and belongs where it sits — a note about what the pulse pane may be asked to measure.)*

Four operator-driven runs, reports under `/home/cortana/ai/logs/slot-bench-20261002T21*.json`.

| file | role / ctx | prefill | decode | aperture peak |
|---|---|---|---|---|
| unsloth/Qwen3.8-27B Q8_K_XL, 29.30 GiB dense | coder 8192 | 306 / 360 | **7.2** | 31.39 GiB (RSS 3.80) |
| same, `--spec mtp` with the sidecar MTP file | | 292 / 349 | **16.2 / 16.9** | 34.89 |
| same, target's own in-file head | | 293 / 346 | **14.8** (accept 0.62) | 34.02 |
| same, fim role | fim 4096 | 311 | 7.3 → infill 6.4–7.1 | 31.14 |
| bartowski/DeepSeek-Coder-V2-Lite Q8_0, 15.56 GiB MoE 64/6 | coder 8192 | **1394 / 1333** | **43.4 / 26.6** | 19.04 (RSS 1.54) |

- **The head is the lever**: 7.2 → 14.8 tok/s free (in-file), → 16.2 with the
  1.28 GiB sidecar. Accept 0.62 vs 0.55; the speed gap is inside the noise of a
  tg64 sample. Either way the 27B dense stays out of interactive service without
  it, and Flash-Next (no `nextn` anywhere) cannot do this at all.
- **Fast ≠ agent**: the MoE reads 6x faster and `tool_call.emitted = false`,
  predicted by `template_markers = chat,system`. New badge in `capabilities()`
  (`tools` / `no tool protocol`), validated 4/4 against the bench probe.
- **A pooling declaration cancels the generative badges**: the Qwen3 reranker
  declares FIM specials and inherits Qwen tool tags but cannot generate. The
  models-page fixture had `pooling_type` on the *autocomplete* row — already
  conflating them; it has its own row now, FIM specials included so the refusal
  is what is tested.
- Estimator: no-draft pricing is accurate (31.61 priced / 31.39 peak), but a
  draft file's own KV + compute graph (~2 GiB) and the in-file head (~2.6 GiB)
  are unpriced in `peak_bytes()`. Open.
- Tool fixes: `--spec-draft` that resolves to nothing now refuses instead of
  silently measuring the target's own head (`resolve_draft()`); a target with
  its own head is no longer given a sidecar unless explicitly asked. `pp8192`
  needs `--ctx 16384` — at 8192 the probe self-skips (slot + 64 output).

### 6.11 Long actions, the venv question, and what a machine-specific Python stack

means for `pip install -r requirements.txt` (2026-09-14)

The report was "the ComfyUI update did nothing — no progress, nothing updated".
It had in fact succeeded once before that (job #20: 113 commits, venv sync,
restart, healthy in 6 s). Two separate problems were masquerading as one:

1. **Progress existed and was invisible.** A job is a row in `jobs` plus a daemon
   thread, so it survives navigation — but its log panel lived only inside the
   Status page's ComfyUI tab. Navigate to Metrics and the machine is still
   working while the page says nothing. Fixed by a bar in `base.html` that polls
   `/api/jobs/active` (4 s running / 15 s idle, one SQL query, no provider
   probes): name of the job, target, elapsed, **current step**, a `view log`
   toggle that expands the tail without leaving the page, and a finished job held
   on screen for 60 s so "did it work?" is answerable after the fact. It also
   locks `[data-needs-machine]` controls while something runs — a courtesy; the
   409 in §4.4 is the guarantee, because a disabled button is not a lock.
2. **The updater was right by luck, and one day wrong.** It installed into
   `paths.COMFY_VENV`, an assumed path, while the service starts
   `$COMFY_PYTHON` from its own launch env. Today those agree; the day they
   don't, an update installs into a venv nothing runs and reports success.
   `service_python()` now reads the launch env and says *where* its answer came
   from, including when it has to fall back because the recorded interpreter is
   gone ("would be used, but does not exist" is a log line, not a surprise).

**The landmine.** ComfyUI's README documents exactly `pip install -r
requirements.txt`, and its requirements list `torch`, `torchvision`,
`torchaudio` **unpinned**. This box runs local ROCm builds —
`torch 2.12.0a0+rocm7.13.0a20260411`, `triton 3.7.0+git8108eb2c.rocm…` — which
exist on no index. Run the documented command with `--upgrade` and pip resolves
torch to a CUDA wheel: the update "succeeds", pip prints nothing alarming, and
the GPU is gone. So the portal follows the README *plus* three things the README
cannot know:

- a generated **constraint file** (`~/ai/state/portal/comfyui-constraints.txt`)
  pinning every installed package whose version carries a local segment (`+rocm`,
  `+git`) and the torch family, passed as `-c` on every install. The log names
  what is being held and why — an invisible hold reads as "it didn't upgrade";
- **`--upgrade` is opt-in**, a checkbox, because on this machine "upgrade all
  dependencies" is a different and more dangerous action than "update ComfyUI";
- **verification before the restart**: import torch in that venv, require
  `torch.version.hip` on a host whose ROCm is installed. A sync that produced a
  CUDA build fails the job *while the old service is still running*, and the
  error carries the `git reset --hard <prev>` that undoes the checkout half.

A held package that changed version anyway is a failure, not a footnote: the job
compares the pins before and after and refuses to restart on top of a replaced
torch. This is the general rule for a stack built for one machine — *follow
upstream, then prove nothing upstream-specific was undone.*

**The bar's own lock was broken by the time it was tested.** `dataset.jobBusy`
writes the attribute `data-job-busy`; every rule acting on the lock spells
`data-jobbusy`. Nothing matched, so the bar painted correctly and locked nothing
beyond `disabled` — which the next panel re-render clears. Two lessons:

- `dataset.*` is not a JS property bag, it is attribute-name surgery. Anything a
  stylesheet selects by must be written with `setAttribute`.
- **A mutation test that isn't caught and a check that is broken look identical
  until something prints the state.** The mutated and clean runs produced the
  same report. `run.mjs` now tags failures with the phase that produced them and,
  for a body-attribute assertion, logs the value and the last `/api/jobs/active`
  poll. Related: the mutation tool restores files from a `finally` now — an earlier
  version of it left an injected bug in the tree whenever the sandbox died (it
  must be run under `uv run python`, not `python`, or the sandbox has no uvicorn),
  and every run after that reported "refusing to run on a dirty tree".

**What the tests had to get right** (`tests/test_comfyui_update.py`, 17 tests; a
scripted `subprocess.run`, no network, no systemd): the dirty-repo path used to
raise `NameError` from a typo (`diry`), which is exactly what "the update did
nothing" looks like from the outside — now a refusal that lists the files. Two
less obvious rules the harness enforces: dispatch fakes on **command structure**,
not on a substring of the joined command line (the temp dir contains the test's
own name, and `test_pip_installs…` once turned every command into a pip
install), and assert what a job *did* to the machine ("the service was not
restarted", "the pull never happened") rather than only what it printed.

### 6.12 What the router actually does with an idle model (measured, b10964 / v0.4.1)

Written down because the planning rules in §4.7 depend on it and because "it
probably unloads idle models" is how an OOM gets scheduled. All of this was
measured on this machine on 2026-09-15 with the deployed build
(`~/ai/releases/llama.cpp-b10964`), not read off a changelog.

#### 6.12.0 First: what "memory" means on this box, and the driver fix it rests on

There is a precondition under every number below, and it was not written down
until the owner pointed at it. It changes which figures are even about the same
thing.

**There is no VRAM.** This is an iGPU (Strix Halo, `gfx1151`): `rocm-smi` reports
`VRAM Total Memory (B): 536870912` — **512 MiB**, a stub. The memory models live
in is the **GTT aperture**, and that aperture is the whole GPU's view of system
RAM.

**The aperture was capped at 64 GiB, and that was too small to run the flagship
model.** `amdgpu` defaults to a 64 GiB GTT limit — under the *kernel* command
line / the deprecated `gttsize` module parameter. The 88 GiB model this box serves
crosses it, and the symptom is not a clean failure: **the model loads, and then
reports only a 4096-token context.** A 4096 ceiling on a model with a 262144-token
trained context looks like a model defect, not a driver limit, which is what made
it expensive to find.

The fix the owner worked out was to **move the setting off the kernel command line
and onto the AMD TTM module itself**, into a dedicated file:

```console
/etc/modprobe.d/amdttm.conf
  options ttm    pages_limit=25600000                            #  97.7 GiB
  options amdttm pages_limit=28311552 page_pool_size=28311552    # 108.0 GiB
```

`amdttm` (AMD's out-of-tree copy of TTM) is the one that binds: the driver reports
`mem_info_gtt_total` = **108.0 GiB**, matching `28311552 × 4 KiB`. The value sits
in the **100–110 GiB band the owner identifies as the highest stable on this OS**
— above it the box is not reliable, so this is not a knob to push further.

**Why the fix was hard to find — and what actually causes the clamp.** The owner
hit this over a week before it was solved, working Unsloth Desktop, a different
Qwen model, and their own research. The mechanism they landed on: it appears
**when there is enough RAM for the weights but not enough for the caches to run
the model** — precisely a KV/score-cache allocation failing inside a loaded model,
which is the phase `n_ctx_slot` is printed *before*. Community reports of the same
clamp came from people on other models and smaller machines, with the same hint
that it was an issue *during model initialisation under limited RAM*. That is
corroborating rather than proven here, and it predicts something testable (§6.12.3).

The aperture limit is the lever that *resolved* it: at the 64 GiB default a large
model could still map its weights while leaving too little of the GPU's view for
the caches. **The clamp is the useful signal, not the finding** — a model that
loads and reports a context far below its trained length is a low-memory failure
mode masquerading as a model defect. That is worth detecting, and it is a shape
bigger than this one driver: any model, any machine, weights fitting and caches
not. The portal should look for "context ≪ trained context", not for 64 GiB.

The owner has seen it only on Qwen3.8-Flash-Next, and 64 GiB is **not** claimed to
be a magic threshold: the same clamp should appear at any aperture where weights
fit and caches do not.

Consequences that follow from this and nothing else:

- **The budget unit is GTT, not `MemAvailable` and not `rocm-smi` VRAM.** They
  disagree by ~200×: right now `MemAvailable` reads ~20 GB, `rocm-smi` says 512
  MiB, and GTT is **71 / 108 GiB**. §4.7's budget must be planned against the
  aperture, and the portal now reads it (`app/metrics.py::_gtt()`, reported as
  `pool_total` / `pool_used` with `is_igpu`).
- **`free`/`MemAvailable` is the wrong gauge here**, which is also why the memory
  guardian in §6.12.2 had to learn to watch swap.
- **The fix is machine configuration, not repo state.** It lives in
  `/etc/modprobe.d/amdttm.conf`, is not in git, and a kernel/driver update or a
  lost modprobe file silently returns the box to 64 GiB — where the flagship model
  loads and quietly reports 4096. `health.aperture_advisory()` now checks it: `bad`
  at the 64 GiB default, `warn` below the expected 108 GiB, and `info` when the
  aperture was probed but could not be read — three states deliberately not
  collapsed, because "unknown" and "empty" are different answers.

Measured 2026-09-16: `gtt_total` 108.00 GiB, `gtt_used` 71.07 GiB, `vram_total`
0.50 GiB, `pages_limit` 25600000 (ttm) / 28311552 (amdttm).

**There is no idle TTL.** Two eviction paths exist and both are demand-driven:

- `unload_lru()` runs **inside the load path** — a loaded model is ejected when
  *another* model needs the slot (`tools/server/server-models.cpp:1114`);
- the scheduler's `tick()` evicts idle models **only while the request queue is
  non-empty** and queued requests outnumber free/freeing slots (`:348-383`).

An idle model with an empty queue is never shed, however much the desktop wants
its memory.

**`--sleep-idle-seconds` is not an ejection.** Throwaway worker, Qwen3.5-0.8B,
`--sleep-idle-seconds 4`, RSS sampled from `/proc`:

```
startup 847 MB → after a completion 851 MB → idle +4s 797 MB → idle +8s 636 MB
wake on next request: OK, 870 MB          log: entering / exiting sleeping state
```

It works, it wakes correctly, and it freed ~25% of RSS. `handle_sleeping_state()`
calls `destroy()` then `load_model()` — the *context* goes (KV cache, compute
buffers); the weights stay, and the first request after idle pays the reload.

**Loaded weights here are anonymous memory.** Live worker serving the 94 GB MoE
under `--fit on --fit-target 8192`:

```
VmRSS 32.7 GB   RssAnon 31.2 GB   RssFile 0.3 GB   RssShmem 1.2 GB   VmLck 0
MemAvailable 13.5 GB (of 122 GB)   Committed_AS 53.5 GB
```

0.3 GB file-backed out of 32.7: the weights are not reclaimable page cache. The
OS cannot take them back; only an unload can. So `models_max` and the §4.7 budget
are the actual protection, and `--fit` is a **load-time** guard, not a
steady-state one.

**What the portal can add, and why it should:** the router exposes
`POST /models/unload` (and `GET /models/sse` for state). A portal-side TTL —
watch idleness per loaded model, unload past the personality's threshold, show it
in the job bar — is the missing primitive, implemented where the policy lives
instead of in a C++ default.

**Measured (see §6.12.2 for how, and for the tool bugs that had to be fixed
before any of this could be trusted).** Three of the four answers change what the
portal has to do, so they are stated as conclusions rather than open questions:

1. **LRU eviction works.** At `models_max = 2` with three models in demand,
   llama.cpp logs `evicting idle LRU … for a queued request` and cycles correctly;
   a cold call cost 3.0 s against 1.5 s warm on small models. So *evicting* is not
   the missing piece — deciding *when* is.
2. **`fit` is blind to other residents.** 18 GB held by another process changed
   the chosen context not at all (`n_ctx_slot` 16384 either way), with no refusal
   and no allocation error. It also does not cap a model's own cost: an 87.2 GB
   model occupied ~98 GB under `--fit-target 8192`. **The §4.7 budget is therefore
   mandatory, not advisory** — nothing downstream refuses an over-committed set.
   See `tools/fit_probe.py`.
   *Superseded in part on 2026-09-23 by §6.12.7: with the residents being llama.cpp
   processes rather than a host allocation, `fit` measured free device memory correctly
   and reduced the context. It is blind to host residents, not to device ones. The
   conclusion this item exists to support — that the portal's budget is mandatory —
   stands, and §6.12.7 explains why.*
3. **Unload during generation is destructive.** `POST /models/unload` returns 200
   `{"success":true}` and the in-flight generation dies (`http 500 … Failed to read
   connection`). A TTL built directly on unload will cut users off mid-sentence;
   the portal must check for in-flight requests first, and the router gives no
   signal that it did so — the success is a lie by omission.
4. **Idle models never expire.** RSS flat across 600 s with an empty queue,
   `still_resident: true`. Confirms the source reading: `unload_lru` runs inside
   the load path, and `tick()` only evicts while the queue is non-empty. The TTL
   really does have to live in the portal.

Still open: reload latency per model (the cost side of every TTL — what "you
haven't used Qwen in 20 minutes" actually charges the user), and whether the
portal's unload can be sequenced to avoid killing a live request.

#### 6.12.1 `tools/ttl_oom_lab.py`: the harness for those four

Those experiments load models that together exceed the box, which means they have
to run while the production router is stopped — and the router hosts whatever is
driving the experiments. So this is a standalone, stdlib-only script that runs
alone and reports to disk, not something an agent does with tools.

It exists because of three hard facts about being the model host:

- **The script must not be the OOM victim.** Every `llama-server` it starts runs
  in its **own session** with `oom_score_adj=900`, so when the kernel needs
  someone to kill, it is the model process — not the desktop, not the browser, not
  the script holding the results. (Lowering the parent's own score needs
  privilege; it tries and honestly reports that it could not.)
- **The router must come back.** Cleanup runs on success, on error, on Ctrl-C, on
  signal, and at exit; and *before* starting, the script looks for a previous run
  that left a `state.json` behind — a run killed with SIGKILL must not leave the
  box without a router.
- **A run that dies must still have succeeded.** The parent stays tiny by
  construction (children write to files, never pipes; memory is read from
  `/proc`; results are appended and flushed as they happen), so the evidence
  survives its own crash.

A **memory guardian** samples every second and, unless `--allow-oom` is passed,
kills the children below `--floor-gb` of `MemAvailable`. That is the point: what
we want to learn is what llama.cpp does when it cannot allocate, not whether this
desktop survives a kernel OOM — and an attempt that is allowed to proceed until
memory actually runs short is the only version of the experiment that answers the
question honestly.

| | |
| --- | --- |
| `--list-models` | what it can see: shards collapsed to whole size, `mmproj-*`/`imatrix-*`/draft files dropped, dangling symlinks *reported* (two of them, which is what crashed the old indexer) |
| `--dry-run` | the plan and the memory figure, writes nothing to the results tree (goes to `/tmp`) |
| `--mini` | ~1 GB, **router untouched**: proves spawn-separation, `oom_score_adj`, sampling, event extraction, cleanup, reporting |
| `--full --yes` | stops the router and runs E0–E4 |
| `--background` | re-exec via `systemd-run --user` and return, so the run leaves the agent's process tree and survives the agent. A new session alone is **not** enough here — see §6.12.2 |
| `--restore-router-only` | emergency "put the router back" |

E0 idle model never expires · E1 does `fit` see other residents · E2 LRU thrash
at `models_max=2` · E3 `POST /models/unload` during generation · E4 does unload
return the memory, and what does a reload cost.

**Validated (`--mini`, this box):** server up, `/completion` 200 in 0.04 s, RSS
0.62 GB, `own_session: True`, `oom_score_adj=900`, three llama.cpp events
extracted, clean SIGTERM, `summary.md` + `results.json` + 1 Hz `samples.csv` all
written, router never touched. One bug class caught by running it rather than
reading it: the script joins its argv string into characters when writing the
summary — the reason the harness gets run before the experiment does.

#### 6.12.2 What the runs measured, and the four things that were wrong first

Five `--full` runs. Each got further than the last because each exposed a deeper
cause; recording the causes matters more than the numbers, because they are the
reason the first numbers must not be trusted.

1. **`POST /models/load` is an ack, not a load.** It answers `200 {"success":true}`
   immediately and loads in the background. Timing the ack recorded load times of
   `0.0 s` and sampled RSS before the weights existed, and — because nothing said
   *still loading* — the script fired three 87 GB loads back to back. The guardian
   caught the pile-up; the measurement did not survive. Fixed by reading
   `GET /models` and waiting for `loaded`.
2. **A new session does not escape an agent harness.** `start_new_session` defeats
   `killpg`, but the harness sweeps its descendants by *tree*, so a detached run
   died anyway. `systemd-run --user` places the unit under `user@.service`, outside
   the tree; verified by watching a unit finish after its launcher was gone.
3. **The OOM killer is not the interesting failure — swap is.** The samples show
   why a `--floor-gb` of 6 never had a chance: free memory fell **12.95 → 7.85 GB
   in a single 1 Hz sample** while the child's RSS stayed at 0.13 GB. The pages
   were not becoming resident, they were going to swap (journal: *6.3 G swap peak*),
   and a machine swapping a model does not fail cleanly — it thrashes until the
   kernel intervenes, and systemd then SIGTERMs the whole unit, result and all. So
   the guardian trips on **swap used** (`--swap-floor-gb`, default 1), not on RAM.
   Watching `MemAvailable` alone is watching the wrong gauge.
4. **`--fit-target` does not bound what a model costs.** From two independent runs,
   identically: loading `Qwen3.8-Flash-Next-GGUF` (**87.2 GB** on disk) under
   `--fit on --fit-target 8192` took **55 s** and drove free memory **116 → 18 GB**
   — a **~98 GB footprint, 12 GB more than the file**. `fit-target` sizes the
   context against the machine; it is not a memory cap.

**E0/E2/E3 answered correctly despite the broken load timing**, because they never
depended on it: E0 — RSS flat across 600 s idle, `still_resident: true`, *no idle
expiry*; E2 — llama.cpp's own `evicting idle LRU … for a queued request` cycling
through three models at `models_max=2`, so eviction works and `complete()`'s
cold/warm timings (3.0 s → 1.5 s) are real; E3 — **the one that matters for any
TTL**: `POST /models/unload` during an in-flight generation returns **200
`{"success":true}`** and the generation dies, `http 500 … Failed to read
connection` after 14 s. The router will happily cut off a user mid-sentence. A
portal TTL must therefore check for in-flight requests before unloading, and
never treat unload as safe.

**E1 answered properly, and cheaply — by giving up on the big-model approach.**
`fit` is a *startup* decision, so it does not need 150 GB of models to
demonstrate: hold R GB of anonymous memory in a helper (the same class of page a
GGUF weight is, per §6.12), start an identical worker with and without it, compare
the context it settles on. `tools/fit_probe.py` does this in two minutes with the
router up and the agent alive.

| | free | `n_ctx_slot` | started? |
| --- | --- | --- | --- |
| nothing held | 21.9 GB | **16384** | yes, generates |
| 18 GB held | **3.8 GB** | **16384** | yes, generates |

**`fit` is blind to other residents.** Identical decision with 18 GB less memory,
no shrink, no refusal, no allocation error. Two models that each individually fit
can be loaded together, and the machine discovers the truth by swapping. This
settles the open question in §4.7: **the portal's memory budget is mandatory, not
advisory** — nothing downstream will refuse an over-committed personality, so the
portal is the only thing between a personality and a swap-stalled box.

The probe reported *inconclusive* on its first successful run: its context regex
matched bare `n_ctx`, which llama.cpp does not print (`n_ctx_slot` is the post-fit
number). And its readiness check had accepted **any** HTTP answer — 200, 400, 404,
503 — so a port that had merely *bound* counted as healthy and every run looked
fine. Both fixed; readiness is now "it produced a token". A probe that cannot tell
loaded from listening is worse than no probe, because it reports success.

```
python3 tools/ttl_oom_lab.py --background --full --yes
# → runs detached; the agent that launched it dies with the router
# → results: ~/ai/logs/ttl-oom/latest/{summary.md,results.json,samples.csv,events.log}
# → the router is restored at the end and verified on :8080; start the agent again
```

#### 6.12.3 Does the aperture come back? Measured — and the answer is yes

The owner's report was specific: after an OOM mid-task, no `llama-server` was visible in the
process list and total-used-vs-available still showed the memory gone, requiring a reboot before
the model would load again. That is the signature of driver-side retention, and it is the one
thing §6.12 had never tested. It now has.

Three unload/reload cycles of `Qwen3.8-Flash-Next-GSQ-RCO` through `POST /models/unload` and
`/models/load`, GTT read from driver sysfs after each:

```
cycle      baseline   after unload   after reload
  1         54.46G        0.40G        54.15G
  2         54.46G        0.40G        54.15G
  3         54.46G        0.40G        54.15G
unload returned 54.06 GiB; reload cost 53.75 GiB; net vs previous reload +0.00 GiB
```

0.40 GiB is the 512 MiB stub the iGPU always holds. `MemAvailable` went 63 → 118.5 GiB. **No
ratchet, three cycles running.**

Then the failure the owner actually described, reproduced: a generation started, then the worker
**SIGKILLed mid-task**, router parent left alive.

```
loaded, idle        GTT 54.42 GiB   avail  62.17 GiB   llamaRSS  8.84 GiB
mid-generation      GTT 54.42 GiB   avail  58.16 GiB   llamaRSS 12.52 GiB
after SIGKILL +15s  GTT  0.40 GiB   avail 118.38 GiB   llamaRSS  0.69 GiB
after SIGKILL +40s  GTT  0.40 GiB   avail 118.53 GiB   llamaRSS  0.69 GiB
```

Nothing is retained — not even across a hard kill during generation. Worth noting why this was
worth testing rather than assuming: the router **parent** holds `/dev/dri/renderD128` on three
fds and `/dev/kfd` on one, so an inherited open file description keeping the child's buffer
objects alive was a live hypothesis with the right shape. It is not what happens here.

**What the remaining memory behaviour therefore is:** the two mechanisms §6.12 already recorded —
idle models never expire, and idle *allocates* (prompt cache + checkpoints, upstream saw a 2.6 GiB
idle-slot save followed by an OOM kill five seconds later). Not a leak.

**What is still unexplained:** the "no process visible, memory still gone" observation. Two
remaining candidates, and the discriminator for both is to read `MemAvailable` rather than a GUI's
used bar: a worker orphaned when its router parent died (orphans are not zombies and are easy to
miss in a unit-grouped process list), or page-cache accounting — a loaded Flash-Next model leaves
~53 GiB in `Inactive(file)`, which several monitors render as "used" while `MemAvailable` stays
high.

**lazy-mode, measured against the managed router's own settings.** Same model, same build (b10964):

| configuration | GTT aperture used |
|---|---|
| managed unit, `LLAMA_ARGS=""` → no `--lazy-mode` | **80.5 GiB** |
| `--lazy-mode on` | **54.5 GiB** |

`on` reads tensor rows from disk on demand instead of keeping them resident, which keeps weights
in reclaimable page cache rather than pinned anonymous buffers. Every `llama-server` OOM kill in
the journal — 26 across five boots — carries `file-rss: 8 kB` with `anon-rss: 21–32 GiB`: never
page-cache backed, in the 27–35 B era and the Flash-Next era alike. That is why `GLOBAL_DEFAULTS`
now emits it.

**Confound stated so it is not read as a clean A/B:** kernel `7.0.0-31 → 7.0.0-34` was installed
09:25 and booted 09:30 the same morning `--lazy-mode on` started. The amdgpu DKMS build (7.1.3,
`7.1.3.31500000` in dmesg) is identical across all six boots and rebuilt for the new kernel, so
the driver is not the variable; the kernel is, and it was not isolated.

**Two config landmines found while doing this, both silent:**

- `--models-max` defaults to **4**. The managed unit forces 1; a hand-started router with
  `--models-dir` and no `--models-max` reports `max_instances: 4` with `models_autoload` on — four
  concurrent models on a 108 GiB aperture.
- `-c/--ctx-size` defaults to **0 = loaded from model**, so an unset context is 262144 for this
  model whether anyone meant it or not.

**Known gap in the portal's own estimate, unfixed.** `kv_bytes_at()` prices one slot and ignores
`--ctx-checkpoints` and prompt caching. The slot part is *not* a factor of four: `server.cpp`
resolves `--parallel -1` to **4 slots with `kv_unified = true`**, a shared pool rather than four
copies. The unpriced part is idle-time allocation, against `HEADROOM_BYTES` of 4 GiB when §6.12.2
measured a footprint ~12 GiB above file size. Fixing it means deciding whether the allowance
belongs in `peak_bytes` (which tests pin at exactly `HEADROOM_BYTES`) or in a separate resident
cost the budget checks and the models page both read.

#### 6.12.4 Is `--metrics` eating memory? No — read from the source, not inferred

Worth asking rather than assuming, because the two routers on this box really do differ: the
managed unit sets `LLAMA_METRICS=1` and passes `--metrics`, a hand-started `llama-server` does not,
and `--metrics` defaults to **disabled**. A per-request metrics store that never gets cleared would
have been a plausible source of the memory that seemed unattributed.

`tools/server/server-common.h:442` settles it. `server_metrics` is a fixed set of scalar counters —
two `bucket` structs (count / steps / time) and a handful of `uint64_t` totals. There is no
per-request record and no history; the only container in it is `std::vector<uint64_t>
n_accepted_per_pos`, one entry per speculative-decoding draft position, which exists only when
speculative decoding is on. Nothing accumulates with uptime.

So `--metrics` is not the difference between the managed router and a manual one. The measured
difference is `--lazy-mode` (80.5 vs 54.5 GiB, §6.12.3), and the direction that matters is the
opposite of the intuitive one: the managed unit is the *more* constrained of the two, because it
forces `--models-max 1` where a hand-started router takes the default 4.

### 6.12.5 Re-pricing a stored model: the branch that never ran (2026-09-23)

`peak_bytes()` has had a branch for `peak_bytes(row, some_other_context)` since it was written. On
this box it has never produced a number:

```
peak at est_ctx (no ctx argument) : 82802543168
peak at explicit 8192             : None        ← for every one of the 16 models here
```

The reason is two vocabularies for one fact. `model_index` stores `layers`, `kv_heads`,
`attention_heads`; the estimator reads `block_count`, `attention_kv_count`, `attention_count`. The
branch handed the row itself to `kv_bytes_at()`, which looked up `block_count`, found nothing, and
answered "cannot plan". Nothing was visibly wrong, because None is the honest-looking answer.

**`est_inputs` was the fix already sitting in the table** — it is each row's own record of what its
estimate used, in the estimator's vocabulary — except that it recorded only scalars. For gemma-4
and Laguna, whose estimates come from per-layer head arrays and sliding-window caps, that meant a
column claiming to hold "the inputs the estimate used" while omitting the inputs that decided the
answer, and no way to reproduce or check those two rows' numbers at all. `est_total` now records
`attention_kv_count_layers`, `attention_count_layers`, `sliding_window` and
`sliding_window_pattern` too; absent keys stay absent, so "this arch declares no per-layer shape"
does not become indistinguishable from "this row predates us storing it". After a rescan:

```
repricable at ctx 8192                    : 16/16
stored estimate reproduces from its inputs: yes for all 16
```

**Repricing is verified before it is trusted.** Pricing a row's own inputs back at the context they
were recorded at must reproduce `est_kv`. When it does not, the geometry was never stored, and the
answer is `None` rather than a scalar approximation — a plausible number that is not the right one
is the failure this index exists to prevent, and a row like that needs a rescan, not a guess.

**A second inconsistency in the same table:** `est_kv` is priced at the model's **trained** context
while `est_ctx` records the largest context that **fits**. `qwen3-emb-8b-Q8_0` stores an estimate
made at 40960 beside an `est_ctx` of 32768. `peak_bytes()` reused the stored cache whenever the
caller asked for `est_ctx`, reporting a trained-context cache as a reduced-context one — conservative,
but describing a cost that is not the cost of the question asked. `estimate_ctx()` now says where
the stored number was made, and it is reused only when it is that context's price.

**And the direction a fallback may be wrong in, corrected by measuring it.** The fallback added
earlier in this session scaled the stored cache linearly and was documented as erring "the safe
way" — overstating. That was wrong, and wrong in the unsafe direction:

| gemma-4-31B-it-QAT-Q4_0 (7.075 GiB cache at 262144) | exact | linear scaling |
|---|---|---|
| ctx 8192 | **0.718 GiB** | 0.221 GiB |
| ctx 32768 | **1.333 GiB** | 0.884 GiB |
| ctx 524288 | 13.638 GiB | 14.150 GiB |

Cache size is concave in ctx — window-capped layers hold `min(ctx, window)` — so the line from the
origin to the stored point lies **below** the truth beneath that point. Linear scaling understates by
3.2× at 8192. `_conservative_peak()` now clamps to `max(stored, scaled)`, so both directions err
toward refusing a context that would have fitted; the one error a loader cannot survive is pricing a
model below what it costs. A linearly-scaled Qwen row, by contrast, is exact at every context — the
error is specific to windowed archs, which is why it was invisible until measured.

All three fixes are mutation-checked: removing the clamp, removing the reproducibility guard, and
making repricing blind to `est_inputs` each fail exactly the test guarding it.

### 6.12.6 The second ceiling: what llama.cpp holds in host RAM (2026-09-23)

The estimator priced weights + KV + a 4 GiB headroom against a 108 GiB aperture, and §6.12.2 had
measured the real footprint running ~12 GiB above the file size. What that 12 GiB *was* had never
been identified. It is mostly two features that are on by default, priced at nothing, and not in
the aperture at all.

Measured on this box with the server's own accounting (`created context checkpoint ... size = N MiB`)rather than inferred from the source:

| what | value | how it was established |
|---|---|---|
| prompt cache limit | **8192 MiB** | start-up log: "prompt cache is enabled, size limit: 8192 MiB" |
| checkpoints per slot | **32** | start-up log: "context checkpoints enabled, max = 32, min spacing = 8192" |
| one checkpoint, Qwen3.5-0.8B | **19.266 MiB** | identical at contexts 16384, 32768, 65536 **and** 131072, and at n_tokens 4056 and 4568 |
| one checkpoint, Qwythos-9B | **50.251 MiB** | same run shape, different model |
| checkpoints on a dense model | **none** | with `--ctx-checkpoints 32` in effect, the server logged `main/do_checkpoint = no` |
| aperture cost | **zero** | GTT did not move at all between checkpoint-on and checkpoint-off runs; RSS rose by exactly the logged checkpoint total |

Four things follow from that, and two of them kill the obvious formulas:

- **Checkpoint size is not proportional to context, and not proportional to session length.** A
  constant 19.266 MiB across an eight-fold context range and across a growing conversation is a
  property of the model's cache layout. "Cost scales with context" and "cost scales with how long
  the session is" are the two things an estimator would try first, and both are wrong here.
- **Checkpoints are gated per model, not per setting** (`server-context.cpp`: SWA in use, or sequence
  removal that is full or bounded). That is why the flag is aliased `--swa-checkpoints`. Nine of the
  sixteen models on this box have the hybrid layout that gets them.
- **It is host RAM, not aperture.** `common_prompt_checkpoint` is `std::vector<uint8_t>`; the
  measurement agrees. Which matters because this box has 122.83 GiB of RAM and a 108.00 GiB GTT
  aperture carved out of it — so the caches and the models are competing for the same physical
  memory, and a check that consults only the aperture cannot see the competition at all. That is the
  shape of "it should fit but the kernel killed it".
- The prompt cache limit is a *cap*, so it is priceable; `cache-ram = -1` removes the cap and is
  therefore returned as `None` rather than as a big number.

`modelindex.host_cache_bytes()` prices both, and `personality.Budget` now carries a second verdict
(`fits_ram`, `ram_headroom_bytes`) alongside the aperture one. `apply()` refuses on `fits_ram` alone.
The two ceilings disagree on real numbers: **98 GiB of loaded models inside a 100 GiB aperture on a
110 GiB machine fits the aperture and misses RAM by 4 GiB.**

Checkpoints are priced as the model's KV for 4096 tokens — 28.0 MiB against the 19.266 MiB measured
on the 0.8B model (1.45x) and 128.0 MiB against 50.251 MiB on the 9B (2.55x). Conservative in both
cases, and stated as a token-count guess rather than a derivation: the mechanism that decides a
checkpoint's size is the KV cache's internal layout, and that was not derived here.

**This is not a check that refuses everything.** On the live library, the shipped `default`
personality costs 93.73 GiB against the 108.00 GiB aperture (14.27 spare) and 93.73 + **12.97 GiB**
of host caches against 122.83 GiB of RAM (16.13 spare). `presets.ini` is unchanged by any of this.

#### 6.12.7 — what `fit` really does, and what one load really costs (2026-09-23)

**A recorded conclusion was wrong, and it was this portal's own.** §6.12.2 item 2 says `fit` is
"blind to other residents", from a probe where 18 GB held elsewhere changed the chosen context not at
all. Re-asking the question with the residents being llama.cpp processes rather than a host
allocation, `fit` both sees and acts:

```
(router holding 54.5 GiB)  projected to use 38893 MiB of device memory vs. 54888 MiB of free device memory
                           will leave 15995 >= 1024 MiB of free device memory, no changes needed
(same model, +12 GiB hog)  projected to use 38893 MiB of device memory vs. 36972 MiB of free device memory
                           cannot meet free memory target of 1024 MiB, need to reduce device memory by 2944 MiB
                           context size reduced from 1048576 to 957184
```

Each "free" figure is the aperture minus what other processes hold, so it is not blind to *device*
residents. The earlier probe held its 18 GB where a device-memory gauge cannot see it — on an iGPU
host RAM and aperture are the same physical pool but not the same accounting. What the old
measurement got right is the part the portal depends on: `fit` adjusts only arguments left unset, per
load, at load time, and it does not bound a plan.

**Meeting its own target does not mean the load survives.** In the second run the server logged
`successfully fit params to free device memory`, then died:

```
ggml_backend_cuda_buffer_type_alloc_buffer: allocating 1022.77 MiB on device 0: cudaMalloc failed: out of memory
graph_reserve: failed to allocate compute buffers
llama_init_from_model: failed to initialize the context: failed to allocate compute pp buffers
srv    llama_server: exiting due to model loading error
```

`fit`'s projection omits the compute graph it reserves afterwards. That ~1 GiB arrives *after* fit has
declared success, which is the exact shape of "this model should fit and then OOMs at load".

**What a load costs in the aperture.** GTT delta measured around one load, minus weights and repriced
KV, at ctx 32768:

| model | weights | KV predicted | measured GTT delta | unexplained |
|---|---|---|---|---|
| Qwythos-9B-Q4_K_M | 5.24 GiB | 1.00 GiB | 6.17 GiB | −0.07 GiB |
| Qwen3.6-35B-A3B-Q4_K_M | 19.71 | 0.31 | 20.62 | +0.60 |
| ThinkingCap-27B-Q8_0 | 27.05 | 1.77 | 28.28 | −0.54 |

The same Qwythos model at its trained 1048576 measured 38.16 GiB against 37.24 predicted (+0.92), and
llama.cpp's own projection agrees with the portal's (38893 MiB). So **a load costs weights + KV + about
1 GiB**, and `HEADROOM_BYTES` (4 GiB) is a deliberate margin roughly four times the measured overhead,
not a measurement — it is where the graph reserve and `fit`'s optimism get paid for. Keeping it is a
choice; as of this section it is a documented one rather than an inherited number.

**Two things read from the server instead of inferred.** `n_parallel is set to auto, using n_parallel =
4 and kv_unified = true` confirms `personality.AUTO_PARALLEL_SLOTS = 4` for b10964. And the layer
assignment confirms the hybrid pricing: Qwythos-9B logs `llama_memory_recurrent` for exactly
{0,1,2,4,5,6,8,…} — 24 layers — with `llama_kv_cache` holding a device buffer only for layers
3, 7, 11, … i.e. **8 KV layers out of 32, which is `ceil(32 / full_attention_interval)`**, the number
`repriced_kv()` uses. (`n_swa = 0` in the same log is about sliding-window attention, a different
thing from this split, and is not a contradiction.)

Both halves are reproducible: `tools/fit_awareness_probe.py` runs the hog A/B and prints what `fit`
decides, and `tools/load_cost_probe.py` measures per-model aperture deltas against the portal's own
prediction. They load real models, so they need tens of GiB of free aperture — run them with the
router stopped or on small models. `tools/fit_probe.py`, which produced the superseded claim, stays in
the tree with a pointer to this correction.

`fit = on` is now suppressed as a no-op rather than emitted: the server's default is `on`, so the line
the live file used to carry said nothing. `fit-target`'s default 1024 is likewise suppressed, while a
deliberate `fit-target = 8192` is still written — suppressing restatements, not decisions.

**Open items.** The per-checkpoint price is still a token-count guess. `parallel = -1` is priced as 4
slots, now confirmed for this build but re-read per build rather than derived. `HEADROOM_BYTES` and
`host_cache_bytes` may partly overlap: the 4 GiB margin and the priced host caches both describe costs
that are not weights or KV, and one clean load measured end to end would say how much each explains.

#### 6.12.8 — the day the benchmark OOM-killed the router (2026-09-30)

Measured, not inferred, and the cause was mine. `tools/slot_bench.py --attach` was pointed at the
**resident** model to get numbers without loading anything. The state at the time:

| | |
|---|---|
| loaded model | `ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF:IQ3_S`, 77.86 GiB of weights |
| context the worker was given | **262144** — the model's full trained context, because the router passes no `--ctx-size` and the model asks for that |
| aperture in use | 86.93 / 108.00 GiB |
| `MemAvailable` before the first probe | **0.00 GiB** |
| first probe | `/completion`, 512-token prompt → HTTP 500; next two → connection refused |
| what happened | the kernel OOM-killed the router service; systemd restarted it |

The worker's own args show why there was no margin — the runtime's defaults are not free, and this
section had never priced them for a model loaded at its trained context:

```
-cram, --cache-ram N        max cache size in MiB (default: 8192)      -> up to 8 GiB per worker
-ctxcp, --ctx-checkpoints N context checkpoints per slot (default: 32)
-cms,  --checkpoint-min-step N  minimum spacing between checkpoints (default: 8192 tokens)
```

So a load priced as *weights + KV at ctx* under-states the truth by the prompt cache and up to 32
checkpoint copies per slot, and at 262144 tokens those are not rounding errors. `est_total` and
`peak_bytes` both said this model "fits" — they were right about the aperture and blind to the host
caches on top of it. That is the same gap §6.12.6 left open ("`HEADROOM_BYTES` and `host_cache_bytes`
may partly overlap"), now with a date and a corpse attached.

What changed in the tool, so it cannot do this again:

- `--attach` has a hard floor: it reads `MemAvailable` **before** sending anything and refuses below
  6 GiB (12 GiB if `--big-probes` is asked for), then re-checks between probe groups and halts.
- attach mode's default probes are 512 and 2048 tokens; the 8192-token prefill is opt-in.
- the first server error stops the speed probes: a 500 or a refused connection means the worker is
  already in trouble, and further requests are both noise and pressure.
- every run reports the **lowest `MemAvailable` observed**, not just peak aperture.

Verified against the live box afterwards: it now prints `MemAvailable 0.97 GiB — below the attach
floor, refusing to probe` and sends no request at all.

**What the portal owes the operator because of this.** Not a silent context cap — the standing rule is
that a model's context is only limited when the author chooses it or when it is the only way to fit.
What is owed is the number this event turned up: **free memory after load, including what the runtime
will ask for by itself.** A personality that loads a 262144-context model and leaves 0.97 GiB spare is
a personality that will lose the router, and the editor should say so in the same breath as it says
"fits". Concretely: price `--cache-ram` (8192 MiB default) and `--ctx-checkpoints` against the chosen
context in `peak_bytes`, surface `free after load` next to the verdict, and treat
`ctx = trained_ctx` with a thin margin as a warning rather than a success.

#### 6.12.9 — the benchmark hung on a draft model, and that was two defects (2026-09-30)

The operator ran `slot_bench.py` against `Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF:Q8_0`
and had to kill it. Their reading of the file was correct and mine was not: the `-MTP-`
repo is a **speculative-decoding head**, meant to be paired with the matching full
model, and it cannot be exercised on its own.

Two separate defects, both now fixed in `tools/slot_bench.py`:

- **No per-request timeout.** Every call inherited the socket default, so a stalled
  generation wedges the whole run — the script cannot report what it cannot finish.
  Each probe now names its own budget (240 s for tool calls, 600 s for the big decode,
  180 s for the small ones), and a slow model is a slow number in the report instead of
  a hung terminal.
- **It probed a draft as if it were a model.** The script now refuses any id or file
  whose name carries a draft marker (`mtp-`, `eagle3-`, `dflash-`, `dspark-`) unless
  `--spec mtp` is given. Refusal is a line in the report; a hang is not.

What "paired" means on this build, read off the binary rather than assumed: the MTP
repo is a **complete model that also carries the nextn tensors** — `Q8_0/Qwopus3.5-4B-
Coder-MTP-Q8_0.gguf` has 310 tensors, 21 architecture keys, and `blk.32.nextn.*` at the
end, the same arch as the base repo. So the pairing is one file plus
`--spec-type draft-mtp` (b11146 help, the `--spec-type` line), not two files loaded
together. `--spec mtp` passes exactly that. The `/metrics` mapping already carries
`spec_decode_num_draft_tokens_total` / `..._accepted_tokens_total` / `..._drafts_total`,
so once a run uses `--spec mtp` the acceptance rate is a number in the report, which is
the only way to know whether draft decoding earns its keep on this box.

Open: whether `--spec-draft-hf <mtp repo>` against the **base** model is a second
configuration worth measuring — a base target plus the MTP head as an external draft.
Not assumed; it needs a run.

#### 6.12.10 — why the MTP run could never start (2026-10-01)

`--spec mtp` reported "the target has no nextn head" for a file that does have one.
Four independent defects, all inside `tools/slot_bench.py`, all hidden by the same
bare `except Exception: return None`:

1. `nextn_tensors()` used `struct` — a name the module never imported. NameError.
2. It built its mmap inside `with open(...)`, so the fd closed before the first
   read; every access raised EBADF. Measured directly: an mmap outlives nothing
   if its file object is gone.
3. Its inline GGUF string reader sliced the payload **at** the u64 length prefix
   rather than after it, so every field after the first key read 8 bytes early.
4. The beside-model draft glob matched the target's own name, so the MTP file was
   passed to llama.cpp as its own `--spec-draft-model`; and `--spec-draft` decided
   "this is a path" from a slash, which every `org/repo` id contains.

Fixes plus `tests/test_slot_bench_spec.py` (6 tests on synthetic GGUF headers with
real tensor-info records): suite 621 passed, 1 skipped.

**Measured on this box, both Qwopus files:** `Qwopus3.5-4B-coder-Q8_0.gguf` and
`Qwopus3.5-4B-Coder-MTP-Q8_0.gguf` each carry **4 `*.nextn.*` tensors** and are
each 4.29 GiB. So on this pair `--spec mtp` is one file, and `--spec-draft` is
only needed for the other shape (unsloth's separate `mtp-*.gguf`). The earlier
note that the base file has no head was read through the broken parser.

Open question the run answers: does draft decoding earn its keep here? The pair
is the same weights, same ctx, 4.3 GiB each — the control and the treatment cost
the same aperture, so a decode-speed difference has nowhere to hide.


#### 6.12.12 The burst probe never measured anything (2026-10-01)

`infill_burst4` reported "fewer than 2 of 4 concurrent requests succeeded" in every
report, and the per-language rows beside it looked healthy — so the failure read
like a server limit rather than a bug. It was a bug: the single-request probe had
been fixed to `/infill`'s body (`input_prefix` / `input_suffix` / `input_middle`),
the burst probe still sent `prompt` / `suffix`, and the runtime answered 500
`key 'input_suffix' not found` (server-context.cpp:4853). The worker's own log said
so on lines 114-117 of `slot-bench-20261001T053028Z.logs/codegemma-2b-GGUF.log`
while the report said only "fewer than 2 of 4". Two rules follow, both in the tool:

* one `fim_payload()` builder for every `/infill` call, and a test that counts
  `srv.post("/infill"` against `fim_payload(` so a fourth call site with hand-written
  keys fails the suite;
* a probe that swallows an exception has to put the reason in the report
  (`infill_burst4.why`), or the number becomes a verdict about the server instead
  of a verdict about the harness.

Autocomplete concurrency — the actual question the burst answers, and the reason an
FIM slot needs its own measurement — is therefore **still unmeasured** on this box.


#### 6.12.11 Speculative decoding: measured, and it is not a portal setting (2026-10-01)

The user corrected a false premise, and the file proved it:
`Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF` is not a draft to be paired with a separate
target. Read both headers with the fixed walker:

| file | tensors | nextn tensors | KV keys | size |
|---|---|---|---|---|
| `Qwopus3.5-4B-coder-Q8_0.gguf` | 441 | **4** | 33 | 4.29 GiB |
| `Qwopus3.5-4B-Coder-MTP-Q8_0.gguf` | 441 | **4** | 35 | 4.29 GiB |

Both carry the head (`blk.32.nextn.{eh_proj,enorm,hnorm,shared_head_norm}.weight`),
so **both** Qwopus repos can draft with their own head, and `--spec-type draft-mtp`
needs one `--model` and no `--spec-draft-model`. My earlier 303-vs-310 counts (in
§6.12.10) came from an ad-hoc parser that mis-walked the tensor section; this table
supersedes them. `find_draft` returning the target as its own `--spec-draft-model`
— the hang the user hit — was the tool lower-casing the marker test and matching
"MTP" inside the target's own name; the naming rule is now shared with modelindex
(`NOT_A_MODEL_MARKERS`, case-sensitive), pinned in `tests/test_slot_bench_spec.py`
and `tests/test_bench_tool.py`.

Measured on this box, same file, ctx 8192, 4 slots, `--spec` off vs on
(reports `slot-bench-20261001T1624*` are no longer on disk; numbers recorded here):

| probe | spec off | spec on |
|---|---|---|
| decode tg64 | **41.51 tok/s** | **95.86 tok/s** |
| tool round-trip mean | 0.531 s | 0.439 s |
| 4 concurrent tool calls | 4×200, 0.90–1.22 s | 4×200, 0.83–1.01 s |

2.31× decode for one flag on a 4B dense Q8_0. The runtime publishes the evidence
beside the speed — `llamacpp:spec_decode_num_draft_tokens_total`,
`..._accepted_tokens_total`, `..._drafts_total` (server-task.cpp:1554-1563) — and
`slot_bench` maps them, so a run that claims a draft speedup has to show the
accept rate that produced it.

**The decision.** In b11146 no `--spec-*` key is preset-eligible: the whole
speculative group sits in the argument block presets cannot reach (see the
eligibility probe, `tools/preset_eligibility_probe.py`). The portal writes
`presets.ini` and nothing else, so **speculative decoding is not a portal setting
and stays out of the settings registry.** That is a rule, not an omission: a key
the runtime accepts only on the command line cannot be applied by a personality,
and a UI control that writes nothing is worse than no control. Re-open it if a
future build moves those keys — check `llama-server --help` and re-run the probe.

**§6.12.8 correction — what `--kv-unified` pools.** The journal for the 77.86 GiB
load shows `slot ...: n_ctx = 131072 (2 slots)`: the worker got **two** 131072-slot
pools, ~13.6 GiB together, while the aperture rose only **11.84 GiB** over the
weights (77.86 → 89.70). What is measured, and what follows for the estimator: the
runtime reserves prompt cache and checkpoints on its own
(`--cache-ram` 8192 MiB, `--ctx-checkpoints` 32 per slot), `--kv-unified` pools the
KV **slots** but not that budget, and neither `est_total` nor `peak_bytes` prices
any of it against a 4 GiB `HEADROOM_BYTES`. `slot_bench` now prices the per-worker
cache in the non-unified case too and prints it in the floor line
("(+8 runtime cache)") so the number compared against free memory is the one the
runtime will actually ask for.

#### 6.12.13 Two candidate models, one engine fork, and a borrowed projector (2026-10-03)

**`ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF` (cached: IQ3_S-mtp, 11.29 GiB) reads clean on every
header signal**: arch `qwen35`, 65 blocks, embedding 5120, trained ctx 262 144, vocab 248 320, FIM
specials present, `nextn_layers = 1` and 4 `*.nextn.*` head tensors in the same file — free
speculative decoding, no sidecar, `--spec mtp` costs 0.0 GiB of extra file, template markers `chat,system,tool-results,tools` so the tool
protocol is declared, pooling unset (generative). Expected decode from the box's measured dense rate
(~4.1 GiB/s of weights): 11.29 GiB → **~18 tok/s**, and with its own head at the 27B's measured
accept ~0.62 → **~35-38 tok/s**, against unsloth Q8_K_XL's measured 7.2 / 14.8. That is the answer to
"is the Q8_K_XL really slower than an IQ3-class file" — the gap is bytes-per-token, not branding, and
the price is a 3-bit-class quant: the card's own KLD is 0.051 at IQ3_S against ~0.002-class for Q8,
measured **against Swift's own BF16**, at 512-token context only, and the card concedes IQ3_S is
*worse* than ISTA's matched quant on five of seven held-out domains. MTP decoding is unmeasured by
the author. It is a post-train ("long-horizon, agentic and coding tasks, 58.5 % fewer thinking
tokens, +0.35 % score") — inside the fine-tune tolerance — but the derivatives around it
(`...-Uncensored-...`, `heretic`) are not, and the licence is `swift-open-license-1.0`.

**`jcbtc/Qwen3.8-Flash-CIRU-STRIX-Orca` is parked, and the gate for revisiting it is written down.**
Four independent disqualifiers, all from its own card: (1) *"The compact expert format needs the
matching CIRU runtime. Stock llama.cpp and Hugging Face hosted inference do not load it"* — it is not
a llama.cpp model, it is a patched ggml fork with its own build chain (measured on NixOS); (2) files
≈ 120 GiB (67.16 GGUF + 48.83 GiB `ple/` per-layer-embedding payload that must live on fast NVMe +
3.85 GiB MTP draft + projector), with **87.31 GiB GPU-visible after loading** on a 128 GiB box — on a
108 GiB aperture that leaves no room for a second slot, an embedder, or an autocomplete slot, which
is the whole point of the router; (3) provenance: `orcarouter/Qwen3.8-Flash-Next-Uncensored`,
"refusal-removed", no alignment layer added — refused for the coding/agentic slot by the operator's
standing rule; (4) its evidence is self-reported and hedged by its own author ("these few runs do not
establish…", HumanEval 0-19 subsets, a HermesAgent scorer known to miss instruction violations).
The claims that *would* matter here — 1 251 tok/s prefill at 64 K, 59 tok/s decode with MTP depth 4 —
are one-slot measurements. Our personalities run **up to** four models — most want two, the
default/safe/large-model profiles want exactly one — and every speed number measured here so far
was taken at `--parallel 4`, the configuration few personalities will actually use. slot_bench
now defaults to one slot for that reason; the multi-slot numbers are the pessimistic end and are
being re-measured.

Decision, and it is an operational rule, not a preference: **the portal's llama.cpp is not replaced.**
Any second engine is evaluated in quarantine — its own release directory, its own port, a probe that
starts and stops only its own child, the portal's `presets.ini`/router/release dir untouched, and
never two routers resident at once (they both answer for one aperture). Only after that probe reports
the advertised numbers on *this* box does it earn a first-class toggle in the portal, and that toggle
must be mutual exclusion, not two live servers.

**Projector interchange is a header question** (see `MODEL_SIGNALS.md`, "Cross-file signals"):
llama.cpp compares one number, `n_embd_text` vs the projector's `projection_dim` (`mtmd.cpp:604`).
5120 == 5120 for Swift 1.5 + unsloth's `mmproj-BF16.gguf`, the Swift vocabulary still carries
`///`, and its rewritten template (8 952 B vs 9 993 B) still
mentions vision — so the borrowed projector is predicted to load. `tools/mmproj_swap_probe.py` prints
that prediction and then asks a synthetic PNG what colour it is, using the portal's own build.

**Open item — an accuracy harness, staged.** slot_bench measures speed, aperture, concurrency and
*whether a tool call is emitted at all*; it never checks whether emitted code is correct. Proposal in
three stages, deliberately not built yet: (1) a task pack of parameterised short functions
(easy→hard) with hidden unit tests, executed in a rlimit'd subprocess — parameterised on purpose,
because public HumanEval/LeetCode text is training data and a memorised answer measures nothing;
(2) depth-aware needle-in-a-haystack recall, which is the test that actually prices a context window
on this box (it pairs with the pp-vs-ctx curve and `--ctx-checkpoints`), scored exactly; (3) only
then a portal "model testing" page, and only showing numbers that carry their provenance — build,
flags, seed, date — the same rule every other portal claim follows. A leaderboard without provenance
is opinion with a table.

#### 6.12.14 The daily model, re-measured at one slot; and a cache id is a file-name artefact (2026-10-03)

`slot_bench --parallel 1`, `--ctx 16384`, the router idle, reports
`slot-bench-20261003T070019Z` and `…T070212Z`. Same file
(`ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF:IQ3_S`, 77.88 GiB across 2 shards,
MoE 512 experts / 10 active, trained ctx 262 144, FIM specials, tool markers) run
twice, defaults vs the residency flags from the operator's notes:

| | defaults | `--load-mode mmap --lazy-mode on --gpu-layers 99` |
|---|---|---|
| prefill pp512 / pp2048 / pp8192 | 409 / 486 / **464** tok/s | 396 / 493 / **474** tok/s |
| decode tg64 (same three prompts) | 23.0 / 23.0 / 20.8 tok/s | 22.9 / 23.2 / 21.0 tok/s |
| TTFT at pp8192 | 13.12 s | 12.84 s |
| tool round-trip | emitted, args parsed | emitted, args parsed |
| **aperture peak** | **52.71 GiB** | **52.71 GiB** |
| **lowest MemAvailable** | 38.88 GiB | **65.64 GiB** |
| VmRSS peak (0.5 s poll) | 28.52 GiB | 51.60 GiB |

Three things follow, and the third is a measurement warning, not a model fact.

**The flags buy headroom, not speed.** Speed is inside the noise either way
(pp8192 464 → 474), the GPU-visible footprint is identical to the hundredth of a
GiB, and the system kept 27 GiB more free at the low point. That is the shape you
would expect from 512 experts: most expert rows are never touched by a given
prompt, so keeping them resident is only a larger anonymous footprint, while
`--lazy-mode on` reads the rows through the page cache on demand. For a personality
that co-exists with other models, `--lazy-mode on` is therefore a safety setting —
and it was already what the hand-started router used, which is now documented
rather than folklore.

**VmRSS is not a capacity number.** The two runs report RSS 28.52 and 51.60 GiB
while the *worse* MemAvailable belongs to the *lower*-RSS run. RSS counts mapped
file pages (weights read through mmap — reclaimable, and excluded from
MemAvailable by design), so the two metrics can point in opposite directions and
both be true. `slot_bench` records `peak_rss_gib` as a 0.5-second-poll maximum; it
is a diagnostic of what one process touched, never a number to size a personality
with. The portal's `peak_bytes` prices aperture + MemAvailable, which is what this
table supports; keep doing that.

**A 16 K context is almost free on this architecture, and expensive to be wrong
about everywhere else.** KV at ctx 16384 priced 0.16 GiB for one unified-pool slot
against a trained ctx of 262 144. The same ctx on `unsloth/Qwen3.8-27B` costs an
order of magnitude more per token. So context length is a per-model decision, not
a personality-wide one — which is why `ctx-size` belongs to the model section of a
preset and not the global one.

**`ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF` is cached as `:MTP`, not `:IQ3_S-mtp`.**
`llama-server --cache-list` prints `ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF:MTP`
(its own `IQ3_S-mtp.gguf`) next to `unsloth/Qwen3.8-27B-GGUF:Q8_K_XL` — the tag is
the file name's last dash-separated token, and unsloth's `UD` prefix is dropped by
the same rule that keeps our `mtp` suffix. Asking for `:IQ3_S-mtp` printed "not
cached" for a file already on disk, because the message listed *hub* files only.
`slot_bench.miss_hint()` now prints the ids llama.cpp offers and points at the
cached file when the request matches its name, with `-`/`_` spellings folded
together; pinned by two tests in `tests/test_bench_tool.py`. The quant itself is a
column (`quant = IQ3` for this file), never part of the id — the id says which file
llama.cpp will load, not what it was quantized with.

#### 6.12.15 The small 27B that decodes fastest here — measured at one and two slots (2026-10-03)

`ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF:MTP` (11.29 GiB single file, dense 27.3 B
params, IQ3 weights, trained ctx 262 144, `:MTP` because the head is inside it).
Three `slot_bench` runs, `--role coder`, router idle, reports
`slot-bench-20261003T143432Z` / `…T143538Z` / `…T143658Z`.

| run | slots | ctx/slot | prefill pp512/2048/8192 | decode tg64 | accept | aperture peak |
|---|---|---|---|---|---|---|
| head **off** | 1 | 16 384 | 326 / 362 / 354 | **16.7 / 16.4 / 16.0** | — | 13.36 GiB |
| head **on** (`--spec mtp`) | 1 | 16 384 | 309 / 356 / 344 | **26.6 / 25.1 / 30.3** | 0.62 / 0.56 / 0.77 | 14.83 GiB |
| head on, wide personality | 2 | 8 192 | 318 / 366 / skipped | 27.1 / 25.3 | same | **14.86 GiB** |

**The head is worth 1.57× decode for 1.5 GiB of aperture.** Compare §6.12.4 on the
same architecture at Q8_K_XL: 7.2 → 14.8 tok/s was 2.06×. The multiplier shrinks
because IQ3 weights stream in 3.6× faster, so a bigger share of the step is compute
the draft has to redo. The absolute number is what matters here: **25–30 tok/s is
the fastest decode of anything cached on this box**, 32 % above the MoE we run daily
(23.0, §6.12.14) at **3.6× less aperture** (14.9 vs 52.7 GiB).

**The second slot is almost free — in aperture.** 2 slots cost +0.03 GiB over 1 slot
at 8 192/slot, and decode did not drop at all (27.1 vs 26.6). `--kv-unified` prices ONE
pool, so ctx is what moves the number, not the slot count: the pool costs 0.44 GiB at
8 192/slot and 0.88 GiB at 16 384/slot for two slots together (dry-run confirms). The
constraint a wide personality actually hits is **tokens per slot**: at 8 192/slot the
pp8192 probe was refused — `needs ~8256 tokens, slot serves 8192` — so a 2-slot
personality of this model cannot take a full 8 K prompt without trimming. And per
token this file is 5.5× the MoE's appetite: 0.88 GiB for the pool where Flash-Next
asks 0.16 GiB for the same ctx. That trade (weights in aperture vs tokens in flight)
is the personality decision, and both sides are priceable from headers alone.

**Its tool protocol is declared *and* exercised** — `supports_tools`,
`supports_parallel_tool_calls`, `supports_preserve_reasoning`, `supports_reasoning_effort`
all true in `/props`, and the bench's round-trip emitted `read_aperture` with valid JSON
arguments at 7.5 s. So for a `chat`/`agent` role this file has no header-level caveat.

**Infill rows are liveness in a `coder` run, but these ones carry a hint.** All eleven
splice probes returned coherent code (c# 123 chars, lua 80, bash 125), where the daily
MoE at the same ctx returned an empty python completion in 217 ms and a bash row that
repeated `echo "gtt_used"` four times (§6.12.14). Both files declare the four FIM
specials, so the header cannot tell these apart — this is exactly the gap the labelled
task-pack is for, and it says the 27B deserves a `--role fim` run before it is ruled out
of an autocomplete slot.

**The borrowed projector passes the header gate.** `mmproj_swap_probe --header-only` on
`unsloth/Qwen3.8-27B-GGUF`'s `mmproj-BF16.gguf` (0.87 GiB) against this file: LLM
`embedding_length = 5120`, projector `projection_dim = 5120`, `projector_type =
qwen3vl_merger`, and the LLM vocabulary still carries ``/``/
`vision_start`/`vision_end`. mtmd's single numeric gate matches, so the loader will
accept the pair; whether the post-train's chat template can place the image
placeholder is what the live probe still has to answer. Nothing was started — the
router was holding 87 GiB at the time.

**`spec-type` is carryable in `presets.ini` on b11146 — in both scopes.**
`tools/spec_preset_probe.py --key spec-type --value draft-mtp`, no `--measure`: a
`[*]` section and a per-model `[ukisai/…:MTP]` section both start the router cleanly
and the key reaches the worker command line (`--spec-type` visible in `/models`
status args). The `mmap` control case still fails as it did in September, which is
what makes the acceptance meaningful rather than the parser shrugging. **Not yet
proven to engage**: that needs `--measure`, which loads a worker, and the box had no
room. Until then a personality may set MTP, but the portal must not claim it is on.

**My resolver was wrong twice on the same file, and the second error was the
dangerous one.** First I rejected `…-IQ3_S-mtp.gguf` as a sidecar because I patterned
`mtp` as a suffix; llama.cpp's test is a *substring* of the basename for `mtp-` with
its trailing dash (download.cpp:661), and this file is the model with its head inside.
Then my test caught the overcorrection: a tag-only substring search resolves `:IQ3_S`
to the MTP-tagged file when no IQ3_S-tagged file exists — pricing one quant and
measuring another. `cached_model_file()` now matches the exact tag from
`modelindex._hf_split` (the portal's own transcription of `get_gguf_split_info`, reused
rather than re-transcribed) and only then falls back to llama.cpp's lenient rule.
Six tests in `tests/test_mmproj_probe.py`, including: an adapter named
`mtp-Qwen3.8-27B-Q4_0.gguf` is never returned as a model, only shard part 1 is
loadable, and an absent repo stays absent.

#### 6.12.16 The id the runtime prints is not the id that loads (2026-10-03)

`spec_preset_probe --measure` asked the probe router to load
`ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF:MTP` — an id **`llama-server --cache-list`
printed itself** for a 12.12 GiB IQ3_S file sitting in the cache. It never loaded, and
the router log says why:

```
D download task: https://huggingface.co/ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF/
    resolve/d74895b.../Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ2_S-mtp.gguf -> ~/.cache/...
D common_download_file_single_online: no previous model file found blobs/6ce61b53...
```

It started pulling a **different quantization of the same repo** — IQ2_S, 12 GiB —
rather than load the IQ3_S file already on disk. Two rules in llama.cpp disagree:

| | rule | source |
|---|---|---|
| **listing** | id = last dash-separated token of the file name → `...-IQ3_S-mtp.gguf` is `:MTP` | `get_gguf_split_info`, mirrored by `modelindex._hf_split` |
| **loading** | `repo:TAG` → regex `TAG[.-]`, case-insensitive, **first** non-sidecar match in the repo's file list wins; shards other than part 1 skipped | `find_best_model`, `common/download.cpp` |

Both `...-IQ2_S-mtp.gguf` and `...-IQ3_S-mtp.gguf` match `/MTP[.-]/`, IQ2_S sorts
first, and the tie-break that would prefer the closest quantization is disabled
because the tag has no digits (`extract_quant_bits("MTP") == 0`). The file has an id
at all because the sidecar filter tests for the substring `mtp-` — `mtp` as a
**suffix** is not excluded, so a draft-head-bearing file is listed as a model.

A personality naming that model would trade its chosen weights for a smaller quant
and download 12 GiB to get them. What the portal now does about it:

* `modelindex.gguf_is_model_name()` / `loader_pick()` — the load rule ported, and
  pinned by tests against the exact repository listing above.
* `id_round_trip(row, siblings)` — a fact when the rule picks a file we can see
  (`id-resolves-elsewhere`), and a warning when the tag is not a quantization at all
  (`id-is-not-a-quant`: safe only as long as the hub holds no rival, which an offline
  rescan cannot know). One such problem on this box right now, recorded by `rescan()`.
* `capabilities()` adds a warn badge — *id not load-safe* / *id loads another file* —
  naming IQ2_S in the note, so the models page says it before a personality bakes it in.
* `slot_bench.miss_hint()` no longer recommends an id that matches more than one hub
  file; it prints the path instead. Last cycle it told the operator "try `:MTP`".
* Both probes now start the router `--offline` by default. A bad id should be an
  error, not a transfer. `--allow-download` opts out.

**The fence available today:** `offline = true` in `[*]` is preset-eligible and
reaches the worker as `--offline` (probe, same run). That makes the router refuse
every download; downloads then stay a deliberate act through the portal.

While this was running the same probe answered the personalities blocker:
`spec-type = draft-mtp` **is** accepted in a per-model section and reaches the worker
command line (`relevant args: ['--spec-type']`). Whether the draft actually runs at a
worker started that way is stage 3 and still unmeasured — it needs a load, and the
operator's own worker was holding 87.6 GiB of the 108 GiB aperture at the time.

**Vision with a borrowed projector works.** The earlier `DID NOT ANSWER AS EXPECTED`
was my probe, not the model: a reasoning template spent all 40 `max_tokens` inside
the thinking block, so `content` was empty (`finish_reason=length`) while 40 tokens
streamed as `reasoning_content`. Fixed: the probe starts `--reasoning off` and reads
either field. Rerun on `:MTP` + unsloth's `mmproj-BF16.gguf` at ctx 16 384:

```
server says: loaded multimodal model, '.../mmproj-BF16.gguf'
prompt tokens text-only 25 -> with image 43 (the image contributed 18)
image question answer (content, finish=stop): 'Red'
vision through a borrowed projector: WORKS
```

The control request (same text, no image) is what makes that a verdict: without it a
template that silently drops the image would still look fine. 18 image tokens is the
floor, not typical — `--image-min-tokens` is now passed through for anything harder
than "what colour".

#### 6.12.17 A preset section really does turn MTP on — and three instruments were blind while claiming to measure it (2026-10-04)

The stage-3 question was whether `spec-type = draft-mtp` written into a per-model section of
`presets.ini` survives the router→worker hop and actually drafts, or whether speculative decoding
is one of those flags that only works on the command line. It works. One completion against
`Jackrong/Qwopus3.5-4B-Coder-GGUF:Q8_0`, ctx 8192, temperature 0, section `[Jackrong/Qwopus3.5-4B-Coder-GGUF:Q8_0]`:

```
--spec-type present in the loaded worker's argv            yes
/metrics delta    drafts 18   draft tokens 54   accepted 44      accept 0.815
worker's own log  draft acceptance = 0.81481 (44 accepted / 54 generated), mean len 3.44
decode            79 tok/s
```

The two accounting channels agree token for token, which is the reason both are kept: `/metrics`
needs a flag, the log line does not.

**What was wrong before the answer was believable.** Every defect below reported `None`, and a
column of `None`s reads as "the draft generated nothing" — a confident wrong answer:

* `/metrics` is refused unless the server started with `--metrics`
  (`server-context.cpp:4670`, "Start it with `--metrics`"). The probe never passed it.
* The counter names are `llamacpp:spec_decode_num_drafts_total`,
  `..._draft_tokens_total`, `..._accepted_tokens_total` (`server-task.cpp:1554+`, and the
  Prometheus writer adds the `llamacpp:` prefix). The probe first parsed
  `llama:spec_drafts_total`, which no build has ever published, then the unprefixed names.
* A router **proxies** `/metrics` to a worker (`server.cpp:216`, `models_routes->proxy_get`),
  so with more than one model in play the request has to name the model. Same rule as
  `/completion`: an unnamed request is refused, and every earlier run of this probe ended in an
  `HTTP 400` traceback for exactly that reason.
* A fresh worker counts from zero, so a failed *before* reading must not void the verdict; it is
  reported as a single reading instead.

**Two id rules in the listing, measured on a throwaway hub (`/tmp/fakehub`, never the library).**
Both explain why the same probe input printed `True` on one run and `False` on another:

* Several cached files that reduce to the same tag collapse into **one** model. Two files named
  `Muse-IQ2_S-mtp.gguf` and `Muse-IQ3_S-mtp.gguf` produced a single `[cache] orga/Muse-MTP-GGUF:MTP`.
  Which file that id reaches is decided by the load-time matcher of §6.12.16, not by the listing.
* A preset section and a cache entry with the same name are **not both listed**: the section wins
  and the `[cache]` line disappears. So `[ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF:MTP]` in
  `presets.ini` is simultaneously the intended way to configure that cached file and a way to hide
  the file's own registration.

**The phantom section, and why the portal cannot produce one.** A `[name]` section whose model
resolves to no file makes the router *advertise* that model; asking for it spawns a worker that
never loads — state stays `loading`, no error, and under `offline = true` no download either.
Verified by accident on the throwaway hub, where the only "model" was my own section. The portal
cannot write this: `resolve_targets()` refuses (`personality.py:661`, "names models or model
configs with no library entry and no stored config"), which is the fence doing its job. Hand-edited
`presets.ini` has no such fence.

**Box state noted while checking that refusal:** `/home/cortana/ai/models/llama` now holds only
`INDEX.json` — 12 index rows, all `source=cache`. The builtin `safe` personality still targets
`Qwen3.5-0.8B.Q4_K_M`, a library model that is gone, so rendering `safe` currently raises instead
of writing. That is correct behaviour and an operator decision to fix (retarget it, or put a small
model back in the library), not something to paper over in code.

#### 6.12.18 A personality that names a gone model refuses — now with candidates (2026-10-04)

`safe` targets `Qwen3.5-0.8B.Q4_K_M`, a library model that no longer exists: `/home/cortana/ai/models/llama`
holds only `INDEX.json`, and all 13 indexed rows come from the hub cache. `resolve_targets()` already
refused (`personality.py`, "names models or model configs with no library entry and no stored
config"), and the personalities page already caught it per card — so this was never a 500 and never a
silently wrong preset. What was missing was the second half of the answer. The message now reads:

```
personality 'safe' names models or model configs with no library entry and no stored config:
Qwen3.5-0.8B.Q4_K_M — smallest that can serve a chat slot: lmstudio-community/codegemma-2b-GGUF:Q5_K_M
(1.71 GiB), mradermacher/Granite-4.1-3b-coder-GGUF:Q8_0 (3.37 GiB),
Jackrong/Qwopus3.5-4B-Coder-GGUF:Q8_0 (4.29 GiB)
```

Two lists from `suggest_for()`, deliberately not a fix: **closest by name** (`difflib`, cutoff 0.55 —
this is how a rename, a re-quantization, or an org move gets recognized: `Qwen3.5-0.8B.Q4_K_M` →
`orga/Qwen3.5-0.8B-GGUF:Q4_K_M`), and **smallest that can serve a chat slot**, sized.

The filter on the second list is the part worth keeping. The two smallest rows on this box are
`embeddinggemma-300M` (0.31 GiB) and `Qwen3-Reranker-0.6B` (0.60 GiB); both are `pooling_type` rows,
so offering them for a chat slot would be cheap, indexed, and unable to answer a completion. Rows with
`pooling_type` are excluded and the label says what the list is for.

It stays a refusal. Substituting a model the operator did not choose is exactly the failure mode this
portal keeps being bitten by, so the suggestions ride *beside* the error, never instead of it.
`apply`/`preview` raise `PersonalityError` → HTTP 400 with the same text, and the page shows it on the
card rather than dropping the card.

#### 6.12.19 `safe` and `default` generated the same presets.ini — what a personality cannot do (2026-10-04)

After retargeting `safe` (its model had been deleted; the operator chose
`Jackrong/Qwopus3.5-4B-Coder-GGUF:Q8_0`, 4.29 GiB, cached), `render(safe)["text"] ==
render(default)["text"]` — byte for byte. Both were `[*]` with `lazy-mode/stop-timeout/threads` and
no model sections at all, because:

  * a bare model entry means "that file, llama.cpp's own defaults, nothing added" (§4.10.4), so it
    emits no section; and
  * `default` names every model and sets nothing for any of them, so it emits none either.

**What this establishes about the design.** `presets.ini` carries *settings per model*, never an
allow-list. The router discovers models from `--models-dir` and the hub cache; a personality naming
one model does not hide the other twelve. That was already implied by the refusal of `[*]`
model-scoped keys (§4.10.2, "[*] applies to every model the router *discovers*"), and now it is
stated where a person choosing a personality will read it. A personality on this box can set: global
floor keys, per-model settings for models it names, and — only via the launcher, not the preset —
`--models-max` (measured earlier: `models-max = 1` inside the preset file did not stop either model
from loading, §4.10.2).

**The bug it exposed: "active" could be true for several personalities at once.** The page marks a
personality active when `render()` equals the file on disk. Identical bodies made `safe` and
`default` both active — a state the router cannot be in. `render()` now writes
`# personality: <name>` into the header, so the file records who wrote it and at most one
personality matches. Two personalities that legitimately produce the same body are still the same
router behaviour; the file no longer claims to be two things.

**Measured, not assumed: cache ids are legal section names.** A probe router on :8145 with
`--models-dir ~/ai/models/llama` (which holds no models) and `--models-preset` pointing at a file
containing `[Jackrong/Qwopus3.5-4B-Coder-GGUF:Q8_0]` listed:

```
Available models (13):
  [     cache] ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF:IQ3_S
  [    preset] Jackrong/Qwopus3.5-4B-Coder-GGUF:Q8_0
```

so `sections[<repo:TAG>]` is honored and the generated header comment — which told the reader section
names "MUST match directory names under ~/ai/models/llama" — was wrong on a cache-only box. The
header now says directory names *or* cache model ids, with the measurement next to it. `check_generated`
accepts a cache id when it is indexed and refuses it when it is not (control asserted in the test).

`safe` is also now defined as *chat-capable smallest*, not smallest: the two smallest indexed rows are
`embeddinggemma-300M` (0.31 GiB) and `Qwen3-Reranker-0.6B` (0.60 GiB), both `pooling_type`, neither
able to answer the completion a fallback exists to answer.

#### 6.12.20 The context a client sees is three settings plus a silent chooser (2026-10-04)

The operator's report: on the portal-launched router, a model reported **about half** the context
window it had always reported, with "whatever config or preset settings it was running under",
while the same model under a hand-started router was fine. The ask that came with it is the
design rule this section exists for: *the default profile must not artificially limit a model's
context, and where a setting limits it as a side effect, that setting's help must say so.*

There is no single knob called "context per slot", which is why it was hard to find. The number a
client is given is computed at load from three settings and one chooser:

```
window = min(llama_n_ctx_seq, --kv-unified-per-slot (if set), n_ctx_train)
```

`tools/server/server-context.cpp:4246-4254` (`n_ctx_slot()`), and that same function is what
`/props`, `/slots` and `/v1/models` report (`:4419`, `:4775`, `:4825`) — so this is the number
clients and the Web UI read, not an internal one.

**1. `--parallel` decides whether the pool is shared or divided.** With slots left **auto**, the
server turns on the unified KV cache and each slot can use the whole pool:

```
n_parallel is set to auto, using n_parallel = 4 and kv_unified = true
n_ctx_seq = 262144            # 4 slots, window 262144
```

Set slots **explicitly** and the unified cache is off (`common/common.h:578` defaults
`kv_unified = false`; `common/arg.cpp:1726` says "default: enabled if number of slots is auto"),
and ctx-size is divided across the slots:

```
--parallel 2 -c 32768   -> n_ctx_seq = 16384   # 32768 / 2
n_ctx_seq (16384) < n_ctx_train (262144) -- the full capacity of the model will not be utilized
```

`src/llama-context.cpp:293` is the branch: unified ⇒ `n_ctx_seq = n_ctx`; not unified ⇒
`n_ctx_seq = n_ctx / n_seq_max`. **Requesting slots is the setting that halves the per-slot
window** — the most surprising thing measured here, because "more slots" reads as capacity and is
also a division of the one number every client reads.

**2. Leaving `ctx-size` unset makes llama.cpp choose, and the choice is priced by `--fit`.**
Unset means "size the pool so every slot can have the trained context" — `trained × slots` — and
`--fit` (on by default, target 1024 MiB free) pays for that by shrinking the pool when the
aperture cannot:

```
common/fit.cpp:267,271   n_ctx_max = n_ctx_train * n_streams; n_ctx = n_ctx_max
common/fit.cpp:393-430   linear interpolation down toward --fit-ctx * n_streams, rounded to 256*n_streams
common/common.h:483      fit_params_min_ctx = 4096        # the floor, -fitc/--fit-ctx
```

The reduction is logged with `LOG_TRC` — trace level. In a normal router log the context
**shrinks and leaves no trace**, which is exactly the report: a smaller window, "I never set a
context size", and nothing in the log about one.

**3. Pinning ctx-size locks the chooser out.** `common/arg.cpp:1643-1645` sets
`fit_params_min_ctx = UINT32_MAX` when `-c` is given, and `fit.cpp:459` prints
`context size set by user to N -> no change`. This is why the hand-started router
(`-c 262144`, `-fa on`, `-ngl 99`, `--lazy-mode on`) never showed the shrink.

**4. `--kv-unified-per-slot` is the lever for naming the window directly** (new in this build;
`common/arg.cpp:1648-1655`, `tools/server/server.cpp:169-177`): with ctx-size unset, the pool is
sized to `slots × N` and the window is `N`.

Measured on this box (b11396, `Jackrong/Qwopus3.5-4B-Coder-GGUF:Q8_0`, 4.29 GiB, trained 262144,
probe on loopback with no presets; the 4.3 GiB model is a probe subject, not the 177B):

| settings | slots | window a client sees |
|---|---|---|
| (none — what a personality emits today) | 4 auto | 262144 |
| `parallel = 2` | 2 | 262144 (pool auto-extended to 524288) |
| `parallel = 2`, `ctx-size = 32768` | 2 | **16384** |
| `parallel = 2`, `ctx-size = 32768`, `kv-unified` | 2 | 32768 |
| `kv-unified-per-slot = 8192` | 4 auto | **8192** |

**Why the 177B is the case that shows it.** Its trained context is 262144 and it is the
heaviest resident here (~78 GiB of weights, 84 GiB with its estimate). Full per-slot context
costs a 262144-token pool *per slot*, so 4 slots need a pool ~4× the KV. On a 108.00 GiB
aperture, `fit` pays for the weights first and the pool is what is left — so the window lands
below trained, and it lands there quietly.

**Preset eligibility, measured on the build the router runs** (`tools/preset_eligibility_probe.py`
with `mmap` as the positive control, which is rejected as it must be): `temp`, `top-k`, `top-p`,
`min-p`, `presence-penalty`, `repeat-penalty`, `dry-multiplier`, `kv-unified-per-slot` and
`fit-ctx` are all accepted in a preset section. So a personality *can* carry a model's upstream
"recommended llama settings" — it just could not name them, because the registry had 20 keys and
none of these were among them.

**What the portal does with this.** `modelindex.context_window()` computes the window and names
**who decided it** (`ctx-size`, `kv-unified-per-slot`, `slots divide the pool`, or `fit, because
ctx-size is unset`). Eleven tests in `tests/test_context_window.py` pin it to the four rows above,
plus the fit-shrink and the cannot-price cases. A registry entry now cites this section, and
`/help` carries the prose.

**What the portal did not get.** The registry did not gain the keys above. Measured in §6.12.21:
the build this box runs refuses 22 of them, so they are not writable here.

#### 6.12.21 The settings registry offered flags the running server refuses (2026-10-04)

**Why this audit.** A key in the registry is a line the generator writes into `presets.ini`. If
`llama-server` does not know the flag, the router hands it down and the model never loads. The
settings editor would be a button that stops the router. So every key must be checked against the
binary that runs, not against a source tree.

**How it was measured.** One process per key:

```
llama-server --model codegemma-2b-Q5_K_M.gguf --offline --host 127.0.0.1 --port <free> \
             -ngl 0 --ctx-size 1024 --parallel 1 --<key> [sample value]
```

Accepted means the log printed `llama_server: listening on`. Refused means
`error: invalid argument: --<key>`. A control key, `--definitely-not-a-flag`, produced the same
refusal line, so "refused" means "unknown", not "my value was wrong". Scripts:
`tools/registry_flag_audit.py` (all 30 keys, committed 2026-10-05: `--list`, `--keys a,b`, `--json`, private temp work dir, kernel-assigned ports), plus `/tmp/probe2.py` and `/tmp/probe3.py` for the flag-shape checks.

**What it found.**

1. **All 30 registry keys are accepted** by `llama.cpp-b11396`, except three the registry already
   declares router-only: `load-on-startup`, `stop-timeout`, `dedup-cache-models`. Those are not
   server flags, so there is nothing to accept them.
2. **`--kv-unified` takes no value on this build.** `--kv-unified true`, `false`, `1`, `0`, `on`,
   and `auto` were all refused. `--kv-unified` bare and `--no-kv-unified` were both accepted. The
   registry lists `kv-unified` as a choice of `on|off|auto`, so the open question was the preset
   layer: does a `kv-unified = <value>` line in `presets.ini` survive `common_preset`, and what
   argv does it become? **Measured 2026-10-05** with `tools/spec_preset_probe.py` against
   `codegemma-2b-GGUF:Q5_K_M` (report: `/tmp/spec_preset_report.json`):

   | `presets.ini` line | argv the worker received | model loaded |
   | --- | --- | --- |
   | `kv-unified = on` | `--kv-unified` | yes, 59 chars at 98.5 tok/s |
   | `kv-unified = true` | `--kv-unified` | yes, 98.2 tok/s |
   | `kv-unified = off` | `--no-kv-unified` | yes, 98.3 tok/s |
   | `kv-unified = auto` | `--kv-unified` | yes, 98.3 tok/s |

   The preset parser rewrites the value into the flag form the server accepts, so the registry's
   `on|off|auto` choice is safe to write. Two facts follow, and the registry help now states both.
   **`auto` is not "unset" — it turns unification on**, and `auto` is the registry default, so
   naming this key at all unifies the pool. To leave the decision to the server, remove the key.
3. **22 flags documented in the newer source at `~/ai/llama.cpp` do not exist in b11396:**
   `mlock`, `no-mmap`, `cache-type-q`, `keep-echo`, `top-a`, `chat-escape`, `chat-regex`,
   `top-p-slg`, `ctx-read`, `cache-ram-buffers`, `prefill-verify`, `prefill-min-tokens`,
   `context-patterns`, `pool-want`, `parallel.slots`, `mrope`, `grammar-debug`,
   `chat-completion-suffix`, `chat-render`, `chat-template-obj`, `no-default-weights`,
   `fallback-padding-off`. They read perfectly plausibly in the source, which is why "the source
   documents it" is not a test.

**The guard.** `tests/test_personality.py` now pins the registry to the measured list
(`AUDITED_B11396_ACCEPTS`) and keeps the 22 refused flags in `FLAGGED_NOT_IN_B11396`. Adding a
key without measuring it fails the suite; so does taking one of the refused flags. Verified by
putting `mlock` in the registry: both tests failed, naming it.

**The lesson worth keeping.** Two ways to learn what a binary accepts, and both can lie.
`llama-server --help` prints no server-params section on this build, so a help-text check gives
false negatives for server-only keys. And the source tree is newer than the deployed build, so a
source check accepts flags the box cannot load. Start the server and read its log.

#### 6.12.22 The window sweep: trained context is the cap (2026-10-05, b11396)

**Why this run.** `modelindex.context_window()` predicts the tokens a client gets. It was fitted
on one model (Qwopus, trained 262144). Predicting from one measurement is a guess, so the operator
ran eight setting combinations on a model with a small trained context and read the window back
from the server's own `/props`.

**The sweep.** `lmstudio-community/codegemma-2b-GGUF:Q5_K_M` — 1.71 GiB, trained 8192. Each server
started `--offline` on its own port, `sleep 14`, then `/props` was read:

| settings typed | slots reported | window reported |
| --- | --- | --- |
| (nothing) | 4 | 8192 |
| `--parallel 2 -c 32768` | 2 | 8192 |
| `--parallel 2 -c 32768 --kv-unified` | 2 | 8192 |
| `--parallel 2` | 2 | 8192 |
| `--kv-unified-per-slot 8192` | 4 | 8192 |
| `--parallel 2 --kv-unified-per-slot 8192` | 2 | 8192 |
| `-c 8192` | 4 | 8192 |
| `--parallel 4 -c 131072` | 4 | 8192 |

**What it proves.** A slot gets `min(n_ctx_seq, kv-unified-per-slot, n_ctx_train)`
(`server-context.cpp:4246-4254`). Trained 8192 sits below every pool in the table, so every row
reads 8192 — pools of 32768 and 131072 included. **A bigger pool buys slots, not tokens per
client.** Rows 2, 3, 6 and 8 are the discriminating ones: a predictor without the trained-context
cap reports 16384 for row 2 and 32768 for row 8, and both are wrong on the measurement.

**For the recommended settings.** `--parallel 2 -c 32768` on a model trained for 8192 gives two
slots of 8192 each, priced at two slots. It does not give either client a 32768-token window. Ask
the same question of a big-trained model and the answer changes: Qwopus (trained 262144) served
`slot_ctx=16384` at `--ctx-size 16384`, and `slot_ctx=32768` at `--ctx-size 32768 --parallel 2`.

**The big-trained rows, from `tools/slot_bench.py` (router stopped, 2026-10-05):**

| run | served | decode tok/s | notes |
| --- | --- | --- | --- |
| Qwopus Q8_0, ctx 16384, 1 slot | 16384 | 41.7 | prefill 1605–1928 tok/s, tool call OK |
| Qwopus MTP Q8_0, ctx 16384, `--spec mtp` | 16384 | 94.1–97.1 | draft 47–48 of 16 tokens, accept 0.98–1.00 |
| Qwopus Q8_0, ctx 32768, 2 slots | 32768 | 40.1–41.7 | 4/4 concurrent `/infill`: 0.78, 1.77, 2.51, 1.93 s |
| codegemma Q5_K_M, ctx 4096 (`--role fim`) | 4096 | 102.3–104.5 | `pp8192` skipped: slot ctx 4096 is below 8192 |
| embeddinggemma-300M Q8_0, ctx 2048 | 2048 | — | embeddings dim 768 in 45.9 ms; `/rerank` → 501 |
| Qwen3-Reranker-0.6B Q8_0, ctx 16384 | 16384 | — | embeddings dim 1024 in 22.4 ms; `/rerank` 2 results in 36.9 ms |

**Speculative decoding, measured.** The MTP file took decode from 41.7 to 94–97 tok/s — 2.3x — with
acceptance 0.98 to 1.00 and 47–48 draft tokens per 16-token step. It costs nothing extra in
aperture (peak 6.28 GiB against 6.69 GiB for the plain file) because the head is the target's own.
One catch the index now shows: **the MTP file has no vision tower.** The portal reports
`vision=True` for `Qwopus3.5-4B-Coder-GGUF:Q8_0` and `vision=False` for the MTP file of the same
weights. A vision job must load the plain file.

**Capability rule, checked live.** The rule that `pooling_type 4` means `/rerank` works and
`pooling_type 1` means 501 was measured from the server's answers, not inferred: embeddinggemma
returned 501, Qwen3-Reranker returned 2 ranked results in 36.9 ms.

**The guard.** `tests/test_context_window.py` pins all eight sweep rows plus the divide notes.
Verified by breaking the code: removing the trained-context cap failed 1 test; treating named
slots as unified failed 3. Both mutations restored, suite clean.

**Reports kept:** `/home/cortana/ai/logs/slot-bench-20261005T1511*.json` and `.logs`,
`/tmp/spec_preset_report.json`.


#### 6.12.23 The personality page states tokens per client, and links its own evidence (2026-10-05)

**What changed.** A personality card reported what the box pays (worst-case resident GiB)
and never said what a client gets. Two personalities, one handing 16384 tokens per request
and one handing 8192, looked identical. `personality.context_for()` now returns the window,
who decided it, and the loaded cost per model. The card renders them as a table.

**No new ruler.** The window comes from `modelindex.context_window()`, the same predictor
`tests/test_context_window.py` pins to server output. The cost comes from
`personality._conservative_peak()`, the existing over-estimating fallback.

**The gap the feature exposed.** `peak_bytes()` prices only the context the index stored. A
pinned `ctx-size` asks for a different context, so the predictor answered `unpriced`.
`context_window()` now takes a `price` callable; `context_for()` passes
`_conservative_peak`. Measured with the fixture: `ctx-size 32768` over 2 named slots is a
16384-token window, not 32768.

**Who decides, named.** Naming slots divides the pool. Naming `ctx-size` locks fit out.
Leaving both unset gives the trained context, capped. The row says which of the three it is,
because the number alone cannot explain itself (§6.12.20).

**Citations became links.** `kb.ref_of()` reduces a citation string to a section id, and the
settings table prints `why (§6.12.20)` for the 27 keys that cite a section. Keys citing
source lines only render no link. `tests/test_personality_routes.py` fails if any link opens
nothing. Mutation check: matching the first number in a citation instead of `sec N` produced
`citation links that open nothing: ['43']`.

**Unpriced is an answer.** A model with no header geometry reports `window: None` and says
the index cannot price it. It is never shown with a guessed token count.

**Suite isolation, measured.** Running an ad-hoc subset in a custom order produced 6
help-page failures (401 and missing sections). Each module passes alone; the canonical
`tests/` run is 758 passed, 3 skipped, and `scripts/check-ui.sh` exits 0. Order artifacts are
diagnosed by running the module alone, then the full suite, before treating a failure as a
feature defect.

#### 6.12.24 What the monitoring pages cost, measured on the running portal (2026-10-05)

**Why.** The operator's note: the metrics page is visited only during testing, and the
refresh ticks should follow need, load created, and importance. The audit measured first.

**Server cost per request, median of 3, live portal at :8090:**

| Endpoint | Median | Polled by |
| --- | --- | --- |
| `/api/router` | 0.6 ms | dashboard, every 2.5 s |
| `/api/metrics` | 0.6 ms | metrics page, every 2.0 s |
| `/api/health` | 0.5 ms | health page |
| `/api/hf/status` | 219 ms, one cold call 16.2 s | dashboard, **on page load and on button, never on a timer** |

**Read.** The four hot endpoints are sub-millisecond: cached state, sysfs reads, no
subprocess. The one expensive endpoint is not polled. So dashboard polling is not the load.

**Where real cost sits.** `metrics.gpu()` shells out to `rocm-smi` — measured 77 ms per
refresh, cached 5 s (`_GPU_TTL`). The metrics page polls every 2 s, so 3 of every 5 polls
re-read CPU, memory, and disk only, and rocm-smi runs at most every 5 s. Opening the
dashboard costs the server about 220 ms of subprocess work for the huggingface panel.

**CPU percent depends on the interval.** `metrics.cpu_percent()` is a delta between
successive calls. The poll interval *is* the measurement window: at 2 s it is a 2 s average,
at 5 s a 5 s average. Faster polling does not make the number more current, it makes it
noisier.

**Fixed in the same pass.** Disk and GPU on `/metrics` are rendered once, at page load, under
a heading that said "Live metrics". They now print their sample time and the poll reports its
own time. `tests/test_ui_contract.py` fails if the sample time disappears; the mutation check
produced `assert 0 >= 2`. The literal `location.href = '/login'` is now
`gotoInternal('/login')`, which rejects scheme, authority, and `//` prefixes.

#### 6.12.25 Re-auditing on b11429, and the three things the audit itself got wrong

The operator's `~/ai/bin/llama-server` moved from b11396 to b11429, so the registry's
eligibility claims went from measured to two-builds-old. Re-running `tools/registry_flag_audit.py`
produced 3 refusals and a wall of `exit(-6)`/`exit(-11)` — all of them artifacts. Fixed, then
re-measured: **26 accepted, 4 declined, 0 refused, exit 0.**

**Artifact 1 — preset-only keys probed on a command line.** `stop-timeout`, `load-on-startup`,
`dedup-cache-models` are read from presets by the server's preset parser; `--stop-timeout` is not
a CLI flag and never was. `kv-unified` is value-mapped the same way: the CLI refuses
`--kv-unified auto` because the flag takes no value, while a preset writes the bare
`--kv-unified`. The registry now marks it `preset_mapped`, and the audit declines these keys
with the name of the tool that can measure them.

**Artifact 2 — the probe model.** The picker took the smallest cached file. The smallest is an
embedder, and b11429 aborts loading one:
`GGML_ASSERT(n_outputs_max <= cparams.n_outputs_max)` in `llama_context::output_reserve`
(`src/llama-context.cpp:2275`) — its output count is the embedding width. That is the `exit(-6)`.
The next pick was an MTP draft, which segfaults as a model (`rc=-11`). A probe that dies says
nothing about the flag it carried. The picker now reuses the portal's own rules —
`modelindex.NOT_A_MODEL_MARKERS` (substring, covers every `_is_aux` prefix) and `_shard_of` —
plus `pooling_type` from `modelmeta.sniff_gguf`. It lands on `codegemma-2b-Q5_K_M`.

**Artifact 3 — a clean audit exited 1.** "Declined" counted as "did not accept" made four keys
print under *keys the build did not accept*. A nightly run on that exit code alerts on four
working keys forever. Declined is now its own verdict, listed separately, exit 0.

**Measured 2026-10-05 on b11429, 30 keys, probe model `codegemma-2b-Q5_K_M` (1.71 GiB),
`-ngl 0 --ctx-size 1024 --parallel 1`, 4 probes in flight:** accepted 26, declined 4
(`kv-unified`, `stop-timeout`, `load-on-startup`, `dedup-cache-models`), refused 0.

**The audit rule this pins.** When a build moves, re-run the audit; read a `declined` line as
"not measurable this way", never as a defect.

### 6.12.26 The settings surface, measured against v0.6.0 (2026-10-06, b11429)

The operator's `~/ai/bin/llama-server` moved from b11396 to b11429, and `b11429` **is** the
`v0.6.0` tag (`v0.6.0=69ecae922`, `b11429=d81235049`; the installed libs say `0.5.0` in the
b11396 release dir and `0.6.0` in the b11429 one). A version bump is when llama.cpp renames,
merges and drops flags, so the whole settings registry went from measured to two-builds-old.
Measured from the build and the source, not from the changelog:

**1. The server's own settings surface did not move between the installed builds.**
`llama-server --help` from b11396 and from b11429 is **byte-identical** (736 lines, 198 flag
names each, zero added, zero removed). The registry needs no rename, no removal, and no
re-measurement of key names. `tools/registry_flag_audit.py` re-run against b11429 remains the
26 accepted / 4 declined / 0 refused result of §6.12.25.

**2. The version bump added two flags, one of which is not a server setting.** Across
`v0.5.0..v0.6.0` in `common/arg.cpp`: `+--nextn`, `+--spec-draft-sampling`, nothing removed.

- `--nextn` is declared `.set_examples({LLAMA_EXAMPLE_IMATRIX})` — an `llama-imatrix`
  data-collection switch. It is not a server flag, not in the server's `--help`, and not a
  preset key. The portal must not treat "nextn" as a setting: the model's own MTP head is a
  file fact (§4.9.3), and `--spec-type` is the knob that uses it.
- `--spec-draft-sampling {greedy,probabilistic}` is server + cli + spec, env
  `LLAMA_ARG_SPEC_DRAFT_SAMPLING`. Greedy takes the draft's argmax; probabilistic samples the
  draft and lets the target verify by rejection sampling. It existed before b11396, so it is
  already in the deployed build and already reachable from a preset section. The registry did
  not name it; it is the first candidate key for the registry-as-data work (§6.14.1), and
  §6.12.17 already proved a preset section carries `spec-type` to the worker.

**3. The preset parser changed shape, not behaviour.** `common/preset.cpp` moved 114 lines
between the tags: `std::string` paths became `std::filesystem::path` with
`fs_path_to_utf8()` at the log and error boundaries, and the models-directory scan switched
from `fs_list()` to `std::filesystem::directory_iterator`. Key acceptance, section handling
and `ignore_unknown_keys` are untouched.

*A hypothesis I killed before it became a finding:* `fs_list(models_dir, true)` looked like a
recursive scan and `directory_iterator` is not, which would have meant the router stopped
seeing model directories nested more than one level deep — exactly the thing `health.model_store()`
reports. It is false: `fs_list`'s second parameter is `include_directories`, not `recursive`,
and its body is a plain non-recursive `directory_iterator` in v0.5.0 too. The rewrite is a
like-for-like substitution. Checked the implementation, not the call site.

**4. The preset-only key set is identical in both versions.**
`common_params_add_preset_options()` in v0.5.0 and v0.6.0 declares the same three keys:
`load-on-startup`, `stop-timeout`, `dedup-cache-models`. They have no CLI form, which is why
they are absent from `--help`, and why the flag audit declines them (§6.12.25). That is now
explained by the source, not inferred from the probe.

**5. Registry defaults cross-checked against the build's own words.** New instrument,
`tools/registry_defaults_audit.py`: parse `llama-server --help` into arg blocks and compare
each registry default with the default the build states. `personality.render()` omits any key
equal to `SERVER_DEFAULTS`, so a default that disagrees with the build is a silent preset
change — a no-op line the server would have applied anyway, or a dropped line the server
needed. Result on b11429, 30 keys: 28 agree, 2 are deliberate sentinels, and the 3 keys the
help states no default for are exactly the preset-only trio of §4.

- `kv-unified`: registry `auto`, help `enabled`. Kept on purpose — `auto` behaves as on
  (§6.12.21), so omitting the line reproduces the server's own behaviour, and the registry's
  help tells an operator who wants the server to decide to delete the key.
- `kv-unified-per-slot`: registry `0`, help `unset`. Same reasoning: 0 means the server
  divides the pool, which is what omitting the key does.
- Formatting only (not value disagreements, normalised by the tool): `temp 0.8` vs `0.80`,
  `presence-penalty 0.0` vs `0.00`, `repeat-penalty`, `dry-multiplier`, and bools printed as
  `enabled` where the registry stores `True`.

**6. One real gap, found by that cross-check.** `dedup-cache-models` is in `SETTINGS` with
`default: False` and has **no `SERVER_DEFAULTS` entry**, so `render()` cannot recognise its
value as the default. An operator who sets it to `false` gets a line written that changes
nothing — the artifact §4.10.0 exists to prevent. Fixed by adding the entry, with a test that
the key is omitted when it equals the default.

**7. What 0.6.0 offers the portal that is not a setting.** `GET /v1/models` and the router's
`GET /models` now carry an `architecture` object — `input_modalities` (text plus each media
type, from the projector file) and `output_modalities` (from GGUF metadata). The router
computes both **offline**: unloaded and sleeping models report them, a native decision model
advertises `decisions` and is served by `/v1/systemone`, and cached values survive sleep and
unload. The portal derives its capability column from GGUF headers itself (§4.9.3); the router
now publishes the same class of fact for models it has never loaded, which makes it a
corroborating source for the index and a candidate `visible_when` source for the settings
table (§6.14.1). Not a migration — a second measurement to compare against.

**The rule this pins.** Between two installed builds, diff `llama-server --help` first: it is
one command, it is the surface the portal renders, and it settles key renames and defaults in
one comparison. Read the source diff for *why*, after the help diff says *what*.

### 6.13 The model-index warm-up thread, and what it was breaking (2026-09-24)

`create_app()` used to do three things on one daemon thread: build the schema and first-run password
(`_settings()`), create `model_index`, and rescan the library. Only the rescan needed to be off the
request path — a library on a cold mount must not stall a page load — and putting all three there cost
twice.

**In production:** an app could be constructed with no schema and no password, and nothing waited for
the thread to fix that.

**In the test suite, where it had been hiding:** the thread rescans whatever `app.core.paths` points at
at the moment it happens to run. A dozen test files build the app while monkeypatching `paths` to their
own throwaway homes, and the whole suite shares one `model_index` table. A warm-up scan started by one
file lands during another and rewrites the rows that test is asserting on. At HEAD this surfaced as
tests in `test_personality.py` failing at random — a *different* test each run, none of them touching
the index — and the captured log inside the failing test named the culprit directly:
`main.py:442 model index: 2 model(s)`, from a thread that test never started. The failing test also
proved it was not new: 2 failures in 3 runs of the same file combination at HEAD.

| | |
|---|---|
| schema + `modelindex.init()` | synchronous in `create_app()` — cheap, idempotent, needed before the first request |
| library rescan | still off the request path, gated by `AI_PORTAL_INDEX_STARTUP` |
| test suite | `tests/conftest.py` sets it to `0` for the session; the tests that are *about* the scan set it back to `1` and wait for the rows |
| UI harness | sets it to `1` — its portal runs in a subprocess and cannot race this process's tables |
| threads | recorded in `main.INDEX_STARTUP_THREADS` and joined after each test, so a re-enabled scan cannot outlive the test that asked for it |

Verified rather than asserted: the four-file combination that had been failing ran 4/4 clean with the
guard, and produced a failure in 1 of 4 runs with `tests/conftest.py` moved aside. Full suite **572
passed, 1 skipped**, twice in a row, where before it was nondeterministic.

`_seed_library()`'s own docstring had described this race accurately and worked around it by seeding
the directory the thread would scan. It now rescans itself. The workaround was the tell: a test that
has to guess which directory a background thread is about to read is a test that depends on it.

### 6.14 Design briefs from the operator (2026-10-06)

Two briefs, recorded here so the next session starts from the same picture. Both are
directions for the polish phase, not work orders for today. Each has a first slice that
can ship alone.

#### 6.14.1 The personalities page: a deck, a flow, and a settings table

Measured first: the page renders 3672 x 16720 px. That is a document, not a console. The
settings editor is the long part — every key the portal knows, with a paragraph of help
prose, repeated per model, inside a form.

What the operator asked for:

1. **A stacked deck of personality cards.** Every card shows its title strip. Only the
   selected card shows its body. Clicking a title raises that card. The card raised on
   page load is the active personality; raising a card is not activating it. Those are
   two verbs and the page says which one happened.
2. **The editor is a flow, not a wall.** Three visible steps: pick an existing
   personality or start a new one; set global settings; attach model settings. A
   personality is a container — global settings plus any number of named per-model
   settings, and it may hold none.
3. **The prose leaves the form.** The knowledgebase holds the explanation; the row shows
   one short line and a link to the section that proves it (§6.12.21 already built the
   citation links and the guard that keeps them opening).
4. **Settings become data.** The registry row grows the fields a renderer needs: name,
   alias, param kind (switch, bool, number, string, path, list), default, scope (global,
   model, both), validation rule, allowed values or range, help text, help link, a
   visibility rule, and substitutions for measured facts (cpu and gpu thread counts, a
   model's reasoning levels). llama.cpp grows flags and changes meanings; a data edit is
   the target, not a page edit.
5. **Row layout is fixed.** Upper left: `NAME [control] (default)`. Below: one short
   line. Below that: a collapsible detail area that opens the help section.
6. **Visibility is measured, not asserted.** A setting shows when the model's metadata
   says it applies — FIM specials, pooling type, nextn tensors, chat-template markers.
   The header index (§4.9) already reads those facts; the editor asks it.

First slice, smallest shippable: express the 30 registry entries in that row structure
(`kind`, `scope`, `default`, `accepts`, `help`, `cite`, `visible_when`) and render one
row type from it. The page looks the same. The data is what changes, and everything
after it — the deck, the flow, the per-model settings editor — reads from the same
structure. `tools/registry_flag_audit.py` (§6.12.21) is the tool that keeps the table
honest after a llama.cpp upgrade: 30 keys, one run, flags the build refused.

Reusable pieces the operator wants kept reusable, named here so the split is deliberate:
the personality card, the settings row, the citation link, and the sizing ruler
(`modelindex.context_window`) that the page, the bench tool, and the probe all read.

**First slice shipped 2026-10-06** (`app/core/settingrows.py`). The registry rows — 32 now —
carry `aliases`, `range`, `values`, `specials`, `unit`, `visible_when` and `substitutions` in a
presentation table merged into `SETTINGS`, so there is one table to read and not two to keep in
step; `settingrows.row()` derives the rest (renderer `kind`, `scope`, the rule sentence, the help
link, the writable refusal). The rules it enforces:

- **A visibility rule names an index fact or raises.** `VISIBLE_FACTS` is built only from columns
  the index stores, so `ctx-checkpoints` and `checkpoint-min-step` drop per model when
  `full_attention_interval` is 1 (checkpoints are gated per model, §6.12.6) and `jinja` drops for a
  file with no chat template. `reasoning` is deliberately *not* a fact: the index has no column for
  template reasoning fields, so `reasoning-effort` has no visibility rule, and inventing one would
  be an assertion about a column that does not exist.
- **A quoted number comes from the table the budget prices.** `{cache_ram_mib}` resolves to
  `SERVER_DEFAULTS["cache-ram"]`, `{threads_default}` to `GLOBAL_DEFAULTS`, `{ctx_trained}` to the
  model row. A help line with its own copy of 8192 is a second ruler.
- **A declared bound is the bound `accepts` enforces.** `tests/test_settingrows.py` probes every
  range against the validator, one step outside the lower bound included. It caught a real one on
  arrival: `n-gpu-layers` had been given `range: (-1, None)` because llama.cpp's CLI takes -1 for
  auto, while this portal's validator accepts `auto`/`all`/`int >= 0` and refuses -1.
- **A new key is a data edit.** `spec-type` and `spec-draft-sampling` are the proof: defaults
  quoted from b11429's own help (§6.12.26), acceptance measured with `tools/registry_flag_audit.py`
  and recorded in `AUDITED_B11429_ACCEPTS`, no template touched.

The keys table on `/personalities` renders from `settingrows.rows()` and nothing else. The editor's
grids and the deck are the next slices.

##### 6.14.1a The editor becomes the row component (2026-10-07)

**What changed.** Both editor grids now render `settingrows.editable_rows()` in the fixed row layout:
NAME, the control, `default 8192 MiB`, one short help line, and a collapsible detail carrying the
rule sentence, the side effect, the spellings b11429 declares for the key, the model facts the row
needs, and the link to the /help section that proves it. Long prose went into the detail, which is
what keeps the grid readable -- the page was 16,720 px tall because nothing was collapsible (§6.12.21).
Control ids are unchanged (`ed-g-<key>-sel|val`, `ed-m-<key>-sel|val`), so the jsdom harness drives
the same controls it always drove, and six cases pass.

**Per-model visibility, measured from the index.** `_model_row_limits` walks the index rows and asks
`settingrows.hidden_reason` for each row, and the result rides on the model `<option>` as JSON. The
client hides the rows the selected file cannot use and writes the missing fact into the row:
`jinja` on a file with no chat template says so rather than disappearing. Computing this on the
server is deliberate -- the naming rules are `VISIBLE_FACTS`, and a JavaScript copy is a second,
weaker statement about the same files.

**The rule that makes hidden rows harmless.** The per-model controls are one set of inputs reused
across every model, so a value typed for a model that supports a setting is still in the box when a
model that does not is selected. `captureCurrent` skips hidden rows and `loadModel` clears them, so
a row the file cannot use contributes nothing to the payload. The limits blob is fail-open on purpose
(a parse failure shows every row): a row offered and refused on the preview is a visible refusal,
where a row that silently vanishes is a setting nobody can find.

**An equivalent mutation, found by breaking the code.** Five guards were mutation-checked. Four fired.
The fifth -- passing `settingrows.rows()` where the route passes `editable_rows()` -- passed every
assertion in `tests/test_personality_routes.py` and `tests/test_ui_contract.py`. Nothing is wrong
with the test: the registry currently contains no non-writable key, so the two calls return the same
rows and the page cannot show a difference. "The editor offers only writable keys" was an
unfalsifiable claim. `test_a_key_the_router_owns_is_explained_and_never_offered_a_control` fixes that
by constructing the case: `setting_is_writable` is made to refuse `flash-attn`, and the page must
show the key in the keys table with *the router sets this* on the row and offer no control for it in
either grid. With that test in place the same mutation fails.

**Enforcement.** `tests/test_personality_routes.py::test_the_editor_rows_carry_their_default_their_rule_and_their_proof`
compares every rendered row against the row layer -- which keys are offered, whether the default is
stated (and only when one exists), the derived rule sentence, the side effect, the aliases, the help
link, and the presence of a visibility line. `test_the_page_says_which_rows_a_model_cannot_use_and_which_fact_is_missing`
compares the page's limits against `hidden_reason` for the same index rows. `test_ui_contract` checks
the two page-source properties the rendered page cannot prove: the grids loop `editor_rows` and not
`setting_names`, and the client treats a hidden row as absent. `tests/ui/spec.json` pins the detail
blocks (14 per-model, 12 global), the unit in the default line, `data-limits` on the options, and the
missing-fact line. Suite 807 passed / 3 skipped.

##### 6.14.1c Slice 3 (the deck) attempted and reverted, with what it taught (2026-10-07)

**Status: not shipped.** The work is in `/tmp/slice3-attempt.patch` (391 lines) against `c84b2d6`,
plus a handful of test-side changes in the same patch. The tree, the suite and the deployment stayed
at `c84b2d6`: 91 tests green in the three modules the patch touched, deployed sha == HEAD. It was
reverted because one assertion in `tests/test_model_configs.py::test_a_personality_is_active_exactly_when_the_file_on_disk_is_its_file`
was failing against the deck and I declined to ship a page whose "which personality is live" story was
not proven.

**What the deck looked like.** Title strip per personality (`<button class="pers-strip" data-raises=...>`,
name plus the `running` and budget badges), body in `<div class="pers-body">`, raised state carried by
`data-raised` on the card (CSS hides bodies off it), raised-on-load = the active personality, and a
`#pers-deck-note` naming both verbs: "Raised: X. The router is running Y. Raising a card changes what
you can read; it does not change what the router runs." The deck's click handler contains no fetch.
Guards written for it: 3 route tests, 1 contract test, 4 `spec.json` expects -- and all four mutation
checks fired, including one found by an equivalent mutation (the constructed active card was the first
card, so "always raise cards[0]" passed until the case used the last card).

**Finding 1 -- a substring helper is not a scope.** `active_names()` in `tests/test_model_configs.py`
finds the active card by searching for the string `pers-active` inside each card's slice. The deck put
the deck's JavaScript on the same page, and that script contains `.pers-active` as a selector, so the
last card's slice -- which runs to the end of the page -- matched every time. The fix (cut the deck at
`<p class="mut" id="pers-editor">` before counting badges) is in the patch and is correct on its own;
the lesson is that the old helper was already reading the page by prefix-matching ids, and the deck's
`pers-<name>-strip` / `pers-<name>-body` ids broke the old split first. Any new id sharing a prefix
breaks that helper. Page-scoping helpers should bound regions by structural markers, not by prefixes.

**Finding 2 -- the unresolved one.** With the deck applied, the strip badge for `work` rendered at the
moment the test's first assertion runs (file on disk hand-written, so nothing should be active), while
the body badge in the same card -- same `{% if g.get('active') %}` condition, same `g` -- did not
render. Under `c84b2d6`'s template the page contains no active badge at that moment at all. Two
identical conditions on the same object disagreeing across two positions in one card is not explained
by the extraction fix, and I did not have the context budget to finish the investigation. Start there:
dump `generated[i]["active"]` per card for that request, and check whether the deck's `{% set raised %}`
block (which iterates `generated` and appends to a list built with `{% set active_names = [] %}`)
influences the per-card `g` binding.

**Rule for the next attempt.** Ship the deck only when "which personality is live" is proven by an
assertion that reads the live badge from the card that carries it, scoped structurally. If that
assertion cannot be made to pass, the deck does not ship -- the page's active-state story is the one
thing on this page that must not be ambiguous, because it is the sentence an operator acts on.

##### 6.14.1d The row is one line, and the fields wear the page's theme (2026-10-07)

**The operator's two notes on slice 2.** (1) *The page is dark and the fields are not.* The inputs
and selects were browser-default boxes on a `#0e1116` canvas. (2) *Each row reads thick* -- the
default took a line of its own under the control, the help sentence took another, and the word
`detail` took a third. She asked for the default to the right of the field, and the collapsible
control to be the short description itself, or `more` at the end of it rather than on a line after.

**What the row is now.** One baseline-aligned flex line:

```
NAME              [ control ]   default 8192 MiB   Total tokens the server shares among its slots…  more
```

The help sentence *is* the disclosure control: `<summary>` holds the help span plus the toggle word,
so the text you read is the text you click, and the rule, side effect, the build's own spellings, the
model facts the row needs and the `/help#§` link appear underneath it when opened. The toggle word is
CSS state (`.ed-detail[open]>summary .ed-more-closed{display:none}` and its mirror), not script state --
a row says "less" when it is open without anything running. Rows 12 (global) + 14 (per-model); the
parts that used to occupy three lines occupy one, and a closed row is now 1 line + a border.

**Fields, site-wide, in `base.html`.** Panel fill, `var(--line)` border, foreground text, accent on
focus, muted placeholder, disabled state, and `select>option` painted too -- the last one matters
because CSS on the field cannot reach the native dropdown. `color-scheme:dark` is what makes the
popups (dropdown list, date picker) dark; without it the field is themed and its list is white.
Scoped in the base template rather than per page: a field is the same object on /settings, /models and
/help, and a theme applied page by page is a theme that leaks.

**Guards.** Three contract tests in `tests/test_ui_contract.py` -- `test_the_help_line_is_the_control_you_click`,
`test_a_row_is_one_line`, `test_the_form_controls_wear_the_page_theme` -- and three live-page expects in
`tests/ui/spec.json` (`.ed-detail>summary .mut.h` minCount 12, `>summary .ed-more-closed` minCount 14,
and a `contains` on a row's summary text). Every one was mutation-checked: putting the help span back on
its own line fails the first; `.ed-default{grid-column:2}` (the old two-line placement) fails the second;
deleting the control-theme block fails the third; and dropping the toggle word from **one single row**
fails the harness, which is the proof that the live-page counts are not decoration.

**A bug the harness caught in my own assertion.** I wrote the `contains` expect as
`#ed-global .ed-field[data-key="ctx-size"] summary`. It failed: `ctx-size` is `scope=model`, so it
renders in `#ed-model-fields` and never in the global grid. The expect was re-pointed at `cache-ram`
(`scope=both`). Worth recording as the ordinary hazard it is: a spec assertion about a row is a claim
about which grid the row layer puts that key in, and the row layer is the place that decides.

**Limit, stated.** jsdom has no layout engine, so "one line" is asserted structurally -- markup order
(control, then default, then details, inside the grid) plus the CSS rules that place them on a baseline
(`.ed-field{display:flex}`, `.ed-default{flex:0 0 auto}`, `.ed-detail{flex:1 1 ...}`) and the absence of
the old `grid-column` placements. That is testable and mutation-checked, and it is not the same
statement as "the row is N px tall"; the pixel number is the operator's eye on the deployed page.

##### 6.14.1e The settings reference moves into the knowledgebase (2026-10-07)

**The brief, and the duplication it kills.** The keys table under the editor rendered key, kind,
scope, default, rule, help and citation for all 32 keys, and the editor rows rendered the same
fields from the same registry two grids above it. Two renderings of one table on one page is how a
page drifts: the row component changed three times in this slice (6.14.1a, 6.14.1c, 6.14.1d) and
the table did not move with it. The brief asked for the detail where detail belongs — a report in
the knowledgebase, next to the prose the citations point at — with the page keeping the scannable
row.

**What exists now.** `app/core/settingsdoc.py` renders `docs/PORTAL_SETTINGS.md` (621 lines, 32
sections) out of `settingrows.rows()` — the identical row list the editor iterates — and `kb.DOC_ORDER`
registers the document, so it is in `/help`, in the table of contents, and searchable. `--write`
writes it, `--check` exits 1 when the committed file no longer matches the registry.

**Why the document cannot disagree with the form.** It is generated, and the generator is tested
against its own output: `tests/test_settingsdoc.py` (22 tests) compares the committed bytes with
what `render()` produces today. A registry edit that skips `--write` is a failing test rather than a
reference that reads as current and is not.

**Anchors, and the one slug rule.** Each key's section is titled with the key, so its anchor is
`kb.anchor_of(key)` — a public wrapper over the slug `_split_sections` assigns to a heading. The row
carries it as `settings_ref`, and the keys table plus **both** editor grids link `/help#<key>`.
`kb` owns the slug because the page and the document must produce the same string: a page-side
`.replace("_", "-")` is a link to a section that does not exist, which is the failure `/help`
already had once (§6.13's citation guard).

**Two claims in my own prose turned out to be false, and the guards found them before I did.**

1. *`settingsdoc.py`'s docstring: "the editor rows link into the reference by key."* They did not:
   the template linked `/help#§cite` only, so the reference had no inbound link from the page that
   generated it. Now true, and tested at three levels — row data (`tests/test_settingrows.py`), the
   rendered page (two tests in `tests/test_personality_routes.py`), and the template's shape
   (`tests/test_ui_contract.py`, which asserts the link is `{{ r.settings_ref }}` three times and
   that **no** anchor is hardcoded in markup).
2. *"They are in the registry so the validator can refuse them with a reason" (the router-owned
   keys, §4).* Measured: none of the eleven `ROUTER_CONTROLLED_KEYS`/`ROUTER_DERIVED_KEYS` is in
   `SETTINGS`. The form's answer is the message for a key nothing defines — `host: no definition
   for this setting` — and the *generator* is the boundary that refuses loudly, raising
   `personality.render() ... sets router-controlled key(s) ['host'] ... the router overwrites them`
   in `[*]` and in a model section alike (`personality.py:1251`, `personality.py:1281`). §4 now
   prints both refusals, and the form's message is **computed from `check_setting` at render time**,
   so changing that behaviour changes the document instead of leaving a sentence that used to be
   true. `tests/test_settingsdoc.py::test_the_reference_states_the_refusal_the_form_actually_gives`
   asserts the quote equals the message the code gives and that no router key is documented as a
   registry key.

**Two smaller measured fixes.** (a) A switch's registry rule string is literally the word `bool`, so
the first draft of the reference told an operator that a switch *accepts* "bool" — a type name
dressed as a rule. `_accepted` now prints the measured behaviour (false is the server's unset state,
so writing false changes nothing, §6.12.26) and the test refuses the type name. (b)
`settingrows.substitutions()` and `expand()` existed with **nothing calling them** — six keys declare
substitutions and the page prints the help prose verbatim. The reference now expands help and side
effects through the same facts, and inherits the row module's refusal of a placeholder nothing
resolves: `--write` fails on a help line that quotes a fact nothing answers, rather than printing
`{cache_ram_mib}` into a document.

**Guards, and what each was mutation-checked against.** (1) Append one line to the committed
document → the currency test fails. (2) Give the registry a key the document lacks → the section,
table, and row-equality tests fail (3 failures). (3) Print help raw, dropping `expand()` → the
placeholder test fails. (4) Change the document's quoted form refusal → the refusal test fails.
(5) Add a colliding `### ctx-size` heading to the plan → the anchor-ownership test fails.
(6) Hardcode one anchor in the template → the contract test fails. (7) Build the slug in the row
module instead of asking `kb` → both anchor tests fail. (8) Drop the keys-table link → the page test
fails. (9) Rename a generated heading to `### ctx-size — settings` → the exact-heading test fails.

**Stated limit, and the next slice.** The router-owned keys are documented in §4 and never rendered
as rows, so the `writable: false` branch of the row component (the "the router sets this" refusal) is
still exercised only by the monkeypatch case `tests/test_personality_routes.py:782` builds — the
registry contains no non-writable key, which is exactly why that test has to manufacture one.
Registering those eleven keys with their own help and citations would make the form's refusal name
the router instead of saying "no definition", and would make the row component's refusal branch real
data. That is a registry edit with a page consequence (`/api/personalities` settings list would have
to exclude non-writable keys, which `tests/test_personality_routes.py::test_router_owned_keys_are_absent_from_the_page`
currently asserts against the registry), so it is recorded as a slice and not slipped in here.

**One addressing gap, found while writing citations here.** A subsection titled `##### 6.14.1a`
carries no number in `kb`: `_LEAD_NUMBER` wants whitespace after the number, so its id is the full
slug and `find("6.14.1a")` matches nothing. A `§6.14.1a` citation is not a 404 — `refs_in` captures
`6.14.1`, which resolves to the parent's *intro*, the text before the subsection. So lettered
subsections are cited by name, not with `§`, until `kb` gives them their number as an address.

##### 6.14.1f The operator's answers on the deck, the flow, and model personalities (2026-10-07)

Measured on the deployed page first, because both complaints turned out to be facts and not
impressions. `/personalities` renders 142,937 characters with **2** cards (`safe`, `default`), **3**
full `presets.ini` diffs, **2** per-model context tables, **62** collapsible setting rows, and **0**
model personalities against a 14-model catalog. The cards eat the page because each card carries its
own diff, and the page cannot create anything because it calls only two endpoints.

**The endpoints for creation already exist; the page does not link to them.** `POST
/api/personalities/save` (create/replace a custom personality), `/api/personalities/delete` (refuses
`safe` and `default` *by name*, so the button says why it is absent), `/api/model-configs/save` and
`/api/model-configs/delete` (name collisions against the catalog and against aliases, base model must
be in the library, values the registry refuses, delete refused while a personality uses the config).
`tests/test_model_configs.py` drives all four at the API level, which is exactly why the gap
survived: the write paths are proven and the page never uses them. A missing feature can pass a
suite that tests the layer below it.

**The four answers.**

1. **A collapsed strip shows name, live, fits, model count** — `▸ coder-heavy · does not fit · 3/6
   models`. One line per personality, so 10 personalities are a 10-line deck. Budget GiB, host
   caches, findings, the per-model context table and the `presets.ini` diff live in the raised body,
   and the diff is closed by default. Density is the operator's requirement: choosing among 10+
   personalities must not need scrolling, and the raised card is the only one that can be tall.
2. **Creating lives in step 1 of the editor flow** — `1 PERSONALITY [name] (new) (delete)`, `2
   GLOBAL`, `3 MODELS (attach)`. The deck keeps one verb: raising is reading. Two verbs on one object
   is why §6.14.1 asked for the deck's click handler to contain no fetch.
3. **Model personalities are shared, global-namespace objects** — name + catalog model + settings,
   attachable to any number of personalities, listed with who uses them. The uniqueness rule is not a
   UI preference: the router keeps aliases in one namespace across every model it discovers, so a name
   colliding with a catalog model, an existing alias, or a personality is a router that exits.
   `model_config.name_problem` already enforces it and `assigned_personalities` already answers "used
   by".
4. **The router's own state belongs on the status page, not on the personalities page.** The
   operator runs a hand-started router because the portal's settings are not yet trustworthy, so the
   router card on `/` gains a report: the effective environment, the launch parameters, the preset
   settings, and what the process answering the port actually is. And start/stop/restart must notice
   *other* `llama-server` processes: tell the operator they exist, and give options, instead of
   starting a second router behind their back. Recorded here as the shape; the panel and the stray-
   process check are their own slice.

**The gate stays.** §6.14.1c reverted the deck over one unproven assertion, and named the rule: ship
the deck only when "which personality is live" is proven by an assertion that reads the live marker
from the card that carries it, scoped structurally. The diagnosis is in that section's Finding 1:
`active_names()` reads the page by slicing between `id="pers-` prefixes, so any new id sharing the
prefix mis-attributes the badge — which is exactly what looked like two identical Jinja conditions
disagreeing. The gate closes by removing the second derivation and the slicing: `main.py` sets active
from `render()["changed"]` (it already does), the card dict carries it, each card states it once as
`data-active-for="<name>"`, and the guard reads that attribute. A marker read by attribute cannot be
mis-attributed by a slice.

##### 6.14.1g The deck ships: one line per personality, the body under the fold (2026-10-08)

Shipped against the four answers in §6.14.1f, and under the gate §6.14.1c set.

**What the page is now.** A deck: per personality a strip — name, `running`, `fits`/`does not fit`/
`budget unknown`, and the model figure — and a body that is hidden until the card is raised. The
model figure reads `14 models · max 1 resident`, not `14/1`: on the live page the fraction form made
`default`'s library-wide attach list look as if it were over its own limit, which is not what
`models_max` means, and the strip is the line a personality is chosen from. The two
things that made cards eat the screen (the per-model context table and the `presets.ini` diff) are
`<details>` disclosures inside the body, closed by default, so the raised card is tall only when the
operator opens one. The deck opens on the personality the router is running. Live inventory at the
time of writing: **2 cards** — `safe` (1 model, max 1 resident) and `default` (14 models, max 1
resident) — against a 14-model catalog, 0 model personalities, and `personalities_on_disk` empty
(every section in `presets.ini` is one the portal can name).

A note on the note: an earlier draft of this section claimed three personalities, the third being
`coder-heavy`. Measured, it does not exist — not as a card, not in `/api/personalities`, not as a
section in `presets.ini`. It came from reading the operator's design mock as if it were an
inventory. That is written down here rather than quietly fixed, because a count invented in a design
note is how a page gets built for data that is not on the box, and this repo's rule is that the
measurements are what the page states.

**The gate closed as §6.14.1c specified.** `main.py` computes active from `render()["changed"]` and
copies it onto the card dict (`zip(..., strict=True)`); the template reads `c.active` — no second
derivation, no `{% set %}` list built in a loop. The live badge is stated twice by design, on the
strip (so the deck answers "which one is running" without opening a card) and in the body, and both
name their card: `data-live-strip="<name>"`, `data-active-for="<name>"`. The guard compares the two
sets **at the moment something is running**, not once at the end of the test. The first version of
that guard passed a mutation that deleted the strip's badge, because the comparison sat after the
test's last state change, where nothing was active and two empty sets agree with each other
forever.

**The deck's one verb.** The deck's `<script>` block moves one attribute and updates a sentence: no
`fetch`, no `XMLHttpRequest`, no `submit`, no `innerHTML`, no `location.href`. Asserted at source
level, per block, in `test_raising_a_card_is_reading_and_not_applying` — which locates the deck's
block by the element it names, so it is the deck's script being tested and not some other script's
quiet corner. Raised state is `data-raised="yes|no"`, an attribute, with CSS doing the hiding; a
mutation that changed the CSS rule to `display:block` fails it.

**Measured teeth (5 mutations, each failing a named guard).** Strip drops its live badge → the
strip/body comparison fails. Deck script gains a `fetch(` → the contract test fails. Bodies stop
being hidden → the contract test fails. `data-raised` pinned to `no` → the raised-on-load route test
fails. Strip drops the model count → the deck-contents route test fails.

**Two collateral findings, both recorded because they are the kind that recur.**
1. *The HTML validator cannot parse Jinja inside a tag's attribute list when it is unquoted.*
   `<div ... {% if c.active %} data-raised{% endif %}>` reports the tag's own `>` as an escape
   problem and then loses the start tag, which cascades into an unmatched `</div>` 120 lines away
   and is a genuine trap for a human reader. The quoted form —
   `data-raised="{% if c.active %}yes{% else %}no{% endif %}"` — validates clean.
2. *`test_the_page_shows_tokens_per_client_for_each_priced_model` asserted the exact string
   `Context each model gives a client`.* Moving the table under the fold changed the title's case
   (it is a `<summary>` now) and the test failed on casing. A guard that pins presentation strings
   pins the presentation; it was rewritten to assert the disclosure exists and the table follows it.

**Known, not introduced here.** `pytest tests/test_personality_routes.py tests/test_model_configs.py`
fails `test_models_none_means_the_whole_library_for_a_custom_personality_too` — the model index
picks up a hub-cache model from the shared throwaway home. Verified identical at `HEAD` by stashing
this slice's tree and re-running the pair: it is the cross-module `AI_PORTAL_HOME` race the plan
already documents, not deck fallout.

##### 6.14.1h Inventory for the deck, and the 11 settings the page could not write (2026-10-08)

**Why.** The deck shipped with two cards, which is enough to prove a guard fires and not enough to
prove anything about arrangement, about model personalities as shared objects, or about picking a
personality out of a list. The operator asked for a set to test with, named
`Function-Model_Short_Name` (`Agent-ThinkingCap-Qwen3.8`, `Autocomplete-…`, `RAG-…`), and suggested
tuning that enhances a model without constraining it. Suggestions, not directives — so the notes in
each config say where each number came from.

**The inventory, authored through the portal's own endpoints.** Five model configs and three
personalities, created with `POST /api/model-configs/save` and `/api/personalities/save`, and
nothing else: `presets.ini` was not touched (`sha256 b1af77a2…` before and after), because writing
that file is Apply's job and Apply restarts the router.

| model config | base | settings, and where they came from |
|---|---|---|
| `Agent-ThinkingCap-Qwen3.8` | `bottlecapai/ThinkingCap-Qwen3.8-27B-GGUF:Q4_K_M` | `temp 1.0 / top-p 0.95 / top-k 20 / min-p 0.0` and `spec-type = draft-mtp` are the model card's own; `parallel = 2`; context left unset so fit prices it |
| `Agent-Swift-Qwen3.8-Flash` | `ukisai/Swift-1.5-…-Flash-Next-GSQ-RCO-GGUF:IQ3_XXS` | the vendor's recommended llama.cpp settings, verbatim (`-jinja -fa on -ngl 99`, sampling as shipped) |
| `Agent-Qwen3.8-Flash-Coder` | `ISTA-DASLab/…-Coder-GGUF:IQ1_M` | same family sampling; **no** `spec-type`: the index reports no `nextn` head on this file |
| `Autocomplete-Qwopus3.5-MTP` | `Jackrong/Qwopus3.5-4B-Coder-MTP-GGUF:Q8_0` | greedy (`temp 0.0`, `top-k 1`) so a completion is reproducible, `spec-type = draft-mtp` (own head, 97 tok/s measured vs 41 without, §6.12.17), `ctx-size = 8192` written on purpose |
| `RAG-EmbeddingGemma` | `ggml-org/embeddinggemma-300M-GGUF:Q8_0` | nothing to tune: `pooling_type 1`, 2048 tokens trained, 0.31 GiB, `/rerank` answered 501 |

| personality | models_max | worst case, measured on this box (budget 108.0 GiB) |
|---|---|---|
| `VSCode` | 3 | **47.2 GiB, fits, 60.8 GiB spare**; `fits_ram` true (host cache 49.8, RAM 122.8, 25.8 spare) |
| `Flash-VSCode` | 3 | 73.7 GiB, fits, 34.3 spare |
| `Agentic-PI` | 1 | 77.2 GiB, fits, 30.8 spare — vendor settings only, context unset |
| (existing) `safe` / `default` | 1 / 1 | 13.9 GiB / 84.4 GiB (15 models now) |

**The block this found, and it is the important part.** Eleven of the registry's 32 keys could not
be written from the page at all. The API's contract is `settings: dict[str, str]`
(`app/main.py`) — a form field is text and a `presets.ini` line is text — and `coerce_setting`
converted ints and returned everything else as a string, while the accepters ask for Python bools
and floats. Probed directly: `check_setting("jinja", "1")` → `'1' is not a valid value (expects
bool)`; `check_setting("temp", "1.0")` → `'1.0' is not a valid value (expects float)`. That is
`jinja`, `cache-prompt`, `cache-idle-slots`, `load-on-startup`, `dedup-cache-models`, `temp`,
`top-p`, `min-p`, `presence-penalty`, `repeat-penalty`, `dry-multiplier` — the sampling set a model
card recommends, which is exactly what the personalities above needed. The page's own switch
submitted `1` and `0`, and the portal refused them. The docstring claimed "Booleans are coerced
from 1/0"; no code path converts `1`, and `presets.ini` never carries a Python int.

**The fix is the runtime's vocabulary, not a style guide.** `TRUTHY = (on, enabled, true, 1)` and
`FALSEY = (off, disabled, false, 0)` are `is_truthy`/`is_falsey` as written in `common/arg.cpp` at
v0.6.0, and a preset value reaches a bool through `is_truthy` (`common/preset.cpp:155`). Floats
parse as floats because the preset path calls `std::stof` (`common/arg.cpp:2021`). `yes`/`no` are
refused by name, not quietly accepted: the runtime does not know them, so a control offering `yes`
writes a line the router reads as false. The row carries the list (`accepted`),
`settingrows.accepted_of` refuses a key advertising a word outside it and refuses the field on a
non-boolean row, the template loops over it in both grids instead of hardcoding a pair, and
`docs/PORTAL_SETTINGS.md` prints it beside the runtime's default.

**Teeth.** `test_a_switch_can_be_written_from_the_text_a_form_sends` (drop the coercion → fails on
every boolean key); `test_a_sampling_number_can_be_written_from_the_text_a_form_sends` (drop the
float branch → fails on all six floats, and asserts `temp = 20` is refused *as out of range*, not
accepted); `test_the_boolean_words_are_the_builds_own_words` reads the words back out of the b11429
help fixture — all four flags with a `--no-` counterpart state their default as `enabled`, and
`yes` appears nowhere in the help — so editing `TRUTHY` to include it fails at the build's text;
`test_a_key_cannot_advertise_a_word_the_preset_parser_reads_as_false` (hardcoding `yes` in the
registry raises); `test_a_switch_builds_its_options_from_the_row` (a hardcoded pair in the template
fails); `#ed-g-jinja-sel` value `on` in `tests/ui/spec.json` proves the loop renders. Suite: 853
passed, 3 skipped; UI harness exit 0.

**The 15th model, and one duplicate avoided.** `bottlecapai/ThinkingCap-Qwen3.8-27B-Q4_K_M`
(17.44 GiB) was fetched to a staging directory and then to the Hugging Face cache, where the other
14 live. Both files were compared — `sha256 fafa890c…` — and the staging copy deleted, so the
catalog has one id for one file (the router's `dedup-cache-models` compares paths, not bytes). The
index confirms the card's claim independently: `nextn_layers = 1` on this file, so `draft-mtp`
needs no second resident file; `has_fim = 1`; `ctx_trained = 262144`.

**Not measured: Q6_K.** The question "can this box run ThinkingCap at Q6_K" is answered above only
by arithmetic — Q4_K_M costs 34.39 GiB peak at its trained context, Q6_K is roughly a third more
weight bytes, and `VSCode` leaves 60.8 GiB spare — because the Q6_K file is not on this box. The
card's own scores for it are "2.8 pp lower on GPQA-Diamond, 1.7 pp higher on MMLU-Pro, 7.0 pp
lower on AA-LCR", i.e. it is not a strict upgrade. Measuring it means a ~23 GiB download and a
slot-bench run; say the word.

**Stated limits.** The deck is now 5 cards: 57,520 markup chars, of which 27,405 are bodies hidden
by default, with 10 nested `<details>` (the per-model context table and the `presets.ini` diff), all
closed. Nothing is running, and the deck says so. `Autocomplete-Qwopus3.5-MTP` writes
`ctx-size = 8192` with `parallel = 2`, so the card states a 4096-token window per client and names
the halving — writing `ctx-size` is what locks the fit estimate out for that model, which is the
page telling the truth about a setting the portal cannot re-price. And the whole set is inert until
the operator applies it: Apply is gated on confirm and on the machine being idle, and it restarts
the router.

##### 6.14.1i The editor shows the personality it is editing (2026-10-08)

**The complaint, verbatim:** "when a personality global or model level is selected to be edited,
the shown settings on the page should be updated to reflect the personality's current config."
Measured: it was not. Every field started empty, so the editor was proposing a configuration rather
than editing one -- and the payload it built was a set of edits, not a state. Nothing on the page
said what a field was about to overwrite.

**What the page is now.** `personality.settings_view(personality)` returns the two layers the editor
writes into: `global`, the personality's own `[*]`, and `models[<library id>]`, that model's section
exactly as `_layered_settings()` resolves it -- its model config, with any personality-level override
on top. Values are `_ini_value()` strings, so a bool arrives as `on` and not as Python's `True` (the
spelling §6.14.1g established for the control side, now established for the seeded side too). The
view rides on `#ed-persona option[data-config]` and on `/api/personalities` as `config_views`, so the
form, the read endpoint and the generator answer "what does this personality set?" from one place.

**The layering rule, stated in code and on the page.** A model's fields show what that section *sets*.
They do not show what it inherits. Seeding a per-model field with a value inherited from `[*]` would
write that value into the model's own section the next time the personality is saved, and the router's
precedence is CLI > model section > `[*]`, so the copy outranks `[*]` from then on -- one number set
in one place becomes sixteen copies an operator keeps in step by hand. `#ed-layers` says the
consequence in the operator's terms: an empty field means this layer does not set the key, and the
`default` on the row applies; it does not mean zero.

**The editor's state is a working copy, not a list of edits.** `payload()` sends the whole desired
section, so a field nobody touched is written as it already was, and the summary compares the copy
against the view: `no changes — these are VSCode's current settings: 2 value(s) in [*], 3 model
section(s) set`, and, when something differs, `proposed — [bottlecapai/…:Q4_K_M]: parallel=1 (was 2)`.
Two reset buttons return a grid to the personality's current settings and report how many values they
dropped. The old sentence -- "no edits proposed" -- was true of an editor that had lost the comparison
entirely, which is why the new one names the configuration it compared against.

**A bug the slice found in the page it was writing.** Both grids reuse one set of controls, so a
switch is a save plus a load, and the order matters: by the time a `change` handler runs, the select
already holds the *new* name. Capturing "what is in the fields" into the record the select names
therefore writes the outgoing configuration over the incoming one -- the fields you had just loaded,
emptied by the values you had just left. Symptoms: selecting `VSCode` showed an empty global grid and
a summary saying nothing had changed. `shown` and `captured` now name the record the shared inputs are
displaying, capture happens before the switch, and the guard asserts the *order* in the source, because
the symptom (an empty form next to "no changes") is exactly what a passing page looks like here.

**The refusal case keeps its facts.** A personality naming a model that is not on the box resolves to
nothing, so its view carries `error` naming the missing model, and keeps the `[*]` it does carry. An
empty form reads as "this personality sets nothing", which is a different -- and false -- claim.

**The harness grew two assertions this page needs.** A field's *value* is not text content, so
`expectText` cannot reach it: `expectValues` asserts `#ed-g-threads-val` is `8` and `#ed-m-ctx-size-val`
is `2048`. And a page seeded by a selection can only be measured with that record selected, so
`expectText` entries may carry `after_select: true` and are checked once the select cases have driven
the page. The sandbox now seeds a model (`ui-tiny-coder`), a model config on it (a float, an int, a
model-scoped key and a host-cache key) and a personality carrying two `[*]` keys, authored through the
portal's own endpoints -- a sandbox whose personalities set nothing can only ever assert the empty
case, and an empty field beside a correct label is indistinguishable from a filled one. The fixture
asks the index when its row exists before using it: the startup scan is a thread and `/healthz`
answers before it finishes, which was a fixture that passed most days and failed some.

**Teeth (7 mutations, each failing a named guard).** View built with no configs (config layer dropped)
and view returning raw Python values (no `_ini_value`) both fail
`test_the_editor_shows_the_settings_the_personality_already_carries`; copying `[*]` into every model
section fails `test_a_bare_model_shows_nothing_because_its_section_sets_nothing`; the option dropping
`data-config` fails that route guard and the source guard; the read endpoint dropping `config_views`
fails `test_the_api_carries_the_same_current_config_the_page_seeds_from`; the refusal branch dropping
`[*]` fails `test_a_personality_missing_a_model_shows_the_refusal_not_an_empty_form`; capture-after-
switch fails `test_a_switch_saves_the_fields_it_is_leaving`. Suite 865 passed / 3 skipped; UI gate 51
passed; deployed sha == HEAD, live page shows `VSCode` with 11 settings on ThinkingCap Q4_K_M, 10 on
the autocomplete, 2 on the embedder, and `Agentic-PI` raised as the running card. `presets.ini` was
not written by any of this (sha `9eb0c21d8075…` before and after) and the router was not touched.

**Known, not introduced.** The cross-module `AI_PORTAL_HOME` race still fails
`test_models_none_means_the_whole_library_for_a_custom_personality_too` when
`tests/test_personality_routes.py` and `tests/test_model_configs.py` run as a pair; verified identical
at HEAD with this slice stashed.

**Decisions recorded with the slice (operator, same day).** Model personalities stay **shared** global
objects -- `model_configs`, with the `alias` a client on the network types -- and step 3 becomes a
nested deck of collapsible model-personality cards, one per model the personality covers (the file has
one section per model, so `resolve_targets()` already refuses two configs on one model), opening the
same row component, with attach-existing / new-for-this-model / detach. A shared card states who else
uses it, because an edit to a shared object silently changes those personalities too. Names are
**immutable**: renaming means create-new + delete-old, because the alias is the endpoint, not a label.

##### 6.14.1j Step 1 creates, saves and deletes a personality (2026-10-08)

**The missing verbs, in the operator's words:** "create/edit/delete personality is not in the flow"
and, for the deck, "no create, delete buttons ... personality is not shown in edit mode". The write
endpoints have validated personality changes since phase N4 (`POST /api/personalities/save`,
`POST /api/personalities/delete`, `safe`/`default` refused by name), and the page called only
preview and apply -- so there was no way to produce a personality from the page, and no way to remove
one. This is the page slice §6.14.1f answer 2 queued as step 1.

**Step 1 owns the personality, not its settings.** Name, max resident, note, which models it covers,
and create / save / delete. The numbering is presentation and the split is the substance: step 1
writes `personalities/<name>.json` and never `presets.ini`, which is why these verbs are additive --
Preview has nothing to show for them, Apply is not involved, and the router is not restarted. That
boundary is asserted rather than asserted-about: `identityBody()` and `writeIdentity()` must not
contain the apply route.

**The model list is the two kinds a personality's `models` accepts.** A library model id (that file
under its own name, no settings of its own) and a stored model config (the same file under the name
the router routes to), the config printed with the model it renames, because the name is what a client
types and the base is what loads. It prefills from `settings_view.attach` -- the personality's own
list, verbatim -- and not from `describe()`, which reports resolved base models: a different fact, and
not one a form can send back. An empty selection is sent as `null`, which means the whole library, so
the sentence beside the list says that: an unchecked list otherwise reads as "covers nothing", and
`default` is exactly that state.

**The built-in pair is shown, listed and refused.** `safe` and `default` are not hidden -- they
describe the library as it is -- their attach lists render read-only, Save and Delete are locked, and
the reason is in the sentence: derived from the library, refused by name at the endpoints, so raise
one on the deck and create a new personality from its settings. `editable` arrives on the view, so the
form does not keep a second rule about the built-in pair next to the buttons that enforce it.

**Harness: a select case can now type (`then_fill`).** Client-side gating was previously untestable on
its enabled half. "Create is disabled while the name is empty" is easy to assert and proves nothing
about the rule; the rule is *unlocks on a usable name*, and a driver that cannot fill a text field
cannot reach that state at all. `Delete` is confirm-gated at the endpoint and the page asks first;
`requireConfirmFor` covers `/api/personalities/delete` so the denied pass proves no request left the
page without the confirm.

**Teeth.** The route tests drive the page's own payload shape: create a personality with a config and
a bare model in its list, read the stored file back (the typed text arrives coerced by the row's
declared type -- `4`, not `"4"`, which is why the editor seeds with `_ini_value()` spellings), check
the view prefills the same list, save it again with a different list and max, assert `presets.ini`
unchanged across the whole round trip, delete without a confirm (400) and with one (200), and assert
the personality is gone from the page. The built-in pair is refused at both endpoints, `editable` is
false on both views, `default`'s attach is `None` and `safe`'s is its one model. Suite **868 passed /
3 skipped**; UI gate **52 passed**; deployed sha == HEAD, live page carries every step-1 control and
the `__new__` mode option. `presets.ini` sha `9eb0c21d8075…` before and after; the router was not
restarted at any point, and is still serving `Agentic-PI`.

**The cross-module home race is closed.** `test_models_none_means_the_whole_library_for_a_custom_
personality_too` failed only when `tests/test_personality_routes.py` and `tests/test_model_configs.py`
ran as a pair: both modules set `AI_PORTAL_HOME` at import, and the gguf fixture wrote into the home
its own module captured while the index scanned the other. conftest now owns that isolation (commit
`8716352`), and the pair passes -- so the pair that was listed as known-failing in the last four plan
records is no longer known-failing.

##### 6.14.1k Step 3 is a deck of model personalities (2026-10-09)

**The idea, in the operator's words:** model-level settings for a personality are their own "model
personality", which can exist only inside a full personality, while a full personality need not have
any -- so the model-personality editor is the main personality editor with collapsible cards, and a
mechanic to create new ones. The question posed was shared versus embedded. Measured answer: **shared**,
which is what `model_configs` already is. A model config is `(name, base_model, settings)`, `render()`
writes it as the model's section plus `alias = <name>`, and the router routes by that alias (measured:
`POST /v1/chat/completions {"model":"tiny-chat"} → 200`), so a model personality's name is a client
endpoint on the network. `Autocomplete-Qwopus3.5-MTP` is used by `VSCode` and `Flash-VSCode` both:
embedded, the greedy-sampling block is duplicated in two files and they drift; shared, it is one
object and one place to fix.

**One card per model, because that is all the file can hold.** `presets.ini` has one section per model,
and `resolve_targets()` refuses two configs on the same model -- "which settings win" would be answered
by dictionary ordering. So the nested list is naturally one card per model the personality covers, not
an arbitrary pile of cards, and a deck with more cards than models would be a UI for a state the
generator cannot write. The card count is asserted against the index for exactly that reason.

**The card is the selector, and there is one grid.** The per-model fields move into the open card
rather than being cloned per model: two grids is two places where `parallel` can disagree, and the row
component (§6.14.1) is the thing being reused. Two consequences of that fell out of the UI gate. The
strip has to **move** `#ed-model`, not read it -- the sweep drives that select behind the page's back,
so a strip that only read it leaves the open card naming a different file from the selected model. And
a second click on the open card has to close it: a control that changes nothing is what the gate calls
dead, and the honest reading is that a card you have already opened should answer a second click.

**Shared, said out loud.** A card states the object it edits, how many settings it carries, and which
personalities use it -- and the sentence says that editing it changes those too, because that is the
danger of the shared choice and the operator is the one who has to weigh it. `used by` arrives from
the read endpoint rather than being worked out by the page: `POST /api/model-configs/delete` refuses a
config that a personality is still using (`'x' is assigned by y; remove it from those personalities
first`), and a page computing its own answer to that question would be a second, weaker version of the
answer the refusal depends on. The delete button is locked, with the users named, when the endpoint
would refuse.

**Names are immutable** (operator's decision, same day as the shared one): renaming means create-new
plus delete-old, because the alias is the endpoint a client types, not a label. The card's buttons are
therefore attach / create-with-these-settings / save-these-settings / detach / delete, and the delete
of a config in use is asserted as a refusal naming the users, with the config still on disk afterwards.

**Where the writes land, asserted.** Create, save and delete of a model personality write the portal's
own definitions; `presets.ini` is compared before and after the whole round trip and is unchanged, so
no card verb can restart the router -- Apply remains the only path to the file. Detaching is not a
request at all: it changes the personality's model list, which step 1's Save stores. The step-1 list
and the step-3 cards are one fact seen from two sides, so the card refresh reuses the checkbox state as
the single store -- two controls allowed to disagree about one list is the §6.14.1c deck bug arriving in
a new shape.

**Harness and suite.** Personalities page gained: cards visible and counted, the strip's text as a
shared-object statement, `requireConfirmFor` on `/api/model-configs/delete`, and the sweep driving
652 control interactions on the page with zero dead controls. Suite **873 passed / 3 skipped**; UI gate
**53 passed**.

**Known, not introduced.** `presets.ini` is untouched by this slice: sha `9eb0c21d8075…` before and
after, the router was not restarted, and `Agentic-PI` is what it is serving.

##### 6.14.1l The dashboard reports what the router is (2026-10-09)

**The measured reason.** `systemctl is-active ai-llama-router` is not the fact the operator needs.
Measured on this box: the unit is `active` with `MainPID=17541`, `--port 8080`,
`--models-preset /home/cortana/ai/config/llama/presets.ini`, `--models-max 1`, and one worker
(pid 17650, ppid 17541, port 42919, alias `ukisai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-GGUF:IQ3_XXS`,
RSS 11.6 GiB at the census run). A `llama-server` started by hand on 8080 leaves that unit reporting
`active` after `systemctl start` succeeds, while every client is talking to the hand-started process.
Nothing in the portal could see the difference, so the dashboard's three router buttons were acting on
a unit state with no account of the processes behind it.

**The census (`app/core/routerctl.py`).** Parentage is the definition, not a heuristic: the unit's
`MainPID` is the router process, its children are workers, everything else that runs `llama-server` is
outside the portal's control and is named as such. `MainPID=0` (systemd for "not running") is treated
as "no router process", which makes every server found read as foreign — the over-reporting direction,
and the one that keeps the page from claiming nothing is outside the unit when the unit is not running.
The port is parsed from `--port` and `--port=` only: `-p` is `--parallel`, and a census that read
`--parallel 2` as port 2 would invent collisions, so the assertion is on the spellings, not the intent.
Launch facts are read from the process's own command line (`--port`, `--models-preset`,
`--models-max`), which is what makes the preset comparison possible: `presets.ini` sha
`9eb0c21d8075…`, 2 sections, and the file the *running process* was launched with. A router launched
before an Apply is running the old file; "active" hides that, the launched-with line cannot.

**Degradation is a sentence, not a blank.** The probes fail alone: a `ps` that cannot be read yields
"the process list could not be read (…), this panel is not reporting 'no servers outside the unit' —
it is reporting that it did not look." A page that reads most-reassuring when it knows least is the
failure this section is about.

**The endpoint.** `GET /api/router/report` (auth-gated, 401 for a bare API call, not a redirect).
Read-only, asserted: `presets.ini` byte-identical across the call, no job queued.

**The panel.** Statements, not a table: unit and main pid; the router process with its port, preset
file and `--models-max`; workers with alias, port, RSS; the llama-server processes the portal does not
control, with the sentence that they are not started, stopped or restarted here; the port-clash
statement; presets sha/sections/launched-with; what `/v1/models` answers (16 registered, 1 loaded,
1 alias at the measurement). Read at page load and re-read by **check again** — deliberately not on
the 2.5 s pulse, because a `ps`, an HTTP probe and a file read are not second-scale numbers, and the
contract test asserts the census is absent from the poll function. The server-rendered sentences and
the JS-refresh sentences are word-for-word identical: the §6.14.1c rule (one fact, one renderer)
applies to a panel with two renderers by writing the same wording in both.

**The verbs consult it.** Starting is the verb the census informs, so it is gated by what the panel
shows: with llama-server processes outside the unit, the click confirms, names those processes, and
states that the start will not move what already holds those ports. Stop/restart keep their existing
confirm. In the harness, `requireConfirmFor` now covers `/api/services/llamacpp/start`, so a page that
started the router on a bare click is a blocked mutation with no dialog, which the suite fails on.

**Fixture, not a workaround.** Two seeds, both because a census has to be *in* a branch to be
asserted. The UI sandbox home had no `presets.ini`, so the panel could only ever be asserted in its
"the file could not be read" branch; the fixture writes one. And the sentence that matters most —
which `llama-server` processes the portal does not control — was only exercised if the machine running
the tests happened to have a router up, which is asserting the machine, not the feature. So the fixture
copies `/bin/sleep` to a binary named `llama-server` and runs it: `comm` is the executable's name, so
the census sees a server process and nothing else on the system does; the fixture owns it and kills it
in teardown. The spec asserts the branch sentences — no router process (the `systemctl` stand-in
reports `MainPID=0`), the stray named as outside the portal's control, no port held from outside, the
preset file nothing has loaded.

**Asserted.** `tests/test_routerctl.py` (9): parentage (worker is a child, stray is nobody's),
`--port=8080` recognised and `--parallel 2` not a port, `MainPID=0`, launched-with vs rendered file,
unreadable `ps`, report shape (every id on the panel has a key), endpoint shape, read-only, auth.
Contract test: the nine panel ids, the "did not look" sentence, the start gate consulting the census
with a confirm naming ports, no `innerHTML` in the census renderers, census off the pulse. Harness:
`/api/router/report` requested, eight panel ids visible, panel text asserted. Suite **883 passed /
3 skipped**; UI gate **54 passed**. Two mutation entries: start stops consulting the census, the
census stops naming the servers it does not control.

**Left alone deliberately.** The census cannot kill a stray server — it names it. Stopping a process
the portal does not own is not a portal verb, and the panel says so rather than offering a button that
would lie about what it controls.

##### 6.14.1m The pi harness runtime: research before code (2026-10-11)

The operator set the terms for the pi harness page: the launched session must behave **as close to a
terminal pi session as possible** — typing, models, extensions like the Q&A tool "without extra work" —
keep running when they disconnect, be reachable from any machine on the LAN, and after a host reboot
leave the session id on the Projects page so they can resume it. Their instruction was to research pi
and its packages *deeply* before we write a host, and to pivot if the honest answer is that it cannot
be done well. The evidence is in `docs/PI_HARNESS_RESEARCH.md`; the conclusions:

- **RPC mode is not the interface.** pi's `--mode rpc` is a clean JSONL control protocol, but
  `docs/rpc-extension-ui.md` states the degradations outright: `custom()` returns `undefined`,
  `onTerminalInput`/`setFooter`/`setHeader`/`addAutocompleteProvider`/`setEditorComponent` are no-ops,
  `getAllThemes()` returns `[]`, `setTheme()` errors, `setWidget` accepts only string arrays. An RPC
  portal UI is a second implementation of pi's interface that is permanently behind pi's feature set —
  exactly the "chasing problems" outcome the operator ruled out. RPC stays right for headless jobs.
- **Resume is already solved by pi.** Sessions are files under `~/.pi/agent/sessions/<encoded-cwd>/`
  shared by CLI, TUI and the browser UIs; `--session <id>`, `--session-id <id>`, `--continue`,
  `--resume`, `--fork` are in `pi --help`.
- **The property we need is a decoupled session daemon, and only one package has it.**
  PI WEB (`@jmfederico/pi-web`, MIT, 33 releases since 2026-05-09, Node ≥ 22.19 — box has v22.22.1)
  runs sessions in `pi-web-sessiond`, separate from its web/API server, joined over a Unix socket;
  its docs state "a browser disconnect should not stop an agent", that restarting the daemon interrupts
  sessions while "a web/API restart … is not a substitute", that open question forms survive web
  restarts while the daemon lives, and that projects live in `~/.pi-web/projects.json`. pi-web-ui
  (MIT, 196 releases, richest feature set: Git panel, subagents, vision bridge, plugins, `PI_WEB_TOKEN`)
  runs the SDK **in the web process**, so its own README promises "last run was interrupted" after a
  server shutdown. `@agegr/pi-web` shuts idle sessions down (`PI_WEB_IDLE_TIMEOUT_MS` default 600000)
  — a viewer, not a host.
- **So: no PTY host written by us, unless the spike says the terminal cannot meet the fidelity bar.**
  Both serious packages ship `node-pty` + xterm.js (`@xterm/xterm` 6, which is where the Kitty keyboard
  protocol landed — the `Shift+Enter` vs `Enter` distinction pi needs, per its own `docs/tmux.md`). The
  fallback design (a systemd user unit owning `pty.openpty` and a Unix-socket relay) stays in
  `docs/PORTAL_LAYOUT.md` with the spike gates that decide it.

> **Historical queue — items (15)–(26).** Read with §0.1. Statuses, merges and tombstones are
> recorded there: 15 is DONE, 16 is DONE-and-superseded, 19 is SUPERSEDED, 22 is REDUCED, 23/24/25
> merged into 35, 26 merged into 32. These numbers are not the canonical queue.

**Open questions this raised, kept in the queue.** (15) `projects_root` as a portal setting key. (16)
Spike PI WEB on this box — install with `--allow-scripts=node-pty`, `pi-web install`, `pi-web doctor`,
then: session alive after a web/API restart; interrupted and resumable by session id after a sessiond
restart; the URL/deep-link and per-project live-session endpoint the Projects page can read;
`Shift+Enter` inserts a newline in the browser terminal; the Q&A form opens with previews and survives
a reload. (17) The LAN access path — local CA and certificates for `portal.local`/`pihole.local`/
`pihole2.local`, nginx terminating TLS at a sub-path with WebSocket forwarding (the supported pattern
in both packages), the portal password gate plus a shared harness token; not required for
proof-of-concept, required before production, per the operator.
- (18) **Harness model binding, portal-side.** Generate `~/.pi/agent/models.json` from the router's
  registered models and give the built-in `llama.cpp` provider a non-empty stored credential, so a
  launched session binds `provider/model` from the census (which model is loaded) instead of the
  operator's `/login`-and-hope ritual. Measured shape: `POST /api/sessions/:id/model {cwd, provider,
  modelId}` → 200; runs then execute on the local router at zero cost. The portal already reads both
  halves (routerctl: registered models, loaded model, port).
- (19) **Terminal-tab key fidelity is an upstream gap, and the portal must say so.** `@xterm/xterm`
  6.0.0 (pinned by pi-web `^6.0.0`) ships no Kitty keyboard protocol support, so a terminal tab cannot
  tell `Shift+Enter` from `Enter` — measured: the keystroke executed the line. The portal's harness
  affordance is therefore the chat pane; any "open a terminal" link must be labelled with that
  boundary, not offered as equivalent to a desktop terminal.
- (20) **Playwright acceptance for the portal's own pages.** Playwright + Chromium headless-shell now
  runs on this box (measured: it drives PI WEB's Lit UI with open shadow roots). The portal's UI harness
  is jsdom, which proves controls, dialogs, verbs and requests and **cannot see layout, spacing,
  density or contrast** — exactly the complaints the design baseline exists to fix. A thin Playwright
  layer over the portal's pages turns `docs/PORTAL_UI.md`'s measured facts (type scale, row rhythm,
  panel gaps, contrast pairs) into assertions, and gives the design-review gates screenshots to reason
  from.
- (21) **The harness interface is ACP, and the portal implements it.** `app/core/agentd.py`: an ACP
  client (stdio, the stable transport) in the portal's own language, the session registry, the
  Projects page's two states, permission decisions rendered as confirms, a context meter from
  `usage_update`. Harness adapters: `pi-acp` for pi, `opencode acp` for opencode — both MIT, both
  measured here. The self-built PTY host is withdrawn: `@xterm/xterm` 6.0.0 has no Kitty keyboard
  protocol, so a browser terminal loses `Shift+Enter` (measured), and the protocol gives the portal
  more than a terminal does.
- (22) **Harness rows on Services**, beside the llama.cpp and ComfyUI update tools, per the
  operator's instruction: harness version, adapter version, unit, bound models, health, and their
  update verbs.
- (23) **The portal exposes MCP and skills, not a page to be scraped.** A portal MCP server (model
  inventory, personalities, router census, jobs, settings) plus `SKILL.md` skills for portal
  conventions, so any harness in `docs/HARNESS_LANDSCAPE_RESEARCH.md` operates on the portal by
  protocol — including the pi session this repo is developed in.
- (24) **Portal MCP server + memory tools.** `app/core/mcpserver.py`: portal inventory, router census,
  personalities, jobs, settings, project registry, plus `memory_put` / `memory_search` /
  `memory_recall` / `session_checkpoint`, stored in `portal.db` and shown on the pages. Lexical search
  with FTS5 first; embeddings through the router when `embeddinggemma` is loaded (measured: the router
  answers `/v1/embeddings` and says `model is not loaded` until we load it — and loading is the
  portal's job). opencode consumes it from generated config (`mcp`) or at runtime (`POST /mcp`); pi
  consumes it through the MCP adapter already installed.
- (25) **Skills that teach the handoff.** `SKILL.md` files for portal conventions (checkpoint format,
  when to recall before editing, the deploy paths), in the portable location — measured: skills in
  `~/.agents/skills` appear in opencode's `GET /command`, and pi loads them natively.
- (26) **Compaction ladder, in order.** Config (`compaction {auto, prune, reserved}`) → MCP
  `session_checkpoint` + skill → the `POST /session/:id/summarize` verb behind a context meter → and
  only if those are not enough, a thin plugin on `experimental.session.compacting`. No fork; a fork is
  pinned if it ever happens, never pruned.


#### 6.14.1m The portal generates the harness configs from the router (queue 18) — DONE

`app/core/agentconfig.py` (§6.14.1m, queue 18) writes the two files that bind a coding agent to the
portal's llama.cpp router, from the router itself: `~/.pi/agent/models.json` for pi's built-in
`llama.cpp` provider, and `~/.config/opencode/opencode.json` for opencode (`npm:
@ai-sdk/openai-compatible`, `baseURL: http://127.0.0.1:8080/v1`, a non-empty key, its own
`llamacpp` provider id, compaction with `prune: true`). The model list is the **union** of the
router's registered models and its currently loaded models — the state the operator described fighting
with `/models`, where a picker lists a model the server refuses.

The boundaries that make it safe to point a generator at files the operator also edits:

- **Merge, never replace** — an existing file keeps every key it has; our provider joins theirs, and a
  default `model` the operator chose is never moved.
- **A file we cannot parse is a hard stop.** pi allows comments in `models.json`; reproducing a file we
  cannot read is how a generator destroys a hand-written config, so an unparseable file is left alone
  and named in the result.
- **Nothing is written when the router has nothing to say** — an unreachable router or an empty model
  list refuses; an empty picker is worse than no config.
- **A changed file keeps one prior copy** (`*.portal-prev`); an unchanged file writes nothing, so
  pressing the button twice is a no-op.
- **`auth.json` is read for presence and never written** — the credential check answers `key_set`,
  never the value, and never the URL. That is the operator's blank-key failure, made visible.
- **Permissions are one of three named postures** (queue 23): `project` (edits allowed, arbitrary
  shell asks, leaving the project directory asks), `project-auto` (shell allowed inside the project,
  `external_directory` **denied** — full-auto is inside the project, not everywhere), `all`
  (everything, with `~/ai/state/portal/secrets/**` denied). `doom_loop` asks in every posture.

Endpoints: `GET /api/agents/status` (the two files' presence, model counts, base URL, posture,
auth-key presence — no secrets), `POST /api/agents/sync {port?, posture}` (409 with the refusal text
when the router has no models, the posture is unknown, or a file cannot be parsed). 17 tests in
`tests/test_agentconfig.py`; `app/core/routerctl.py` gained `router_models()` (registered ∪ loaded).

> **Historical queue notes — Queue 25 to 29.** These are section-local notes from the harness and
> memory research. §0.1 is the canonical queue: 25/23/24 → **35**, 26 → **32**, 27 → **37**,
> 28 → **36**, 29 → **32** with the host changed to opencode.

Queue **25** — the portal's own MCP server (`portalctl`), because the survey showed the two things pi
gets from extensions that opencode does **not** have built in are memory and the portal's own context,
and both are things only the portal can answer. Tools: `routerctl` (what is loaded, loaded now, loaded
from where), `inspect` (measured page facts: computed styles, gaps, contrast, screenshots — Playwright
is installed and measured working), `lint` (ruff/pyright in the project's own venv),
`layout_measure` (§6.14.1l's tool). Registered into opencode through `POST /mcp` and into pi's config,
so both harnesses get them.

Queue **26** — skills, not extensions: design-playbook's `SKILL.md` set is installed in
`~/.agents/skills/` (discovered by opencode, pi, Codex and Cursor; `name` must match the directory).
Keep new portal knowledge in skills and MCP tools, because those are the harness-agnostic halves.

Queue **27** — persistent memory (hermes-memory's equivalent). Researched and measured in
`docs/MEMORY_PLUGIN_RESEARCH.md`; the recommendation is: adopt **`@mathew-cf/opencode-memory`**
(Apache-2.0; git-tracked markdown store; hybrid keyword ✚ semantic search through `@mathew-cf/rag-cli`,
a 36 MB native binary with a local quantized ONNX MiniLM — measured indexing this repo's 2,121 chunks
in 11.8 s with no cloud and no router call; knowledge bases over source repositories for file-content
retrieval; `session_search` across OpenCode, **Pi** and Codex; hooks that nudge search-first and remind
the agent to write a retrospective **at compaction time**; skill dropped in `~/.agents/skills/` where pi
looks too), and copy **`@cioffi_ai/opencode-memory`**'s *behaviour* into the portal's own conventions
(tiers core/archival/temporary/pinned, per-fact provenance to the source session and messages, a
contradiction lifecycle that flags instead of overwriting, `memory_useful`/`memory_irrelevant` retrieval
feedback, typed options that fail setup on an unknown name). Rejected on measured facts: `ainative`
(paid cloud, API key), `@staticeng` (UNLICENSED), `opencode-memory-pro` (`NOASSERTION` licence, 32
releases in four days then silence), `claude-mem` (compression runs through Claude's Agent SDK in the
cloud; documented token-burn issue #618; wants Bun **and** `uv` for Chroma; AGPL). Deferred:
**OpenViking** — the one with a research pedigree (VikingMem, VLDB 2026; memory as a `viking://`
filesystem with L0/L1/L2 summary tiers and a per-query browsing trajectory you can trace), but it is a
second server (Python), AGPL-3.0 network copyleft, front-loaded write-time LLM cost, 598 open issues
with same-day churn, and it needs a resident embedding ✚ VLM — which this box cannot hand it while the
router runs `max_instances: 1` (measured: `/props`, and `POST /v1/embeddings` → `400 model '…' not
found` for registered-but-unloaded ids). Gate list for adoption is in that doc.

Queue **28** — `tools/layout_measure.py` becomes a **broadly usable layout-inspection tool** the agent
can call: gaps, padding, density, overlap, off-scale tokens, contrast pairs, and screenshots, as
measured facts about a rendered page (Playwright + Chromium installed and measured working here). The
operator's stated preference is a **plugin** over a service (more direct), with an MCP server as an
acceptable fallback if the plugin path is not viable; either way the bar is a robust tool: input
validation on the URL/viewport/selectors, error trapping and recovery, and output that is legible to
the agent **including error states, connection failures especially**. Preferred shape: an opencode
plugin (`@opencode-ai/plugin` tool) whose implementation is a portal CLI the harness calls, so the
same binary serves the portal's own `check-ui` job, the plugin, and MCP. **Not now** — the Projects
page (queue 18's second half) comes first; this is the "do not forget it" note, with the requirements
attached.

Queue **29** *(2026-10-12: this is canonical queue **32**. The requirement below is unchanged; the host
is opencode, not pi-web — see §0.1's decision record. The pi-web measurements quoted here are the
fallback evidence, not the plan.)* — §6.14.1m end to end: **the pi harness page and the session host that
survives a portal restart.** The Projects page (queue 18, §6.14.1n) is built, and queue 33 gave it a
**launch verb of its own** (§6.14.1q): the row's Launch creates the session inside the project and
records the pair on the registry row, so this queue's remaining work is the session *view*, not the
launch. What it needs: the session host as its own supervised unit (not a child of the portal
service), the per-project live-session read with the session id a page can deep-link to, model binding
from the census (queue 18's generator already emits the ids), the two page states (inactive = projects,
operational = the session and nothing else), and `/quit` returning the page to the inactive state with
the session id left visible so a rebooted portal can point at a finished/abandoned session. Measured
support for the hard half already exists in `@jmfederico/pi-web` (queue 16): the session daemon is a
separate systemd user unit, sessions survive restarting the web server, and `GET /api/sessions?cwd=`
answers the running list — what is missing is the portal's side of it.

#### 6.14.1n Projects: the registry, the init shortlist, and the projects root — DONE

The operator's design for the pi harness page has two states, and this is the first one: **inactive**,
where projects are created, adopted, forgotten and chosen; and **operational**, where a running session
is all the page shows. The inactive state has to be a real folder list before the operational state
means anything, because a session launches *in* a project, at a path, with a model.

What is built (commits `bc195e3` and the page commit that follows it):

- **The name rule is the security boundary.** A project name becomes a directory. Lowercase letters,
  digits and single hyphens, 2 to 64 characters, starting and ending with a letter or digit. Every
  refusal quotes the same sentence, so the page can show the rule the operator can type to; `app--1`
  is refused because it is a typo of a name, not a name. The page states the rule where the name is
  typed, and a contract test pins the page's sentence to the server's (`test_the_projects_page_promises_the_name_rule_the_server_enforces`),
  so a page cannot promise a name the endpoint refuses.
- **Toolchains are measured, not assumed.** `uv, npm, node, git, cmake, g++, cargo, go, deno` are
  probed with `shutil.which` plus the version line, and an absent tool carries the install statement.
  The plan splits the ticked boxes into *what will run* and *what is refused, by name with the reason*;
  a missing toolchain never aborts the boxes that could run. (Mutation check: a probe that reports a
  missing tool as present was MISSED by the first test set — the plan tests feed the census themselves,
  so a lying probe passed every one. The test that catches it feeds the probe the real box.)
- **The init shortlist is seven constants** — `git init`, `uv venv` + pyproject, npm + `.nvmrc` +
  tsconfig, CMake + `build/`, `README.md`, `.gitignore`, `AGENTS.md` — each with its needs, dirs,
  files and commands, in a fixed order. It is not a place to type commands.
- **Bounded execution.** List argv only, never a shell string; cwd pinned to the project; stdin
  closed; a per-command timeout; git non-interactive; output bounded; environment copied, not
  extended. The first bug this measured was real: `git commit` dying with *"Please tell me who you
  are"* on a box with no git identity, so the first commit is made with an explicit identity and a
  message naming the project.
- **A half-built project is a reported fact.** The folder stays, the failing step is named with its
  command and exit code, the steps after it do not run, and the answer says nothing was deleted. The
  endpoint answers 409 with that sentence; the page shows it (mutation check: "a half-created project
  is reported as a success" is caught at both the core and endpoint level).
- **Registry plus scan.** A folder made by hand, by `git clone`, or by an agent is discovered;
  adopting writes the registry only. Adoption refuses a path outside the projects root and the root
  itself. **Forgetting deletes nothing on disk** and requires `confirm`.
- **`projects_root` is a portal setting** (queue 18): validated (absolute, no NUL, bounded traversal,
  creatable and writable, with the mkdir-ability answer computed before anything is created), stored
  in the settings table, and read by every path operation. `_safe_join` refuses any name that resolves
  outside a project — the shortlist's names are constants today, and the check exists because the next
  shortlist will not be.

The page (`/projects`, nav-linked): the root line (root, registered count, folders the registry does
not know, root health), the New project card (name + the seven boxes + Plan + Create, with the plan's
refusals and the install statements), the registry rows (facts, Launch **labelled inert** with the
sentence naming §6.14.1m as the page that owns that verb, Forget behind a confirm that says the folder
is not touched), and the unadopted list with Adopt. The page reads everything from
`GET /api/projects`; the row verbs and the plan are endpoints (D1).

**The safety finding worth keeping.** The UI sandbox sets `AI_PORTAL_HOME`, which moves the portal's
`~/ai` tree — and does **not** move `~`. The projects root is a `~` path by default, so the first
Projects-page UI run rendered (and could have written to) the operator's real `~/ai-projects`: the
page's own summary line printed `/home/cortana/ai-projects`, and the sweep clicks every control,
including Create. Fixed by setting `HOME` to the sandbox home in both UI sandboxes, and the fixture
now seeds its own projects root. The rule to remember: **a sandbox that redirects the portal's home
must redirect `~` too**, because everything the portal resolves with `expanduser` follows `~`.

Mutation checks for the page — 9, all caught, three of them only after the assertions were fixed:
forgetting without asking (the spec had not named `/api/projects/forget` in `requireConfirmFor`, so
the sweep's confirm gate was not enforced at all); Launch offered as a live button (`data-inert="yes"`
was satisfied by the page's **CSS** — an assertion on markup text proves nothing about the control the
page builds, so it is asserted on the line that builds it); the shortlist offering boxes whose
toolchain this box does not have (a stubbed `missing = []` kept every other word of the function
intact, so availability must be asserted on the line that decides it); a half-built project reported
as created; a swallowed create refusal; the card rendered without its facts; a swallowed adoption
refusal (source-level: the harness blocks mutation requests, so the refusal branch never runs under
jsdom — a **boundary** of the UI harness, recorded here); the inert control not naming its owner page.

Harness changes this slice needed, both general:
- `tests/ui/run.mjs` tolerates a **nav link to the page you are already on** (`noopSelfLinks`,
  declared per page), matched on `href` only — a link to a *different* page that changes nothing is
  the dead link this check exists to catch, and stays loud.
- The UI fixtures redirect `HOME`, and seed a projects root containing a folder whose *name* is
  markup (`<img src=x onerror=alert(1)>`): the scan lists folders the portal did not author, so a
  stored-XSS payload arrives here by `git clone`.

**Not built, and where it goes:** launching a session (§6.14.1m) — the harness page, its two states,
the session host that survives a portal restart, the per-project live-session read, and the deep link.
Queue 18's first half (model binding) is done: `app/core/agentconfig.py` generates both harness
configs from the census. Queue **29** is §6.14.1m end to end; the Projects page's Launch button is the
handoff point, and is labelled as such on the page.

**Added on the operator's request (2026-10-12), after the project moved to GitHub.** The create card
now carries **opening text** for `README.md` and `AGENTS.md` and a **git remote** with a pull-first
option. The measurements that shaped it:

- Supplying text implies the box: text for `README.md` writes the file even when the box is not
  ticked, and a remote implies the git box. A field the operator filled is an instruction, not a flag.
- A remote is validated **before git sees it**. `ext::` is refused by name — it is git's own "run this
  command to reach the remote" transport — and a URL carrying a password is refused, because git
  stores it in the project's `.git/config` in the clear, where the project's own code can read it.
  `https://`, `http://`, `ssh://`, `git+ssh://`, `git://`, `file://` and the scp-like
  `user@host:path` are accepted.
- The remote's default branch is **read from the remote** (`git ls-remote --symref … HEAD`), not
  assumed: on this box `git init` defaults to `master` while GitHub's is `main`, so a fixed branch
  name fails half the time. The symref line is `ref: refs/heads/main\tHEAD` — the branch is the
  first token, not the tail.
- **Pull first means before the init files are written.** The file loop already leaves an existing
  file alone, so a repository's own `README.md` survives and the portal's templates fill only the
  gaps — the merge rule a fresh clone needs, with no merge logic of our own.
- A remote that does not answer is a **warning, not a refusal**: `origin` is not configured, the
  folder and its files stay, and the project is registered. Measured with `file:///no/such/repo.git`.

Tests: `tests/test_projects.py` (14 added: text, the remote shapes, the pull order against a real
local repository, the dead-remote path, the endpoint carrying all four fields) and
`tests/test_ui_contract.py` / `tests/ui/spec.json` for the fields, the payload and the copy.

##### 6.14.1o The init shortlist's consequences, and the host's cache home (2026-10-13)

Two things the operator asked for, one on the page and one under it.

**The seed is in place before the host starts.** A session created over the harness API reads
`opencode.json` and the `AGENTS.md` files out of the host's cache home
(`~/.cache/ai-portal/harness/<port>/cache`), and a host started with an empty one answers from
defaults — no posture, no compaction, none of the plugins. `seed_cache_home(port)` copies the
portal-generated config there **before** the unit starts, refuses to start a host it cannot seed
(the host has no config to run with — sync the harness configs first), and reports the files it
carried, because the page shows a file list. `status()` answers the same facts from the seed on
disk, with `seeded: false` when there is none, and the harness page shows the seeded state.

**`.gitignore` is a consequence, not a box.** The operator asked
for it directly: *checking "git repository" should produce `.gitignore`, filled in from the stacks
that were picked, and checking the other boxes should fill it further.* The box was removed from the
shortlist — seven boxes became six — and the file is now **derived** by `compose_gitignore(steps)`:
a base every project gets (`.DS_Store`, `*.log`, `.env`, `.env.local`, `.scratch/`) plus one labelled
section per stack (`Python` → `__pycache__/`, `.venv/`; `Node` → `node_modules/`, the lockfile
variants; `C++` → `build/`, `CMakeCache.txt`, `compile_commands.json`).

The rule generalises three ways, and all three are tested:

- **Written even when the git box is not ticked.** A folder with no `.gitignore` is a folder that
  will get a repository later; the file is derived, costs nothing, and the operator should not have
  to remember to ask for it.
- **Written after a pull.** `git clone` carries no ignores the portal can rely on, and a project
  whose first commit includes `node_modules` is the exact failure the file exists to prevent. The
  pull lands first, the derived file goes in after, and a repository that has its own keeps it —
  the same "leave the file that was already there" rule that governs the templates.
- **Never overwriting what the operator wrote.** The derivation is additive. A portal that replaced
  the operator's ignores would be the portal knowing better than the project.
- **Not written into a failed init.** The file is a consequence of the boxes that *ran*. A folder
  whose init failed is reported as failed, and leaving behind a derived file the operator never
  ticked would make the half-built folder look more finished than it is.

`init_of(path)` reads the same facts back out of the registry, so an **adopted** folder composes the
ignores its recorded steps imply, and a folder the registry does not know composes the base only —
this page does not invent facts about folders it has not seen.

The page states the rule where the operator reads it: the sentence beside the boxes
(`#proj-gitignore`, "`.gitignore` is not a box: it is written with the boxes you picked"), the plan
(a `writes: .gitignore — with the python, node sections` line, so the file is visible before the
folder exists), and the create result (`… .gitignore written`, or `.gitignore left as the project had
it`). A file the operator never ticked and never sees named is a file they meet for the first time
in `git status` and do not recognise.

Tests: `tests/test_projects.py` (6 added: the shortlist no longer offers the box, the base alone,
the sections from the ticked stacks with C++ absent when C++ was not ticked, the file written
without the git box, the operator's file left byte-for-byte, a pulled project getting the ignores,
and `init_of` for adopted folders) and `tests/test_ui_contract.py` / `tests/ui/spec.json` for the
sentence, its id, the plan line, and the result. **The spec's box count dropped from 7 to 6, with
the reason in its comment**: a count of 7 means a box came back. A later test added the fourth
generalisation the rule needs — **a failed init does not leave the file behind** — and caught this
slice's own implementation doing it.

**Which projects are being served (queue 33's connection, 2026-10-13).** The queue 33 brief states
the personality↔project link in both directions: the harness launches a project *with a
personality*, and the personality editor names the projects that use it. The second half needs a
fact the portal can measure and could not store. `serve_facts()` answers it — the registry plus
**one** host call shared by every row (per-project asks turn a page refresh into N requests to a
single user unit, and a host that dies mid-refresh answers some rows and not others) — where a
project is served when the host has a live session for its folder and the registry names the
session and personality the launch wrote. `mark_served` takes a name **or** a path (the launcher
knows a path, the page knows a name; a verb that took one would be used two ways), resolved
through `project_path`. A host that did not answer is `unknown` for every project, never "not
served" — asked even with an empty registry, because only the host tells those two answers apart.
The page's rows carry it (`is being served — session ses_x with <personality>`,
`serving state unknown: …` in red, `not served` muted), read from the payload, not from the page's
memory of what it launched.

**The finding worth keeping:** `_live_sessions` ended up defined twice in the module during this
slice, and the second definition silently shadowed the first — two tests then failed in a way that
looked like the *payload's shape* was wrong. A duplicate top-level `def` is the quietest version of
that bug: no error, no warning, the later body wins. The fix is the single definition; the rule is
to read the failing assertion against the payload's actual keys before assuming the shape is wrong.

##### 6.14.1p The dashboard's vitals strip, and thresholds the operator owns (queue 40 + 41, 2026-10-13)

The operator's complaint, measured: the hero carried a **CPU line chart polled constantly**, on a
box where AI work pegs the GPU and leaves CPU idle — **CPU 3.9% while VRAM read 97% and the GTT pool
53%**. A chart of the number that never moves is the instrument that hides the one that does. The
requirement, in their words: *critical metrics visible, occasionally updated, lighting up when
things get dangerous.*

**`app/vitals.py` is a pure function of a metrics snapshot and the router's resident workers**, and
that purity is the reason the thresholds are testable at all: a strip computed inside the dashboard's
poll would have to be tested on the machine it runs on. Four tiles in the operator's order —
**pool, VRAM, RAM, disk** — each `ok` / `watch` / `danger` / `unknown`, a sample stamp on every tile
("occasionally updated" without a stamp is a number the operator cannot age), and the line naming
*who* holds the pool: **`llama-3.1-70b in slot 8080 holds 77.9 G`**, assembled from the census the
portal already has rather than from a new probe.

Three decisions in the shape:

- **The pool carries the danger; VRAM is stated.** On this iGPU the VRAM figure is the driver's
  ~512 MiB stub and reads ~97% *by design*. A tile that shouted there shouted every day and trained
  the operator to ignore the strip, so near-full VRAM on an iGPU follows the pool's rung and says
  why it looks full.
- **The worst filesystem wins.** `/` and `~/ai` are separate here; a tile reporting the root's
  number would call a full `~/ai` `ok`.
- **Four states, not three.** "Not read" is never `ok`. The health pane has been bitten by this
  twice — a missing amdgpu sysfs node, a disk probe that raised, a router that did not answer — and
  in every one of those the box looked healthy.

**Queue 41 in the same slice: the thresholds are portal settings.** Registered in `PORTAL_SETTINGS`
with a numeric control, defaults read **from `vitals.DEFAULTS` rather than restated** (a default in
two files is two numbers that will disagree), validated at the same gate the projects root uses, and
`pool_danger` below `pool_watch` is refused — the one configuration that makes the strip light up in
the wrong order and still look authoritative. A stored value the reader cannot use is **reported on
the strip** (`thresholds_error`), never silently replaced by defaults: an operator edit that did
nothing, invisibly, is worse than a refusal.

**The findings worth keeping.**

1. **A boundary test through `int()` truncation tests the truncation, not the threshold.** My first
   version drove `pool_used` as `int(total * 69.9 / 100)`, which landed below the boundary the
   assertion claimed it was at; the assertion then failed for a reason that looked like the
   threshold being wrong. Keep percentages in floats at the boundary.
2. **A pair rule must be checked from both sides.** Saving `pool_danger` alone has to look back at
   `pool_watch`. My first derivation walked the ladder downwards only, and so silently skipped
   exactly the half of the page that saves the lower rung — the check reported `ok` for a ladder
   that was upside down.
3. **A read path may not assume the app created the tables.** Reading a setting was the first thing
   the dashboard did on a fresh box; `db.init()` is ensured in the accessor. Tests caught it, the
   dashboard would not have.
4. **My own test asserted the wrong semantics.** I asked 69.9% of the pool to be `watch`, which is a
   strip that lights up at *any* value under the danger line. `watch` is the rung *between* watch and
   danger; below the watch value is `ok`, and disk has no watch rung at all. The test was fixed
   against the requirement, not the implementation.

Tests: `tests/test_vitals.py` (11 — the four tiles in order, the stamp, the iGPU asymmetry, the
thresholds at every boundary on both sides, "not read" is not `ok`, the worst filesystem, who holds
the pool with a census that stayed silent, the settings keys and defaults, refused thresholds,
partial sets, and the strip carrying no CPU tile at any CPU value), `tests/test_portalsettings.py`
(6 — the thresholds registered as portal settings, the page's defaults equal to the reader's, a
stored threshold replacing only itself, refusals at the gate before Save, the ladder refused, and a
threshold the strip would ignore being *said* by the strip).

##### 6.14.1q The launch records the pair — project ⇄ personality (queue 33, 2026-10-13)

Queue 33's brief states the link in **both directions**: a launched project records the personality
it runs with, and the personality carries the projects running it. The projects page (§6.14.1n) could
already answer *"is this project being served?"* — it read the host's live sessions by directory. What
it could not answer was **with what**, because the personality was known only to the browser that
clicked the button.

**The launch is the record site**, and the reason is positional, not stylistic: the session id the host
returned, the project folder, and the operator's personality choice all exist together at exactly one
call site. Everywhere else, one of the three is already gone by the time anyone asks. So
`harnessctl.launch_project()` creates the session **inside the project directory** and records the pair
on the registry row in the same verb, and `projects.py` keeps the record it already wrote (the
`notes.serve` stamp) readable in both directions: `serve_facts()` per project, `by_personality()` per
personality, and the inverse view the personality editor needs is derived from the same records.

The directory is the load-bearing half. `create_session` puts the folder in the **query parameter**
(queue 30's measurement: a body key named `directory` is ignored and the session lands in the
server's own cwd), and `serve_facts` finds a served project by matching live sessions on that
directory. A session created anywhere else records a serving fact the host cannot confirm — the page
would show the project as served and the read would find nothing.

**Refusals are said, not dropped.** A project that is not in the registry has no row to record on: the
session is created anyway (the operator asked for it) and the answer carries `recorded: false` with the
reason, because a page that launched a session it cannot list has to say why the projects page will not
show it.

**The page's side.** Launch is no longer an inert label pointing at the harness page — it acts:
`POST /api/projects/launch` with the name, the personality from a `<select>` populated from the
payload, and `confirm`. Two properties the harness checks:

- the button is disabled by **the payload's `launch.available`**, which is the host's own state read
  through `harnessctl.status()`. A button disabled by a literal is a button that lies once the host
  starts; one disabled by the payload flips when the host does. In the UI sandbox the host never runs,
  so the spec supplies the host's answer as a fixture and the sweep drives the real button.
- **a shadowed host is not launchable** (queue 30's measurement, applied to queue 33's record): when
  the unit is not the listener, every session on the port belongs to a process the portal does not
  supervise, so the pair recorded there describes a session the portal cannot list and the row would
  read as served. `launch_facts()` answers `available: false` with the harness page's own sentence as
  `reason`, `launch_project()` refuses the same way before it creates a session, and the page puts the
  reason in the disabled button's own words. `projects.py`, `main.py` and `harnessctl.py` agree on the
  shadow because all three read it from one `status()`.
- the personality list is read from `summary.personalities`, which is `personality.all_personalities()`
  — the personalities component's own names. A list assembled in `projects.py` from a file, a
  directory, or a literal drifts from the names the personalities page stores, and the row ends up
  recording a personality that no longer exists.

**The findings worth keeping.**

1. **`"served"` collides with the vocabulary of its own payload.** My first row key carried the bool,
   and the payload already has a top-level `served` block (the host's census) whose value is a *list of
   names*. A name, a bool, and a record of a live session are three things; the block is `served`, the
   record on a row is `serve`, and `served: true` inside it means the host answered **and** confirmed
   this folder — `null` with `unknown: true` is the host not answering, which is not "not served".
2. **A test that asserts a call list blocks the honest implementation.** My first pair test asserted
   which methods `launch_project` calls. Any of three correct designs (stamp on the row, a session
   field, a log line) satisfies the requirement; the test was rewritten as a **shape** assertion —
   `serve_facts()` reports the project served, with the personality, after the launch.
3. **The fake must hook the function the module has.** My ordering test hooked a `start_unit` variant
   that does not exist and silently never fired; the test passed while proving nothing. Hook the real
   call site, and assert the fact the *page* reads, not the path the code took.
4. **An assertion satisfied by a stylesheet proves nothing about the control.** `data-inert="yes"`
   appears in this page's CSS *and* in its JS. Assert on the line that builds the button.
5. **`launch.dataset.inert = 'yes'` was a contract test, and queue 33 changed the contract.** The
   "inert Launch" tests (§6.14.1m's placeholder) are now the "Launch is disabled by the host's answer"
   tests, in both the contract file and the UI spec (`expectInert` for the page is empty). Deleting an
   obsolete assertion is part of the slice; leaving it makes the suite resist the feature.
6. **A verb that outlives the portal asks for a confirmation, in the page's own words.** Launch opens
   a session on a host that survives a portal restart, so the dialog names **both halves of the pair**
   it is about to record — which project, which personality. The endpoint's `confirm` field and the
   sweep's confirm gate are not the same thing: the field is the page's declaration, the gate is the
   harness's check that a dialog happened first.
7. **Check the host *before* the session exists, not after.** A session opened on a host that is not
   answering (starting, restarting, shadowed) is a session the portal records as serving a project and
   cannot list afterwards. The gate is the same fact the payload's `launch.available` is built from, so
   a click that got past a disabled button is refused by the verb that performs it.

**Left deliberately for the harness page (§6.14.1m, queue 29/32).** The session *view*: deep-linking a
session id, model binding from the census, and `/quit` returning to the inactive state with the id
visible. The projects page records the pair and shows it; the operational state is that page's.

Tests: `tests/test_projects.py` (the serving record read back as facts, `by_personality` from the
records the launches wrote, the personalities offered being the component's names, the launch verb
stated with the host's state), `tests/test_harnessctl.py` (the launch opens the session inside the
project it records as serving; the pair recorded; a launch that cannot record says so),
`tests/test_ui_contract.py` (the launch wired to the endpoint that records the pair, the personality
`<select>` refreshed from the payload, the row showing `st.personality`, the summary sentence
distinguishing recorded from not-recorded, the button's state read off the host),
`tests/ui/spec.json` (the projects page drives the live Launch: the sweep posts to
`/api/projects/launch` **behind the page's own confirm**, and the payload's host answer is supplied by
fixture because the sandbox host never runs), plus the shadow test in `tests/test_projects.py`
(a shadowed host answers `available: false` with the harness page's sentence, and no session is
created on it).

##### 6.14.1q page slices — the reference table, the `used by` line, and the Now strip (2026-10-13)

The four states were approved as the shape of this page; these are the three slices that moved the
page toward it, in the order they landed.

**Reference out of the page (state 4: Reference in Help).** The settings reference table — the key
table the editor had carried since §6.14.1i — left the page for `docs/PORTAL_SETTINGS.md`, the
document the Help page already renders. A page that carries the reference and the editor carries two
things that drift: the table the editor prints and the document an operator reads can disagree about
what a key means, and nothing detects it. The page keeps a citation link into the document's own
anchor. The guards that protected the table in `tests/test_personality_routes.py` moved **with the
content** to the document that now carries it — a guard left behind on removed content is a test that
protects nothing and blocks the next change.

**The editor's `used by` line (the pair, from the personality side).** §6.14.1q records the pair on
the registry row; the projects page shows which personality a project runs with. This is the inverse
direction, on the surface where a personality is *changed*: a personality edited without knowing a
project runs with it is edited blind, because the change lands on that project's next launch.
`/api/personalities` and the editor element carry `used_by` =
`projects.by_personality(projects.serve_facts())` — the records the launches wrote, not a page-side
inference. The page knows personalities and models; it does not know sessions, and a page that
assembled "used by" from its own data would be inventing the host's fact. The line is painted in
`syncIdentity`, so it moves with every other step-1 fact (a line computed once at load describes
whichever personality was selected then), and in the host-did-not-answer state it prints the payload's
reason: **an empty line there is the page claiming it checked.**

**The Now strip (state 1).** The deck answers *which* personality; the brief asks for the surface that
answers *what the router is doing* — the running personality, the model it is serving, the slot it
answers on, and the verb that stops it. Two facts, two sources, kept separate:

- which personality is active is presets.ini's fact (its file is the file Apply would write,
  §6.14.1l), **passed into** `personality.now_state()` from the deck loop, so the page states it once;
- the model and slot are the router's own account (`GET /v1/models`, the port from its env file).
  Neither is knowable from the library: a listed model may not be loaded, and a port in a template is
  not a port the router bound. Unreachable router → the strip names the reason and **never prints a
  slot it did not receive**.

The strip's stop verb goes through `POST /api/services/llamacpp/stop` — confirm-gated, refused while
an update is mid-swap — and lives **outside the personality-editor functions**: stopping the router is
not a fact about the personality being edited.

**Findings worth keeping.**

1. **A verb belongs to the function boundary its tests read.** My first stop handler sat between
   `applyIt`'s closing brace and the next `addEventListener`, inside the function the card tests
   extract by name — a router-stop verb appeared inside the personality editor's function, and
   `assert "postConfig" not in detach` failed. Where a control's handler lives is a statement about
   what the control is for.
2. **Name the answer's key where the answer is defined.** My page-half test asserted `host_ok`;
   the view is defined with `hostOk`. A test written in the page's file that renames the component's
   own key invents that component's shape — assert the key pair the view is defined by.
3. **A `\` in a heredoc is a literal backslash.** It survived into a JS string in the page and
   broke the sweep. Write page JS with a script that writes the file, not a shell heredoc.
4. **A disabled control's reason must be visible text, not a tooltip.** The `reason` a disabled
   Launch carries was in the button's `title`: unobservable to the UI harness and invisible on a touch
   device. `expectText` on `#proj-launch` is the assertion that makes it observable.

Tests: `tests/test_personality_routes.py` (the reference table is the document's, the citation anchor
resolves, `used_by` on the payload with the inverse view's own keys, the Now strip's slot and model
coming from the router), `tests/test_ui_contract.py` (the line wired to the payload and repainted on
personality switch, the strip above the deck carrying its stop verb, the reason as visible text),
`tests/ui/spec.json` (`#ed-used-by` says "cannot confirm" and `#pers-now` says "nothing is active" in
the sandbox — the states the page cannot invent and must carry).

##### 6.14.1r The page read by default — the editor opened by a verb, as steps (queue 33 states 2 and 3, 2026-10-13)

The defect this slice answers is numeric, not aesthetic. `tools/layout_measure.py` on the deployed
code: `/personalities` **6,430 px in three blocks**, tallest `2 Global ([*])` at **5,335 px** laptop /
**10,295 px** phone, while the deck above it is 484 px. The page opens on a wall of fields, which is
*Editing* on arrival. The approved shape reads by default and **opens editing by a verb**.

**State 3.** The editor is collapsed by default; *Open the settings editor* is the verb. The state is
an **attribute on the editor element** (`data-ed-view`, `data-ed-step`) — the page's own deck idiom
(`data-raised`) — and CSS paints the visible step from it. Opening is a navigation, not a save: Save
stays at the editor's foot, so "open" cannot be read as a mutation, and *Close the editor* never
implies a save.

**State 2, inside the editor.** Four steps in the page's own idiom: each carries the step state
(`data-ed-step` / `data-current`) **and the step's name**, so "which one am I on" is text an operator
reads, not a number to infer. A collapsed editor is a reading surface, not a form: its row names the
personality, the values it sets, and which projects run it — so *Read by default* holds even with no
personality selected.

**The markup is composed in one place.** The row markup is built in one function with one data source
and two renderings (collapsed row, open editor). Two hand-written copies of the same facts are two
things that drift.

**Findings worth keeping.**

1. **A gate exemption is a last resort; prefer the state the gate is describing.** The stepper's
   current-step button moved you to the step you were standing on — no DOM change, so the UI gate
   called it dead. The fix was not `expectInert` (the page arguing with the gate) but stating the
   current step as text: a `<b>`, not a verb. A control that changes nothing is not a control.
2. **The sandbox is seeded by the pytest fixture, not by `Sandbox` itself.** A bare `Sandbox` run
   renders unseeded states, so a page that is green under pytest is red under the bare sandbox. This
   is why the personalities page's 19 failures vanished once the fixture seeded the model index: they
   were all the unseeded page (empty index, `UI-Test-Work` absent, presets absent, no router),
   not defects. Verify attribution — check which fixtures a failing assertion needs — before treating
   a sandbox red as a page defect.
3. **A sandbox cannot assert a live router.** A live router's state is not knowable in a sandbox, so
   the spec cannot require a strip that names one. A *control* can always assert its reason: the
   strip's stop verb starts disabled with its reason as visible text, asserted in the sandbox
   (queue 31 finding 4 — the reason is text, never a tooltip).

Tests: `tests/test_ui_contract.py` (collapsed by default and opened by a verb; the four named steps;
the state as an element attribute; the row composed from one source; save not in the open verb; the
stop verb's reason as text), `tests/ui/spec.json` (`#ed-summary` asserts the values the collapsed row
names — the same sentence the open editor titles with; `#ed-steps` the named current step;
`#pers-now-stop` the stop reason). **The personalities page is green in the UI harness for the first
time** — all 19 pre-existing failures resolved. Full suite: 1090 passed, 3 skipped, 0 failed.

**Not in this slice.** A 150 px verb at the very bottom of a 6,430 px page answers nothing for a
reader at the top; the strip the dashboard has (`#dash-harness-now`) is the sibling for a state the
code cannot answer in a sandbox (a serving session needs a running harness). The *global* answer to
the page's height — the deck's density and fold placement, a 30-second glance — is queue 33 state 1's
remaining part, and it needs a measure against the repo's code, which the instrument can only do
against a server running that code.

##### 6.14.1s The strip states the figure (queue 33, state 1 closed, 2026-10-13)

**The density measure first, because it changed the slice.** Deck density, measured in a browser
against this checkout: a strip is **43 px** (22.5 px line + 9 px padding), the card 45 px, the deck
gap 6 px — **49 px per personality**, 490 px for ten. The target was ~46 px. So the 30-second glance
was never a height problem, and chrome-cutting was the wrong slice: what the deck lacked was the
*content* of the glance. The strip carried a fits badge with no number on it.

**State 1.** The strip line now carries the verdict and the figure it was measured against —
`fits · heaviest 4.1 GiB of 108.0 GiB · 3 models · max 1 resident` — from the deck's own budget facts
(`data-worst-case`, `data-budget` on the strip element), asserted in `spec.json` as **text on a real
row**, and attributed per card in `test_each_strip_prints_the_budget_of_its_own_card`.

**Defect 1, found while building it: the strip printed the NEXT card's budget.** The deck's budget
comes from `generated`, ordered by card; the loop's `loop.index` is **1-based**. A strip built with
`loop.index` prints a real, correctly formatted figure that belongs to the following card, and the
last prints nothing. Measured before the fix: `safe` printed 4.0 GiB (its own, by coincidence of
ordering), `default` printed nothing. The page's existing budget test *counted formatted budget
spans* and passed. Attribution — each card compared against its own pricing — is the only check that
sees this. `default` is additionally compared against a deck built with `loop.index`, i.e. every card
priced once and misaligned by one.

**Defect 2: `id="pers-now-stop"` on both branches of the Now strip.** A page rendered with a running
router contained two elements with one id: the script found one, the browser could not name the
other. The stop verb is now **one control found by class**, enabled from the payload's answer. The
contract test asserts one id in the markup, which is why it was able to fail.

**Defect 3: the endpoint did not answer the deck's headline fact.** `/api/personalities` answered the
pool (`budget_bytes`) and nothing per card, so a script had to infer the deck's verdict from a
different fact; it now answers `budgets`, one dict per personality. (Writing the test exposed a third
layer of the same bug class: `render()["budget"]` *is* the budget dict, so `.get("budget")` on it
answers None for every card — which reads as "the library prices nothing", and would have made the
new test pass a deck that printed no numbers at all.)

**Findings for the queue.**
1. **Counting formatted output is not attribution.** A test that asserts "one formatted budget span
   per card" passes on a deck that prints every card's figure on the wrong card. Assert each card
   against the value that card prices, and make the fixture's sizes differ so a misalignment is
   detectable at all.
2. **A duplicate id is invisible to a script.** `getElementById` finds *a* node and reports no
   ambiguity; the browser's own name table has one entry. A two-state branch that puts the same id in
   both arms is the shape that produces it, and the contract guard is one id in the markup.
3. **A measure can retire a number a queue has been carrying.** The 6,430 px figure is a
   deployed-server measurement; the repo-code instrument measured 800 px / 2 blocks on this checkout.
   State 3 removed the defect, and the instrument is what lets the removal be stated rather than
   asserted.

Full suite: **1092 passed, 3 skipped, 0 failed**.

**Not in this slice.** A live-router strip state cannot be asserted in a sandbox (finding 3 of §6.14.1r
stands). The reason-when-it-does-not-fit text on the strip is the next candidate: the payload carries
`budget.unpriced` and the findings, and the strip currently omits both.

##### 6.14.1t The strip states the reason, not only the verdict (queue 33, 2026-10-14)

Queue 33's glance is *"you know, in 30 seconds, what is running and what can be served."* §6.14.1s
closed the number. This closes the other half: a card whose verdict is **not** `fits` printed
`does not fit` and stopped. The reason existed — Budget computes two independent ceilings and an
unpriced list, all of it in the payload — and the strip printed none of it. The operator saw a red
word and had to **raise the card** to learn which ceiling failed. That is the click the glance
exists to remove.

The strip now carries, per card:

```
over the aperture by 104.0 GiB     # worst_case − budget, over the aperture
over RAM by 76.1 GiB               # worst_case + host cache − RAM total
cannot price 1: Unpriceable-7B.gguf  # the reason with an action attached
```

Three decisions:

1. **Two branches, not `if`/`elif`.** The aperture ceiling and the RAM ceiling are independent in
   `Budget` and in `validate()`; a card over both must name both. Collapsing them into one branch
   is the shape that turns a RAM problem into an aperture problem, and the test asserts the
   *negative* on each card (`"RAM" not in safe`, `"aperture" not in default`) so the collapse is
   caught by attribution, not by counting.
2. **The unpriced list names the models.** A count alone is a verdict with no next step: the names
   are the models to index or remove, and they are already in the payload.
3. **Absence is asserted too.** In the sandbox every card fits, so the UI spec asserts
   `.pers-strip .pers-reason` with `maxCount: 0` — a reason printed unconditionally labels a
   healthy card as a defect. This required teaching `checkText` that `maxCount` asserts an
   absence: an empty result is the pass, not a missing region.

**What the guard catches (measured by mutation).** Reason moved into the card body only → caught
(the strip's own text is asserted, not the card's). RAM clause deleted, or printed with the
aperture wording → caught. Reason printed for every card → caught. `unpriced` printed as a count
without names → caught. The template's own arithmetic bug → caught by exact-value comparison:
`x|abs|round(1) / 1073741824` rounds the *byte* count and yields `103.98437101300806 GiB`, a true
sentence at the wrong scale, which passes any test that only asks whether a GiB figure appears.
The scale is asserted as `f"{want:.1f} GiB"`.

Two helper names, one file. `_strips()` already existed at the deck section; a second definition
lower in the file silently shadowed it for every test below (the same shadowing as the `budget`
key bug of §6.14.1s). The duplicate is removed and the visible-text helper `textOf()` sits beside
`_strips()`; the new tests read the strip's markup through the existing helper.

Full suite: recorded below.

**Not in this slice.** A live-router strip state remains unassertable in a sandbox (§6.14.1r
finding 3). `findings` is still not on the strip: its wording is a validator's sentence, not a
deck-scale one, and shortening it is a separate decision from printing it.

##### 6.14.1u The strip badge answers both ceilings, not only the aperture (queue 33, 2026-10-14)

§6.14.1t put the reason on the strip. This slice is about the word beside it. `aperture ==
pool` (§6.14.1s finding 3), and the pool tracked the aperture, so a card the validator puts
over **physical RAM** was badged `fits`. The suite did not notice: the budget tests assert
`findings` and `worst_case_bytes`, and no guard anywhere asserted the strip's badge **word**.
Mutation check: force the strip badge to `fits` for every state — 3 guards failed and the deck
stayed green.

The badge is now one of five states, and the verdict answers the ceiling that failed:

```
fits              # inside both ceilings
over RAM          # fits the aperture, over the machine
does not fit      # over the aperture
budget unknown    # no aperture to measure
cannot be bounded # a model the library cannot price
```

Four things this slice had to get right, each found by mutating the template and watching the
guard stay green:

1. **The badge word is asserted against a table, not against the page.** A guard that reads the
   verdict off the strip and compares it with the strip cannot notice the verdict *changing*.
   The state table carries the expected badge word per state; the guard compares the page with
   the table.
2. **States are forced at the measurement layer.** The first draft hand-edited each card's
   `findings` — that asserts the deck against a state the host can never be in, and the deck
   goes green against a wording the validator never writes. The five states are forced by
   replacing what the budget is *computed from* (`peak_bytes`, `host_cache_bytes`,
   `ram_total_bytes`, `_pool_bytes`). Mutating the personality (`models`, `models_max`) is
   rejected for a second reason: it changes **which card the page has**, so every assertion runs
   against a personality the page does not show. The unpriced state needs the *pricing* step to
   answer None (a GGUF whose header cannot be costed) — a missing model makes `describe()` raise
   and the API answers 409, which is a different state with no card to read.
3. **Span selectors must close at the quote.** `class="badge yes"` is not matched by a pattern
   whose class attribute must END at `badge`; `class="mut pers-reason"` is not matched by one
   that must END at `pers-reason`. Both guards silently measured **nothing**: the badge guard
   compared empty strings, and the clause loop iterated zero times, so the doubled verdict it
   was written to catch passed the suite.
4. **A length guard on markup measures attribute bytes.** `_strips()` yields the button's inner
   markup, which carries `data-worst-case="4311748793"`. A 120-character limit on the raw markup
   was met by a strip that printed no verdict at all. `stripVerdict()` selects the badge and
   reason spans; `textOf()` is kept for name-in-sentence assertions.

**The reason clause: a cause, not the verdict restated.** A clause must state a figure, or the
ceiling it could not measure. `_isVerdictClause()` decides by the figure, not by words:
`over RAM by 3.0 GiB` shares every word with its badge and is the honest form; `cannot be
bounded` beside `cannot price 1: X` is the question asked twice with no reason. The clause rule
**licenses its ceiling words from the state table** — a hand-kept allow-list became a third
thing the template had to agree with, and silently rejected the unmeasurable state's only honest
wording (`no readable memory budget` names the ceiling, so it must be allowed by the state that
produces it).

**Attribution, again.** The strip's sentence must name the card it judges. A guard that reads
only the badge and reason spans cannot see the name — it lives outside them — so the guard
asserts the name among the strip's full text. The deck has already printed a neighbour's number
once (§6.14.1s); the same class of bug applies to sentences.

**UI contract.** One badge per strip; at most one verdict word per strip; the strip's sentence
names the card (`#pers-safe` scoped, because deck-wide text always contains every card's name);
the reason prints the figure. Page-level assertions can only see the state the sandbox happens
to be in — the five states live in `tests/test_personality_routes.py`, and the spec says so.

Mutation table (each is the defect, each confirmed caught, each reverted):

| mutation | guard that caught it |
|---|---|
| unpriceable badge → `budget unknown` | badge == table word, per state |
| RAM clause dropped | §6.14.1t clause guard, over-RAM state |
| RAM badge collapsed to `does not fit` | badge == table word, per state |
| strip omits the card name | name-in-strip text |
| reason clause with no figure/ceiling word | clause rule (states neither) |
| strip badge always `fits` | badge == table word, per state |
| verdict clause with no figure | `_isVerdictClause` |
| badge span selector matches nothing | `stripVerdict` selects spans, not markup bytes |

Suite: **1080 passed, 3 skipped.** UI integration: **15 passed**, personalities green.

**Not in this slice.** The strip still carries no `findings` text (§6.14.1t). A card in the
`no aperture` state and a card in the unpriced state are asserted by forced measurements in
pytest, not on the live page: the sandbox cannot be made unpriceable without reaching the
operator's model library (§ portal-test-sandbox).

##### 6.14.2a The help index is a vertical outline, not a column ribbon (queue 42, 2026-10-14)

§9 of `docs/PORTAL_POLISH.md` measured the help page's TOC at **1,024 px of client width against
13,749 px of content** — 13.4 viewports of horizontal scrolling. The cause is one CSS declaration:
`columns:2` on `.kb-toc`. Multi-column layout balances content **across the inline axis**, so a
long vertical list becomes a wide one that the box's own `overflow-y:auto` cannot reach: the
horizontal overflow is the page's *index*, and the widest element on the page is the thing the
reader uses to find things.

Two changes, each guarded:

1. **`column-count:1`, with the scroll kept.** The index scrolls vertically, which is the reading
   order of a table of contents. The guard asserts the declaration (`columns` / `column-count`
   must be 1) **and** that the rule keeps `overflow-y:auto` — a capped-height index that cannot
   scroll hides the sections below the fold, which is the failure the ribbon was a workaround for.
2. **The outline groups by document.** `kb.toc()` returns every section at level ≥ 2 (235 of
   them). Printed flat, the index answers *"which section is §6.14.1u"* and not *"which document
   am I in"* — and §6.14.2's shape is documents and their headings, not a ribbon. The template
   now loops `docs` and filters `toc` per document, with a sticky heading per group.

The guard is the cheap-mirror kind (same shape as `test_tab_geometry_holds_the_four_rules`): it
asserts the declaration in the page's own stylesheet, so a reverted rule is caught in
milliseconds instead of in a browser sweep. Mutation check: restoring `columns:2` and restoring
the flat loop each failed the guard, and both were reverted.

**Not in this slice.** The left-rail/two-pane layout, the document viewer as a pane (not the
inline section panel), and search as a collapsed affordance — the rest of §6.14.2's sequence. The
41 dead in-page document links §9 names are not touched here.

##### 6.14.1v A card over both ceilings states both clauses, in the order the operator acts (queue 33, 2026-10-14)

Queue 33's glance, the last collapse the strip kept. `Budget` measures two ceilings — the
router's aperture and the machine's physical RAM — and `validate()` reports them as separate
findings. The strip printed the reasons as two independent `{% if %}` blocks, but the *badge*
picked its word with an `if`/`else`: whenever the aperture also failed, the verdict read
`does not fit` and the reader learned only the aperture story. A card the kernel kills for RAM
on a box that is *also* over the aperture — the exact shape of the recorded OOM events on this
box — lost the clause the operator can act on (add RAM) at the moment the card had two defects.

The collapse was invisible to every guard written so far, and that is the finding worth keeping:
**each earlier state forced ONE ceiling at a time**, so the aperture-first `elif` variant of the
same markup was green under all of them. The guard that catches it forces **both** ceilings on
one card and asserts the printed *clause list*: two clauses, each carrying its own figure
derived from the card's own measured numbers, RAM stated first (the memory the operator can add
has a next step the aperture does not offer).

Mutation checks (each caught, each reverted):

| Mutation | Guard that caught it |
|---|---|
| badge forced to `fits` in every state | state table (§6.14.1u) |
| aperture-only `elif` clause chain | new two-clause guard |
| RAM clause deleted | new two-clause guard |
| clauses swapped back to aperture-first | new two-clause guard's order assertion |

The page-level assertion in `tests/ui/spec.json` stays the shape (a badge per strip, max 2 with
the live badge): the fixture's cards fit both ceilings, so the page asserts the clause spans are
ABSENT, and the two-clause card lives in the route guards, where the state can be forced at the
measurement layer (`peak_bytes`, `host_cache_bytes`, `ram_total_bytes`, `_pool_bytes`) — the
same rule §6.14.1u fixed: every sentence asserted must be one the host can produce.

**Not in this slice.** `vitals.py`'s CPU chart keeps its own `if/elif` collapse (gpu → vram →
gtt → cpu) and no guard forces two of those states at once; that is §8's instrument question,
recorded below as a candidate.

#### 6.14.1w Queue 32 on the Projects page: Launch opens the project's session, in place (2026-10-15)

The operator's report, measured: *the Launch button opens a message box about the personality and
nothing happens after OK.* Both halves were real. The confirm box they saw was the **Delete** verb's
dialog, reused for Launch; the Launch click handler itself had been deleted in 9410505 ("Projects: the
Launch verb is labelled inert until the harness session exists"), which shipped the harness page. So
the button confirmed a thing it was never going to do. The queue-32 requirement is the page's second
state — *operational = the session and nothing else* — and the handoff point is this button.

**Requirement.** Press Launch on a row and the Projects page becomes that project's session: the
title and model it runs with, the context meter, the prompt box, the transcript, the answer form when
the agent asks, and Quit. The session is the portal's own launched session, not any session that
happens to point at the folder. Nothing about the host is invented by the page.

**Procedure.** Core verbs in `harnessctl.py`: `session_messages` (the transcript, bounded, never
raises — a session that will not narrate is a session, not a failed page), `session_context` (the
meter: numerator from the host's per-step token accounting, denominator from the ctx the
personality's models are actually served at), `session_question`, `session_answer`, `session_reply`
(question → answer → watch, refusing a malformed form answer), `project_launch` (launch **inside**
the project and record the session with the personality it runs with), `session_quit` (stop serving,
leave the session open for resume), `session_facts` (the page's operational payload). The record is
written at the call site that knows all three facts at once — session id, folder, personality — so
the page reads the answer instead of remembering what it clicked (D1). Routes: `POST
/api/projects/launch`, `GET /api/projects/session?name=`, `POST /api/projects/quit`. The page switches
views in place: banner, focus mode, 3 s poll, `Ctrl+Enter` sends, `Shift+Enter` is a newline natively,
`Esc` returns to the registry with the session id left named.

**Findings.**

1. **A green suite coexisted with a broken live page.** The UI fixture at `tests/ui/spec.json` carried
   keys the real payload never had — `registered`, `personalities`, `launch_available`,
   `harness_session`, `serving_personality`, `session_facts` — and the page fetched
   `?directory=`, `?dir=`, `?project=`, none of which the routes accept: measured live,
   `GET /api/projects/session?project=ai-portal` → 422 `{"type":"missing","loc":["query","name"]}`.
   The harness never asserts those keys, so it exercised a payload the server does not produce. The
   fixture is now written from the live payload's shape.
2. **The suite could not see a missing endpoint.** `interact()` counted every
   `fetch(...).catch(() => null)` as exercised, and the dead-control report skipped steps with
   `!step.json`: a page posting to an endpoint that does not exist reported *all controls exercised*.
   A post that answers no HTTP status is now a failure. (The probe run found a second instance of
   this class on the personalities page — `#mp-attach` in a closed `select` — which the old rule
   counted as exercised and the new rule names.)
3. **A gate that exists only in the browser is not a gate.** `requireConfirm` made the harness the
   sole confirmer for every page action, and the core posted `session_send`/`session_answer` to the
   host unconditionally; the page's `confirm` field was hardcoded `true`, and the core endpoints had
   no `confirm` parameter at all, so a real gate could not be sent. The bodies now carry `confirm`,
   the core refuses without it (409), and the harness confirms it.
4. **Measured on the operator's real tree** (`AI_PORTAL_HOME=$HOME`, installed services): `POST
   /api/projects/launch {name:"ai-portal", personality:"Agentic-PI", confirm:true}` → 200, session
   `ses_ec0dd63e…`, port 4096, `recorded:true`; `GET /api/projects/session?name=ai-portal` → that
   session, `context_limit 262144` (the personality's served ctx — read, not invented), busy false,
   turns 0, transcript 0; `POST /api/projects/quit` → 200 and the markers cleared
   (`served.count 0`, the row reads "no session has been launched for this project").
5. **A guard that refused to launch the portal's own folder was written and dropped.** On this box the
   registered project *is* `/home/cortana/ai-projects/ai-portal` — the operator develops the portal
   with a harness session bound to it. A refusal "the portal's own folder is not a workspace" would
   have disabled the only launch the page has. The name-validation guard (`/`, `..`, over-long) stays.

**Not in this slice:** the session's live event stream (the page polls, 3 s); more than one session
per project (the record holds one; the harness page lists the rest); closing or deleting a session
from the Projects page (the harness page's verbs — measured: no `/api/harness/session/close` route
exists, 404); a separate `/pi` route; queue 43's question-card semantics.

##### 6.14.1x The projects page's active state is the server's state, and a spec key nobody implemented (queue 32, 2026-10-15)

The operator's words, in full, are the requirement:

> "I did not want a tiny panel with a field to submit a prompt at the bottom of the projects
> page … What I want when the projects page is in the 'active' state is for the projects page to
> take me to the launched full opencode session for that project that should be accessible by
> anyone opening the portal and which should remain a durable active agentic task even if the
> user closes their browser session or changes pages on the portal. The portal needs to identify
> what project the active opencode session is for and provide a visually obvious but unintrusive
> means for the user to close/end the active opencode session and return the project page to the
> 'inactive' state … because an 'active' opencode project session must survive a ai-portal
> restart the projects page must detect that running session and take you to it rather than
> showing the 'inactive' launchpad … it was designed like many coding focused tools to have full
> access to the screen's size, we need to be careful with how the portal visually frames and
> interacts with the opencode harness."

**The state is the server's, so the page is a reader of it.** The serve record lives in the
registry (`/api/projects` rows carry `serve`), which is on disk and survives a portal restart,
and it is re-checked against the host (`live_sessions(directory)`) on every read. That is why
*anyone* opening the portal sees the same active session: there is no browser-side state that
could be the only copy. So the page reads the payload on load and **switches itself** — the
auto-switch runs inside `refresh()`, on the load-time fetch, not only on a poll. `opChrome()`
toggles two elements, `#proj-launchpad` and `#proj-op`, because a mode the page produces by
walking its siblings is a mode no test can name.

**The meter's denominator is read from the personality.** Queue 32's budget line
(`context: 61k of 262k tokens`) needs the context the session's models are actually served at, so
`serving_context_limit()` reads the personality that serves the project — a context number
invented in the page is a bar that lies. A session with a known context and no readable
personality says `context limit unknown, so this bar has no ceiling to be measured against`, and
an unreported session says `the host has not reported this session's tokens`.

**Closing the session.** `End session` sits at the head of the session view — where a reader
looking for it looks — styled quiet, because closing the session is not what the view is for. It
confirms, posts `/api/projects/quit` (confirm-gated server-side, like every verb here), the
record clears, and the page falls back to the launchpad by itself.

**The harness fix, which is bigger than this page.** `tests/ui/run.mjs` read its assertions
through bare property access, so **a spec key the runner never implemented was silently
ignored**: the projects scenarios I had just written carried `expectRequestsOnLoad` and
per-fixture `expectVisible`, and both passed vacuously. The runner now fails on any `expect*`
or `then*` key that no scenario implements, with the reason (`no scenario implements this key —
the assertion is vacuous`). The guard caught two keys already in the spec: `expectTitle`,
unimplemented while ten pages asserted it (a page with a wrong title would have passed), and
`expectNoInjectedMarkup`, implemented all along — the guard itself was lying about it.
Implemented here: `expectRequestsOnLoad` (a page that shows a state only after a click passes
every interaction assertion in the world and still fails this one), per-fixture `expectVisible`
(a fixture that answers an error payload, and the page that shows the launchpad on it, were
previously invisible to the spec), and `expectTitle`. The implementation list is **derived from
the runner's own source**, so it cannot drift from it.

Scenarios: a page with two modes needs two entries on one path. `projects-active` carries the
served registry answer with **no browser state of its own** — the same situation as opening the
portal an hour later, from another machine, or after the portal service restarted. Both
directions are asserted: active shows the session and hides the launchpad, inactive shows the
launchpad and hides the session.

**Measured, not asserted.** The first red of `projects-active` was the honest kind: the page
loaded into the launchpad with the registry saying a session was running. The cause was a shape
bug — the row's serving state is `serve.served` and the page looked for `served`; read as
`undefined` that means *not served*, so the page would never have shown a running session to
anyone. The second red was mine: the spec asserted `#proj-sess-quit`, `#proj-ctx-num` and
`#proj-view` — selectors the page never had. Vacuity in the other direction: it passes because
nothing matches.

`tools/layout_measure.py` on both modes: launchpad 4 columns, 0 overflow, 0 short rows, 0
overlapping; session view 1 column, 0/0/0. Suite: 1126 non-UI (+31) and 16 UI (+1 scenario).

**Still open, in the operator's own list:** the live event stream (3 s poll today — prompt,
answer and quit are live, a tool call mid-step is not); more than one active session per project
is not offered, and when more than one project is served the page takes the first; closing a
session from the harness page (measured: no `/api/harness/session/close` route — 404); the
update-status section for the opencode engine itself (queue 42); and the thing the active state
exists to become — this session, replaced.

#### 6.14.2 The knowledgebase becomes a documentation site

`/help` today is one column with panels — the shape of every other portal page. The
operator wants the common docs shape: an outline table of contents fixed on the left;
search results, or the section you are reading, on the right.

The sequence:

1. Left rail with the section tree, right pane with content. Deep links keep working —
   section ids are the links, and the guard over them covers 102 links today.
2. Split the living documents by topic. `PORTAL_PLAN.md` is 3400 lines of plan, history,
   reasoning, and measurement logs in one file. The manual is the outline; the history
   stays linked from it, not deleted.
3. Search stays server-side over section bodies (`app/core/kb.py`), results in the right
   pane, outline filtered on the left.
4. Lists the operator wants findable as sections with ids: the project's tools, the
   measured runs, the decisions and the evidence behind them, commits. A settings row, a
   status page, and a commit message can then point at the same place.

#### 6.14.3 The portal as a workplace hub

The difference between a management portal and a hub: the hub carries work in progress.
The next surface is the pi agent harness — active agent projects visible to anyone on the
local network, from any system, not only this machine's OS GUI. That is a §7 roadmap item.
The pieces it leans on are the ones being built for the portal itself: a knowledgebase
that can be linked into, a settings layer that is data, and a model index that measures
rather than asserts. The pi harness stays an add-on with a side-car (`add-on/`,
`add-on/side-car/`); it is not portal core.

### 6.15 Commit attribution correction (2026-10-06)

Fourteen commits carry `Co-Authored-By: Claude Opus 4.4 <noreply@anthropic.com>`. That
signature is false. The agent writing them runs a Qwen 3.8 Flash derived model inside pi;
it is not Claude and not made by Anthropic. The operator's ruling: leave the history
alone, because docs in this file cite those hashes and a rewrite changes every one of
them, and sign forward as `Co-Authored-By: Flash, the portal gremlin`.

The rule that came out of it is short: do not claim an identity the model running the
session does not have. It applies to commit messages, docstrings, and any text that
outlives the session.





## 7. Build-out roadmap

| Phase | Deliverable | Exit criteria |
| --- | --- | --- |
| A | Skeleton: uv project, `app/core/paths.py`, FastAPI, `portal.db`, read-only status page for all three providers | `systemctl --user start ai-portal`; status page shows router/models/releases |
| B | Service manager: start/stop/restart for router + ComfyUI with confirm dialog, rollback note, audit event, per-bubble controls | stop/restart router from UI with confirm; rollback note written; ComfyUI controllable once its unit is installed |
| C (done, 2026-09-03) | llama.cpp updater: full build→stage→verify→swap→restart pipeline + rollback button | one click bumps a release (no-op path proven by `tools/live_smoke.sh`); old release restorable |
| D | Model manager: inventory, gguf metadata, presets generator, HF-command downloads, dedup/GC | paste an `hf download` cmd → model appears in router with correct preset |
| E | ComfyUI provider: status, venv sync, systemd service | start/stop from UI; update runbook verified |
| **F (done, 2026-09-14)** | Health pane: apt/ROCm/SDK probes, disk, orphans, index freshness (`/health`, `app/core/health.py`) | every UPDATES.md input visible **and the pane found four real problems the first time it ran** (13-day-old index, 3 model dirs one level too deep, HF cache dirs inside the models dir, ROCm never stamped at build time); 28 tests incl. 4 route tests, 4 new mutations (28/28 caught); §4.5 |
| G | Frontend polish per D1 | — |
| H | RAG service: per-project stores, ingestion jobs, MCP interface (§4.8) | two demo projects searchable, zero cross-project hits; start/stop from UI |
| I (done, 2026-09-11) | Settings page: shared runtime env (`ai-env.env`, e.g. ROCm `LD_LIBRARY_PATH`) + per-service host/port with live validation (TCP range, cross-service collision, system-wide binding probe, privileged warning) and external-access toggle (loopback ↔ 0.0.0.0) | change a port in the UI → validated live → persisted to env file → explicit restart applies it; shared var visible + editable for all three services |
| **K (done, 2026-09-13)** — network | Bind **modes** (loopback / lan / all) per service, live reachability probes, firewall state + scoped `ufw` hints without root, exposure warnings, optional router `--api-key-file`, login throttle; all three services on the generated env→wrapper→unit launch pattern | portal + ComfyUI answering on `192.168.1.228` (verified by LAN-address HTTP from this box), router env/unit prepared with argv verified against the live process, `pending_restart` reported until it is restarted |
| J (mostly done, 2026-09-13) | Project reorg + deployment model: source repo vs `~/ai/ai-portal/` deployment, idempotent install/update script, portal as a real user service, logging system (levels via launch param), documented debug tooling (§0). Remaining: `app/main.py` route split, `tools/` migrations move | redeploy-from-git-SHA works and **verifies the running sha**; `dev-run.sh` runs alongside the service on 8091; log level changeable without code edits |
| **L (done, 2026-09-13)** — UI tests | Integration tests for every interactable element: jsdom harness driving each page against a sandboxed portal (`tests/ui/`), static UI contracts (`test_ui_contract.py`), `scripts/check-ui.sh` as the single entry point, pre-commit hook, and a mutation check that proves the suite can fail | every control on every page is driven and must have an effect; tabs are a bijection; destructive requests are confirm-gated before the request; coverage gate fails on any visible control never driven; 15/15 injected regressions caught; found 3 real UI bugs (§6.6) |
| **L.1 (done, 2026-09-13)** — tab geometry contract | Tab/panel junction defined once in `web/static/tabs.css`, asserted as CSS text and measured through jsdom's cascade | tabs sit on the panel's top edge with one baseline that opens only under the active tab; 6 geometry mutations prove it; §6.7 |
| **L.3 (done, 2026-09-13)** — router pulse | Status page reads the journal instead of showing it: per-slot lanes with real phases, throughput stacked by slot, model-switch banner, collapsed attention pane (§6.9) | "is it working, on what, how fast, anything wrong?" answered without reading a log line; 22 parser + 10 page tests; 6 new mutations; service controls provably untouched |
| **M (done, 2026-09-14)** — jobs are machine state, not page state | Global job bar on every page (`/api/jobs/active`, step + elapsed + in-place log), machine-wide exclusivity for disruptive updates with named 409 refusals, service controls gated while a job runs, ComfyUI update rebuilt around the service's own venv + generated pip constraints + verify-before-restart (§6.11) | a job stays visible and its machine stays locked from any page; a ComfyUI sync cannot replace this box's ROCm torch, and cannot restart the service on a venv where `torch.version.hip` is missing; 17 job tests + 18 updater tests, 2 jobbar pages, 5 new mutations (33/33 caught) |
| **N (done 2026-09-16 → 2026-09-22)** — the model table and personalities | N1 index + incremental scan, N2 memory estimate (weights + KV + runtime, sliding-window and per-layer-head aware), N3 models page (one table, live light, capability facts parsed from the chat template rather than pattern-matched, peak RAM), N4 presets: generate → diff → gated apply through the job system with a snapshot before every write, plus a settings editor whose controls are rendered from the registry it validates (§4.9, §4.10) | "smallest full model that fits in 9 GB" is a query, not a heuristic; the smoke test cannot fail on model choice twice; presets gain per-model settings from a generated, diff'd, validated file — and **every apply is undoable from a snapshot taken off disk** |
| **L.2 (done, 2026-09-13)** — dashboard de-noise + build manager | Hero cards carry service state only (SDK inventory → GPU tab, duplicate Metrics link gone); stored builds become one dropdown with Switch/Delete, build-detail confirms, build count, and server-side refusals | the running build is undeletable and unswitchable-to at both ends; 6 backend tests + selectCases/then_click; 17/17 mutations; §6.8 |

Each phase ships behind the same safety model (§6) and keeps the `tools/`
scripts working as the manual fallback.

## 8. Decisions (locked) & open questions

**D1 — Frontend (DECIDED):** HTMX + Jinja templates as the base. The UI is a
single-user ops console, so no JS build step. **Chart-friendly constraint:**
every read is also available as a JSON endpoint (`/api/*`), and the templates
are allowed to embed small inline `<script>` blocks + a `/static/` asset dir.
Adding a CPU/RAM graph = drop in a `<canvas>` + a few lines of JS that
`fetch()` the existing JSON endpoint — **no front-end re-engineering**.

**D2 — Access (DECIDED):** Portal-only auth, simple app password, LAN
(authorized users on our local network). No cloud auth. See §3.

**D3 — Updates (DECIDED):** **Advisory for now.** The portal surfaces apt
pending set + reboot-pending + version drift and *offers* actions; one-click
execution (including handling any required post-update reboot) is a later
enhancement, not required for v1. Design the job interface now so execution
slots in without rework.

**D5 — Rollback store (RESOLVED):** the 244 G LM-Studio rollback copy has been
removed.

**Presets (note from user):** the per-model `[model]` sections were removed from
`~/ai/config/llama/presets.ini` because their hard-coded `ctx-size` caps were
inaccurate and caused llama.cpp failures. **The Phase D presets generator must
derive context from GGUF metadata (actual trained ctx) and be conservative —
never hard-code context maxes.** Only the validated `[*]` globals remain today.

Remaining open (can proceed without them):

- **D4 — ComfyUI service (DECIDED):** user service `ai-comfyui` like the
  router (see §4.3). Unit file committed; install + verify in Phase E.
- **D6 — gguf-indexer.** Port its working code into the portal (my
  recommendation — one codebase to maintain), or keep it as a sidecar tool the
  portal shells out to?
- **D7 — Presets (DECIDED 2026-09-20).** Show the diff, a human approves it. The portal
  generates and displays; it does not write. Next step is a settings-style editor for the
  global and per-model keys, with help text and a control matched to each value type — which
  is why the key definitions live in `personality.SETTINGS` (kind, section, default, choices,
  help, validator) rather than being re-derived by a form.
- **D8 — Compat symlinks.** Keep `~/llama.cpp`, `~/ComfyUI`, `~/comfyui`,
  `~/vulkan` forever, or remove once the portal has been stable for a while?
- **D9 — Personalities.** See §4.7: named states = generated presets +
  allowed-model symlink dir + GGUF-derived memory budget (OOM-proof by
  construction). Sequencing: Phase B (router control) + Phase D (gguf
  metadata) first; `--models-max > 1` for the multi-model scenario.
- **D10 — RAG vector store.** Embedded per-project files (sqlite-vec /
  LanceDB) vs Postgres+pgvector from day one? Recommendation: embedded — no
  new server on a single-user box; pgvector is the scale-up path (§4.8).
- **D11 — RAG/MCP scope.** First MCP tools: search-only vs search + ingest +
  project admin?

## 9. Immediate next steps

1. ~~You: decide D1–D3~~ → **done** (D1 HTMX+Jinja chart-friendly; D2 LAN +
   app password; D3 advisory + one-click later).
2. ~~You: free the 244 G rollback copy~~ → **done** (D5).
3. **Phase A** (done 2026-08-31): uv project + FastAPI + LAN/password auth +
   `portal.db` + read-only status page for all three providers + metrics/health
   probes. Verified: 11 smoke tests pass, and `tools/live_smoke.sh` drives the
   real app against the live router (14 live / 13 disk models). See README.
4. **Router hero / live observability** (done 2026-08-31, commit after A):
   the dashboard hero is the live llama router — `/v1/models` registry
   (loaded/loading state), `/metrics?model=<loaded>` cumulative counters
   (router mode requires `?model=`; bare `/metrics` 400s), journal-derived
   TPS sparkline (b10641 `tg = N t/s` format), classified activity feed
   (`proxy_request` self-noise filtered), per-completion stats parsed from
   `print_timing` blocks (**slot-keyed**, so concurrent agentic +
   autocomplete + RAG slots don't interleave: prompt/gen counts + t/s, total
   time, MTP draft acceptance), and cgroup-v2 service RAM (user-slice
   `app.slice` path; aggregates router + spawned workers). All via
   `/api/router`, polled at the `router_poll_ms` setting.
5. **Phase B** (done): start/stop/restart for router + ComfyUI from the UI
   (confirm + rollback note + audit event).
6. **Phase E (ComfyUI)** (done 2026-09-04): `ai-comfyui` user service via the
   generated env-file pattern (§4.3.1); ComfyUI model inventory on the models
   page (26 categories); portal-side model downloads placed directly into
   `models/<category>/` (`POST /api/models/comfyui/download`, job-tracked,
   `.part`-then-rename) — replacing the browser-download-then-manual-copy
   workflow.
   - **Phase E part 2 (done 2026-09-04)**: ComfyUI update pipeline —
     `git fetch` → behind-count → `git pull --ff-only` (dirty tree aborts
     with the file list) → venv `requirements.txt` sync → service restart +
     health wait; one-click rollback (`git reset --hard <commit>`).
     ComfyUI **hero pane** on the dashboard (service state pill, Web UI
     light + link, version from `/system_stats`, cgroup RAM, journal tail;
     polls `/api/comfyui` at 5 s — the same shape as the router hero).
     Smart **add-model** panel (models page) implementing the §4.2 Downloads
     v1: paste HF CLI command / `org/repo` / URL / local path → portal
     fetches, sniffs, places (GGUF → `~/ai/models/llama/<dir>/`,
     safetensors → `~/ai/comfyui/models/<category>/`), indexes portal.db;
     `app/core/modeladd.py` + `modelmeta` sniffing. Also fixed: stale
     `router.snapshot()`/`_probe` refs from the router.py rename, and a
     real `_gguf_parse_kv` offset bug (any GGUF with >1 KV field failed to
     parse — caught by the new `tests/test_modelmeta.py`, which also covers
     the mmap-refusal fallback for sandboxed tmpfs). 64 tests pass;
     `tools/live_smoke.sh` green. Remaining: C–G per §7 (D's presets +
     GC/dedup are the rest of the model manager), and H (RAG/MCP, §4.8)
     when planned. Each behind the §6 safety model.
7. **Phase I (Settings, done 2026-09-11):** `/settings` page + backend.
   - `app/core/serviceenv.py` — rewritten to own the **shared**
     `~/ai/config/ai-env.env` (read by all three units); `read_env/write_env/
     set_var/get_var`; values quoted for systemd. API: `GET|PUT
     /api/service-env`. Home of the global ROCm
     `LD_LIBRARY_PATH=/opt/rocm/core-10.0/lib` setting.
   - `app/core/units.py` — NEW (split out of serviceenv during Phase I):
     per-service env file + shell wrapper + static unit generation
     (`write_env/write_wrapper/install_unit/env_path/wrapper_path/unit_path`).
     `install_unit` emits BOTH `EnvironmentFile=` lines (shared + per-service).
   - `app/core/serviceconf.py` — NEW: per-service host/port registry
     (`SERVICE_CONF`: llamacpp/comfyui/portal → env file + keys + unit),
     `service_ports()`, `validate_port()` (range 1–65535, cross-service
     collision, live `ss -tlnH` binding probe with own-current-binding
     tolerated, <1024 warning), `set_service_port()` (persist, no restart),
     external access = host 0.0.0.0 vs 127.0.0.1.
   - `app/main.py` — `GET /settings`, `GET /api/settings/services`,
     `GET /api/settings/services/{svc}/validate?port=N`,
     `POST /api/settings/services/{svc}` (422 + structured errors on
     validation failure).
   - `app/core/systemd.py` — `MANAGED_UNITS` gains `portal` (ai-portal), so
     the Settings page can restart the portal itself.
   - ComfyUI env mapping switched to **discrete** `COMFY_HOST`/`COMFY_PORT`
     (+ `COMFY_ARGS` = extra args only) with wrapper
     `--listen "$COMFY_HOST" --port "$COMFY_PORT" $COMFY_ARGS` — matching what
     `serviceconf` reads. Tests updated accordingly (64 green).
   - `.env` (repo root, held an HF token; nothing in code reads it) is now
     gitignored.
8. **Phase F (health pane, done 2026-09-14):** `/health` — the operator's
   "what does this machine need from me?" page, built from docs/UPDATES.md's
   list of things that were previously a runbook.
   - `app/core/health.py` — NEW. One probe per question, each TTL-cached on its
     own and each degrading alone: `apt()` (`apt-get -s upgrade`, GPU/kernel
     packages separated by regex), `rocm()` (dpkg + `hipcc` + the launch
     directory from the shared service env), `torch()` (parsed out of the venv's
     `dist-info` name — never imported), `vulkan()` (installed dirs, the one the
     deployed build used, newest Khronos tag), `disk()` (`du -sxb` per tree),
     `model_store()` (index age, presets without directories, directories the
     router cannot see, broken links in `~/ai/bin`), `host()`, and
     `advisories()` which turns them into bad/warn/info with a link to the
     control that acts. `rocm_root()`/`apt_stamp()` are env-overridable so no
     test reads the host's /opt/rocm or /var/lib/apt.
   - `providers/llamacpp.py` — builds now stamp `"rocm"` into
     `llama-releases.json`, and `provenance()` answers the health pane from local
     reads only (§6.10).
   - `app/main.py` — `GET /health`, `GET /api/health/detail`, `_ago`/`_dur`/`sz`
     Jinja filters (the pane asks "how old" about a dozen things).
   - `web/templates/health.html` — cards, no tabs (§4.5: panels read together,
     not alternatives); `base.html` gains the nav link. Local styles only: the
     attention list, the free-space bar. No tab geometry, so `tabs.css` is not
     required by this page.
   - Tests: 28 in `tests/test_health.py` (probe tests against shims + temp
     trees, 4 route tests asserting what a person sees, that the pane offers no
     form of its own, and that `?refresh=1` really re-probes); UI spec entry with
     `expectNoRequests` on the update endpoints and assertions pinning **values
     next to their labels** (`ROCm installed (apt) 9.9.0`, sizes carrying a unit)
     rather than the existence of a heading; `tests/ui/bin/{apt-get,dpkg-query,
     hipcc}` shims plus sandbox seeds for a ROCm tree, a torch venv, presets.ini
     and the model index, so jsdom sees deterministic numbers; 4 new mutations
     (empty attention list, missing model-store card, raw byte counts, blank
     installed ROCm) — 28/28 caught.
   - The first live render found four real things (index 13 days old, three model
     directories one level too deep for `--models-dir`, HF `hub/`+`xet/` cache
     dirs sitting inside the models dir, and no ROCm stamp on the deployed
     build), which is the exit criterion this phase was actually for.
   - Wide tables (apt packages, disk trees) scroll inside their card via a
     page-local `.scroll`, measured at a 400px viewport: the pane adds no
     horizontal overflow of its own. **Found while measuring:** every existing
     page already overflows ~100px at 400px wide because of the nav's logout
     form (`/`, `/models`, `/metrics`, `/settings` alike) — pre-existing, not
     introduced here, and part of phase G's narrow-viewport pass.
   - ComfyUI's repo commit (`comfy_install()`, a local ``git log`` with no
     network path) completes the version table: when a workflow breaks, "did the
     repo move or did the venv" is two rows, not an excavation.
   - Deferred, on purpose: dashboard attention badge (§6.10), reboot/apt apply
     buttons (§6 says a confirm is not the same as a root policy), and the
     ComfyUI commit row (the repo's git state is not in the registry yet).
9. **Phase M (done, 2026-09-14)** — long actions, everywhere:
   - The complaint was "the ComfyUI update did nothing and showed no progress";
     the job had succeeded. Progress was real and page-local, so the bar moved to
     `base.html` and the machine learned to refuse (§4.4, §6.11).
   - Two live bugs on the way: `ComfyUI.update` raised `NameError` on a dirty
     repo, and the updater installed into an assumed venv instead of the one the
     service runs (`$COMFY_PYTHON`). Both are now tests.
   - The stale-job reclaim wrote its note with `log || ?`, which is NULL when the
     log was NULL: a job killed before its first line left no trace at all. Fixed
     with `COALESCE`, and the test asserts the note survives.
   - Harness changes this needed: page specs can `extends` another page (the
     jobbar pages are the dashboard with a job payload, not a copy of its
     fixtures); fixtures answer by command structure; and the control sweep now
     walks **past** a disabled control instead of stalling on it — with a job
     locking the dashboard, it used to reach nothing and blame unrelated links.
   - Left alone deliberately: per-job progress *percentage* (a cmake build has no
     honest denominator; the step line and log are the truth), and a desktop
     notification (the bar is already the notification).
10. **Phase J** — see §0 for the step list and target layout.

##### 6.14.1y The editor becomes a card surface, not a stepper (2026-10-18)

The owner's verdict on the queue-33 stepper, Oct-18: *"the personalities editor is a hot mess,
there are links to 'steps' that are arranged like tabs but just replace the whole editor section
with no clear way to go back or forward."* The instruction: make the form act like tabs or just
be tabs — or, the owner's own suggestion, **cards on a surface**: one card per settings group
with a unique alias per model, addable/removable, plus a **Global card that appears on every
surface and cannot be removed, only edited**. Clicking a card brings up its editor with its
current values. Discretion granted; the suggestion is a direction, not a spec.

What the surface is (§6.14.1q's stepper retires; its height lesson stays):

- **Three regions, one page**: identity, surface, review — all visible together. The stepper's
  "step N of M" is gone because "which step am I on" is no longer a question the page can ask.
- **The Global card is pinned**: it hosts the one `#ed-global` grid, its strip carries the live
  `N value(s) in [*] · k proposed` figure, and the pin is stated in text on the strip ("always
  on the surface · edited, never removed"). No remove control exists.
- **A model card is on the surface exactly when the tick list covers that model** — the one
  store. `data-on-surface` is painted from it; untick removes the card (the card's "remove from
  surface" button is the same untick, and it names what survives: the settings and any model
  personality). Tick adds it back; clicking an off-surface card's strip also restores it —
  a card the user clicked is a card they mean to use.
- **The card is the editor**: opening a card moves the one model grid into it (the §6.14.1k
  single-input-set rule survives), a second click closes it. The review bar (summary → Preview →
  Apply, budget, refusals, diff) sits at the bottom of every surface: the ledger is never a step
  away, and the summary is the first line the surface answers "what would change?" with.
- **Collapse survives** (the owner's Oct-15 rule): the editor opens open, `#ed-workbench` is the
  one wrapper the collapse verb hides, and the verbs are words.
- **The height rule keeps its teeth**: cards open collapsed; the grid at rest lives hidden in
  `#mp-rest` with the sentence naming the verb. Measured (`tools/layout_measure.py`, laptop):
  stepper layout 2,024 px with the grid's step 5,335 px one click and a whole step away;
  open-grid-by-default surface 5,002 px in one block (the same defect, re-added — caught by the
  measure, fixed the same night); card-surface default **1,534 px tallest block, 2,629 px page,
  no over-three-viewports flag**, every region reachable without a step.

Assertions that lock it (`test_ui_contract`): the stepper is absent (`data-ed-step`,
`ed-steps-list`, `data-ed-step-name` all gone); the three regions and the workbench exist;
the Global card is pinned and hosts the grid with no write control on its strip; the card layer
is marked (`// SURFACE:` … `// SURFACE END:`) and touches no field internals (`innerHTML`,
`querySelectorAll`, `ed-field`, `.value =`, `fetch(`, `postConfig` all absent inside it); grid
ids stay singletons. The jsdom spec asserts the regions visible together, the surface note, the
pinned card's meta, and the collapse sentence; `#ed-global`/`#ed-clear-global`/`#mp-rest` left
the visible list on purpose — the cards' fields are claimed by the after-select value assertions
instead, which is the stronger claim (the values arrive, not just the markup).

The night's jsdom run caught the honest edge cases the redesign had to answer: a strip click on
an off-surface card was a guarded no-op — dead by the gate's own definition — so the strip
restores the card to the surface (built-ins open it for reading and say so); and the inert
attach-select exemptions moved to their new nth paths with the load-state reason restated.

##### 6.14.1z The frame, the name, and the tree (2026-10-18)

Owner Oct-18, on the card surface that replaced the stepper: three connected framing defects —
"frame the personality editor in some way and rather than a 'close the editor' make it so that
the editor is collapseable"; "the personality's surface ... has no visual clues that all those
parts are connected to the specific personality being edited"; "the model selection list ... has
no organization to it and frankly ai model names are overly long and repeative." The tree ask
named the metadata columns (`general.quantized_by / general.name / general.file_type`); what
shipped reads the same facts from the id itself.

**The frame.** The editor is one bordered object with a header band. The band carries the
personality's name (`#ed-frame-who`) and **one labeled collapse toggle** (`#ed-toggle`): the
button never hides — its label ("Collapse the settings editor" / "Open the settings editor")
and `aria-expanded` state which way it acts. The open/close verb pair is forbidden by the
contract test: a pair asks the operator to find a control that exists in only one state, and
the Now-strip lesson (§6.14.1q) says a script finds such a control in half the states. The
toggle hides exactly `#ed-workbench`, the Oct-15 single wrapper. The editor still opens open.

**The name.** `syncIdentity` is the one paint point: the frame header and the surface heading
(`#ed-surface-who`) both name the personality the cards belong to, repainting on every persona
change. Two controls describing the personality were already one fact; now two headings are,
too — both derived from `shown`, never a copy.

**The tree.** The tick list groups by the id's repository namespace — for this library that IS
`general.quantized_by` (bartowski, ggml-org, ISTA-DASLab, Jackrong, TheBloke, unsloth ...), and
the `:QUANT` suffix is `general.file_type` — so the grouping is zero-cost: no GGUF-header
sniffing at page render, the facts come out of the one store the page already reads. Items
show the short name + a quant tag chip, not the wall-long id; user-authored model
personalities group under **User-Defined (model personalities)** naming the file each alias
renames. A filter box hides items that match nothing and the groups that lose all their items
(a hidden checkbox keeps its tick — a filtered list is a view of the personality, not an edit
of it). Groups rest closed when the personality covers none, re-opening per persona, because
coverage is a personality fact; the pass runs after `loadIdentity` ticks boxes, else every
group collapses at boot.

**Measured.** First build: 2576 px editor — 23 files at one column = three viewports, the
harness flagged it. Fix: two-column item grid + the closed-rest rule → **1832 px tallest
block / 2924 px page, no flag** (the card-surface baseline was 1534/2629 with an unorganized
wall). Live-verified on :8090: toggle label+aria swap with workbench 1699→0→1699, filter
"iq3" → 5/23 items 3/11 groups, persona switch repaints both who-lines.

**Contract.** The collapse test is now the single-toggle test (asserts `ed-toggle`,
`aria-expanded`, label via textContent, **forbids** `id="ed-open"`/`id="ed-collapse"`); the
card-surface test requires `ed-frame`/`ed-frame-who`/`ed-surface-who`; routes assert the two
group kinds (`User-Defined (model personalities)`, `Local files` for repository-less ids);
spec `expectVisible` swaps `#ed-open` for `#ed-toggle` and adds the three new ids. UI suite 91
passed (pre-commit gate ran it inline on the commit).

##### 6.14.1aa Control affordance, tree cards, and the tick that said nothing (2026-10-18)

Owner Oct-18, two observations on the same page: **"Input fields, clickable sections,
dropdowns ... the background looks identical to the rest of the panel ... the borders look so
thin they are hardly noticeable"** (portal-wide), and **"i tried selecting one of the ai
models from the default 'safe' personality in the editor and nothing happened that i could
see."** The CSS diagnosis was exact: base.html painted fields `--panel` on `--panel` with a
1px `--line` edge, and the disabled state (`background:var(--line)`) was indistinguishable.

**Site-wide (base.html).** A control layer of the same palette: `--field:#0b0e13` (the canvas-
dark fill — fields visibly RECEDE into the panel) and `--field-line:#3d4a5c` at 1.5px on
inputs/selects/textareas (and the native option menu). Disabled fields recede further, disabled
buttons read disabled, labels wrapping disabled inputs get `not-allowed`, and `details>summary`
gets a pointer cue. The contract test that pinned the old rule
(`test_the_form_controls_wear_the_page_theme`) was updated to the new layer, and a new test
(`test_form_controls_read_as_controls`) pins the tokens, the thickness, and the cursors.

**"Nothing happened" had two real causes.** (1) `safe` is built-in: its list is derived from
the library and write endpoints refuse it by name, so the tick checkboxes are *disabled by
design* — and a disabled checkbox looked exactly like a live one. The refusal now speaks at
the click site (`#ed-attach-cards-note`, painted by `loadIdentity` beside the cards), not only
in the identity note a page-top away, and the disabled cards are dashed + muted. (2) For
EDITABLE personalities the general bug was real: `paintSurface` computed a card's on-surface
state from the **saved** attach list, so ticking a bare library model lit no card until save —
the tick looked dead. The rule is now the live store: `checked.includes(mid)` (+ the config/
alias clauses). A jsdom fixture drives it (select UI-Test-Work, click the `ui-tiny-coder` box,
the count sentence must read `2 of 3`), and the contract pins `checked.includes(mid)`.

**The tree is cards (owner's metaphor).** Each model is a card with its own 1.5px edge; the
checkbox stays in the DOM as the store the personality saves (drawn `position:absolute;
opacity:0` inside the card; the harness clicks it, keyboards reach it via `:focus-within`).
Selected = accent edge, accent tint, accent name (`:has(input:checked)`). A card arriving on
the surface **flashes once and scrolls into view** — the tick is clicked at the bottom of a
2900 px page and the card arrives at the top, out of view; boot is quiet (`paintSurface.quiet`)
so the page never scrolls itself on open.

**Alias management already existed** (`mp-new`/`mp-create` to save a global alias, `mp-save`,
`mp-delete` with a confirm) — the owner could not see it because the affordance bug made every
button and field read as decoration. That is the lesson: an invisible feature is a missing
feature, and the fix for "we need a means to create model personalities" was mostly CSS.
Measured: 1897 px tallest block / 2924 page, no flag. UI suite 93 passed (pre-commit inline,
twice). Commits `50e9cca` + `6f411cf`.

##### 6.14.1ab The editor edits things you can edit (2026-10-18)

Owner Oct-18, four asks: built-ins should not be editor options; verify the three stored
personalities are editable; the name field went missing and "max resident" reads like part
of "name"; and the alias rows in the tree wrap to mush.

**Built-ins left the select.** "If a personality cannot be edited like safe and default they
should not appear as options in the editor to be edited." Agreed: the deck is the reading
surface (its cards say "built-in, not editable in place"); `#ed-persona` now carries
`{% for c in cards if c.editable %}` plus the new mode. The seeding payload moved with it:
the page seeds the editable set, `/api/personalities` carries every personality's view
(built-ins' empty configs are asserted there). Five route tests re-scoped; the locked-pair
test now proves absence-from-the-select + the deck's stated status.

**The three stored personalities verified editable**: `Agentic-PI` (2 models, max 1),
`Flash-VSCode` (3, max 3), `VSCode` (3, max 3) -- `built_in False`, legal names, model lists
resolve, no corrupt columns. Live: each shows the editable click-site sentence.

**The name returns.** The field shows for every personality: read-only for stored records
(the name IS the record; the page states renaming means create-new), typed in new mode. The
label pair that read as one phrase -- `name` + `max resident` -- is now `name` + **max
resident models** (+ tooltip). No name field was deleted; it was new-mode-only by design,
and the design lost to the read.

**Alias rows stack** (owner's layout): the alias carries the row (bigger, bold), the model
it renames sits below in lesser text prefaced with `↳`, one card. The `renames X` string
became `↳ X` in the routes contract.

**The height tail.** Built-ins leaving the select made the boot persona a real one, and the
flow `loadPersona -> loadModel(first model)` has always auto-opened that model's card --
`safe` never showed it because its grid sat on a card among 17 strips. The auto-opened
grid measured **3978 px / three viewports**. Fix: `loadModel(name, openCard=true)` --
persona loads and boot pass `false` and `showCard(null)` parks the grid in the hidden rest
("pick a card, or pick a model below"); strips and the model select remain the opening
verbs (a fixture proves it). Strip figure lines now paint for every on-surface card, not
only the open one. Measured: **2067 px tallest block, no flag.**

**Fixture physics.** With boot on a stored personality, the jinja-toggle cases no longer run
on an empty built-in grid, and the harness cannot select the empty-valued "leave as is"
options (`pickOption` skips them) -- so the §6.14.1i clean-summary assertion moved to the
per-case moment where it is true (the seed-after-create case), and the toggles run last.
UI suite 94 passed (pre-commit inline). Commit `c493c10`.

##### 6.14.1ac The models library becomes a tree (2026-10-18)

Owner Oct-18: the llama.cpp datatable is "barely useable" -- the model id overflowed its
column on virtually every row and obscured the columns behind it; a tree with a filter, like
the personality editor's, levels from metadata (`general.quantized_by / general.name /
general.file_type`), one model per leaf, and the leaf's card shows the table's data with the
parenthesised postfixes as fields of their own.

**The id IS the metadata.** Repository namespace = quantizer, stem = model, `:QUANT` = file
type -- and that triple is the index's unique key, so three levels guarantee one model per
leaf; no fourth property was needed (the owner offered one "if the three are not enough").
`main.py` groups rows into `mv_tree` at render; zero GGUF sniffing.

**The surface.** Closed `<details>` repo groups → model subgroups → leaf BUTTONS (quant chip,
size, live, hub-cache). The filter hides leaves → subs → groups and opens what matches.
A leaf click opens the model's card under the tree: 18 fields -- model id, status,
router-registers-as, source, architecture, quantization, size (all shards), shards, ctx,
layers, attention interval, experts active/total, vision projector, ssm, capabilities
(badges keep their hover notes), chat template markers, peak RAM, priced-at-ctx, fits
(+ max-ctx estimate). The table's sticky-column CSS (the machinery that kept the id visible
while twelve columns scrolled sideways) is gone with the table; the spec's scroll/sticky
container entries went with it.

**Two facts the build surfaced.** (1) "Live" on this router means *registered*, and a
llama.cpp server registers every model it knows -- all 17 leaves carry the light. The boot
rule "open the live leaves' paths" therefore opened the whole tree; boot now opens nothing
(no card, note says how) and opens a card only when exactly one model is registered. The
contract forbids the pre-open loop. (2) Capability badges wear the same `.tag.ok` tone class
as the live badge -- live detection needed its own `.mv-live` marker.

**Tests.** Page tests re-scoped: the capabilities containment check slices the model's
`<section class="mv-card">`; the hub-cache marker counts in the tree region. The jsdom sweep
drives every leaf (click = card mutation), the filter, tabs, and the search XSS contract is
untouched. New contract: tree-not-wall (fields list, button leaves, `aria-pressed`, boot
rule, and the wall's markers forbidden). Measured: boot 800 px, opened-card page 2292 px --
the wall never measured under three viewports at this width. UI suite 95 + modelindex 81 +
smoke/modeladd/network green. Run spec `.scratch/models-tree/spec.md`; commits `efaf7f1` +
the two follow-ups.

##### 6.14.1ad Dense cards, one quant per branch (2026-10-18, round 2)

Owner round 2 on the models tree: the first cards were too thin — "almost none of the
details the prior table had." A card must be a detailed, organized breakdown of each
registered model that answers the operator's actual questions: how big is it, how much
memory will it likely use, what inputs/features does it support, is it MoE (how many
experts, how big), is it a split model, every property the portal stores; which models fit
resident *together*; which ones eat the disk. Minimum: the model's current id on the
router. And each quant of a model gets **its own branch** so "a model card never has to
share space with another model."

**The card is the branch body.** Tree: repo → model → `.mv-quant` branch (summary: quant
chip · size · fits · on-router · hub-cache); opening the branch IS selecting the model,
and the card renders inside it. An accordion closes the others — the cards are dense by
design. Six sections, 36 fields, every column the index stores: **on the router** (id on
router = `live_as`, registered status, fits + max-ctx estimate, peak RAM, priced-at-ctx) ·
**identity** (model id, gguf name, quantizer repo, quant, arch, source) · **size and
disk** (size, shards, sidecar bytes, path) · **memory** (weights, KV cache, total, peak,
trained ctx) · **architecture** (layers, full-attention interval, heads, kv heads,
embedding length, rope dim, vocab, MoE "n of m experts active per token", ssm) · **inputs
and features** (template markers, vision, FIM, self-draft layers, draft head, pooling,
capabilities with their hover notes). Group and model summaries carry **their total bytes
on disk** ("3 file(s) · 160.0G") — the disk question answers itself at the repo level.

**Queued futures (owner, recorded here so they are not lost):**
- *Router visibility control*: the "live" light dates from when personalities were meant to
  control which library models the router sees. Owner still wants that feature, "done in a
  way that doesn't damage the hf cache or llama model library folder" — i.e. a registration
  allowlist on the router side, never file moves, never cache writes.
- *Load/unload buttons on the card* when the llama router is online. Same safety frame as
  every router mutation: confirm-gated, Status/Settings pattern, and the in-session
  model-swap ban stays (swaps stay closed-session via tools/model_swap.py).
- Expert SIZE per expert is not in the index schema (`general.expert_length` is not
  extracted); the card answers "how many active of how many" today. A schema column +
  rescan would add it.

Measured: boot 781 px (all closed, 17 branches), one card open 2230 px. UI suite 95 +
modelindex green; contract rewritten to the branch shape (section list, 36 field labels,
accordion pin, byte totals, boot rule).

##### 6.14.1ae The quant is validated against two references (2026-10-18)

Owner: the two Swift Flash Next models both read as **IQ3** in the cards, while the maker
(and HF) name them IQ3_S and IQ3_XXS — refine the quant extraction, validate via multiple
references.

**The bug was a grammar bug.** The quant regex required DIGITS after the underscore, and the
IQ family's exact tags have letters: `IQ3_S`, `IQ3_XXS`, `IQ1_M` all collapsed to their
stem (and `Q8_0` to `Q8`). The ids stayed unique because llama.cpp's cache-id decoder
grabs the trailing token whole — so the tree separated the models while the cards (and the
`quant` column) said the same thing about both. `_QUANT_RE` now captures the export grammar
whole: IQ-family (`_XXS/_XS/_S/_M/_NL`), K-family (`_M/_S/_XL/_XXS/_XS`, the new K-quant
wave), unsloth decomposed (`UD-Q5_K_XL` — the quant column now says MORE than the id tag),
`Qx_0/1`, MXFP, plain widths — and `Qwen3.8` cannot fabricate a `Q3`.

**The second reference is `general.file_type`, and on this box it is a liar.** The sniff now
reads it through the gguf spec's id table (llama.cpp writes the NUMBER: the Swift IQ3_S
file declares 26 = IQ1_M, its sibling 23 = IQ2_S). Every GSQ-RCO-flavored GGUF here
disagrees with its filename (`Q8_0` files declaring `Q5_1`, `Q5_K_M` declaring `IQ2_XS`) —
exporter sloppiness, not portal bug. `_resolve_quant` validates the pair instead of merging
it: agreement is silent; a coarse filename tag is refined by the header (`IQ3` + `IQ3_S` →
IQ3_S); the K-family enum is coarse on its side (there is no Q4_K_M in the table — silence);
a flat conflict keeps the FILENAME — maker, hub, and the router's own id all use it, and the
shard sizes side with it (IQ3_S 83.7 G > IQ3_XXS 76.0 G) — and stores `quant_note`, which
the card renders as a warn badge plus a `header file_type` field, so the operator sees both
claims and why one won. Schema: `file_type` + `quant_note` columns through the ALTER
migration, drift-pinned INSERT widened.

`4.2bpw` (ThinkingCap's per-weight naming) is not in the tag grammar and reads as no quant —
truthful; inventing a tag from "4.2bpw" would be the same class of lie the headers are
committing. Tests: grammar table (19 names), resolver rule table, and the Swift pair
reproduced at cache shape with the lying header. Full suite 1190 green.

##### 6.14.1b The harness IS opencode — the portal hands over, the store is shared (2026-10-19)

Owner on the Projects page: the portal's own chat panel is "kind of useless as an agentic
harness" — the model announced a tool call and the loop just stopped, no errors, no
activity; and the project's session was invisible to a manual `opencode` in the folder.
"I was hoping opencode had a web interface ... and that you could just serve a real
opencode session from this server via the portal. Fix this!"

**opencode v2 ships the web app in the binary.** `opencode serve` serves it at the server
root — verified live: `GET :4096/` is the OpenCode SPA. The portal's chat panel is now
what it should have been: a *viewer*. Every project row carries **"Open in OpenCode"**
→ the host root (Basic auth: user `opencode`, the page names the key file, never the
secret); the app's connect screen takes the server address + the same password and shows
sessions grouped by project. Live-verified: the merged portal sessions appear there
("Opencode v2 test…", "Local full-duplex voice roleplay app").

**Why the loop halted** (measured, not guessed): the voice-roleplay session's last
assistant row carries two `shell` tool parts stuck at `running`, `finish: None`, and the
store's `permission`/`session_pending` tables are **empty** — the model spawned shell
commands at 13:36 that hung, the owner quit, and the **20:58 reboot** killed the host
mid-turn (`status=130`). opencode has no bash-timeout knob (v2 config schema checked),
and the store kept "running" forever, so the portal viewer showed hours of fake
activity. `transcript()` now reads the host's `idle` row *after* the last assistant row
as proof the turn closed → running tools become `stale` + the bubble names the way out
(the real web UI's interrupt/resume). An open turn's running tools stay running.

**One store** (the quibble, fixed): `XDG_DATA_HOME` isolation came out of the harness env
— its reason (v1 host + v2 CLI on one db: the v2 migration dropped `project_id` and broke
the v1 server, ref=err_4c90c294) is retired now the harness is v2. The comment in
`harnessctl.env_mapping` says: re-isolate FIRST if the harness is ever pinned to v1.
`tools/harness_store_share.py` merged the harness store into `~/.local/share/opencode`:
same-migration-set gate, backups, **named-column INSERTs** (measured: the two dbs order
`session_v2`'s columns differently — `SELECT *` would have written every value into the
wrong column), NULL-safe event_sequence max-merge (the first run crashed on a NOT NULL
and rolled back clean — the transaction boundary is why the store survived), no-clobber
storage dirs. Result verified: the harness host serves the merged sessions, the web UI
lists them by project, and `opencode session list` in ~/ai-projects/voice-roleplay now
shows both portal-launched sessions beside the folder's own history. `opencode attach
http://127.0.0.1:4096` shares the live state too (docs, v2).

Queued futures unchanged (router visibility allowlist, card load/unload) plus one new:
`opencode web --cors` opens the door to the portal *embedding* the web UI (iframe) later;
today's handoff is a link, and the web UI's own connect screen is the auth.

##### 6.14.1c The harness joins the fleet: LAN-reachable, and the Status page watches its versions (2026-10-25)

Owner ask: the portal-hosted opencode should be "like the other hosted services" —
reachable from other systems on the network, switchable from Settings — and the Status
page should carry an opencode tab that checks updates for the copy the Projects page
launches, plugins included, "ideally manage the plug-ins … and if there are any that
must be present for opencode to function on the portal."

**The bind was one env value away.** The wrapper already execs
`opencode serve --hostname "$HARNESS_HOST"`, and the Settings services table is
`SERVICE_CONF`-driven — so the fix was a registry entry, not a new mechanism. Harness
gets `modes: [loopback, all]` and the page + `set_bind` both enforce that list:
`lan` binds ONE interface address, which moves the endpoint off 127.0.0.1, and the
portal's client to the harness is loopback-pinned (§6.14.1) — a lan bind would cut the
portal off the host it launches sessions on. Auth rides along: `OPENCODE_SERVER_PASSWORD`
(HTTP Basic, user `opencode`) is generated by the portal and the wrapper refuses to
start without it, so the harness row reports `auth_kind: password`; `auth_state` gained
the password-missing warning, and `_exposure` passes `harnessctl.key_present()`. The
agent runs shell commands on this box, so the row carries the code-execution exposure
warning like the router's.

**Three versions are three facts** (measured, and the reason a naive "current: latest"
tab would have lied): the RUNNING host answers `/api/info` with 2.0.32; the DISK binary
is 2.1.4 — `opencode` self-updates (autoupdate is its default) under the running
service, which keeps the version it started with; and LATEST is the highest **v2 tag**
— GitHub `releases/latest` answers v1.19.4, the v1 line, so tags are the only honest
v2 channel. `app/core/harnessupd.py` reads all three plus the plugins and caches an
hour; every failed read is stated in the payload (a down host reads as "the host is
not answering", never as version zero, never as "up to date"). The tab's verbs are the
official ones: **Upgrade binary** = `opencode upgrade` (user-level, no root, touches
no running service), **Restart host** = the managed unit (ends a running turn; the
sessions/transcripts in the shared store resume), **Update plugin pins** = move pins +
re-seed + restart. All three are confirm-gated; the bind-restart gate in the bind
route generalized from llamacpp-only to a `_gates` dict so the harness restart asks
its own question.

**Plugins: pinned, and required for the portal: NONE.** The seed writes pinned
`plugins` specs (`opencode-search@1.0.0`); an unpinned spec means "whatever is latest
on release day", so the list lives in the `harness_plugins` setting (validated by
`_PINNED_PKG_RE`, unpinned refused) and the Status tab's pin-update verb is what
writes it. The live host answers `POST /api/plugin/check {"plugins":[…]}` with what
it LOADED (builtins + packages) — the tab reads that plus npm dist-tags. A corrupt
list cannot brick the host: the seed falls back to the pins and says so in the log.
The portal's own integration is the `portal-memory` MCP server in the config — not a
plugin; builtins cover the core (measured: websearch plugins ship builtin). Arbitrary
plugin *adding* stays a recorded future (supply-chain surface; the pin verb covers the
current need).

**Suite lessons from this run:** (1) the jsdom fixture schema is `{path, status, body}`
— a fixture appended with `{route,…}` has `path: undefined` and silently defeats the
ENTIRE fixture list: every page falls back to live endpoints and fails in panels far
from the change (jobbar/census/pulse all "saw ''" — 5 failures, all dashboard-family);
the diff that only ADDED 43 lines still carried the whole file's em-dashes as \u2014
escapes, so `ensure_ascii=False` is the spec.json serialization contract. (2) The
contract "action buttons ship disabled in the markup, unlocked by the panel loader"
applies to every dashboard panel: `oc-*-btn` ship `disabled`, and the unlock lines sit
literally in `loadOpencodeUpd` where the panel test can see them. (3) `/api/services`
mirrors `MANAGED_UNITS`, so a new managed unit is a test-pinned payload change.
Suite: 1216 collected, green.

##### 6.14.1d The image-forge research: an agent-facing toolbox has a real engine path (2026-10-25)

Owner opened the image-toolbox exploration (generation + editing + deterministic
helpers for the agent; the feature list is a research starting point, not an MVP)
and proposed two acceptance anchors: a header logo replacing the "AI Portal"
text, and a favicon set. The research round lands in **docs/IMAGE_FORGE_PROPOSAL.md**
(adjudication pending). Findings that shape the design:

- **The engine carries images** (source-verified, opencode v2): MCP tool results
  with image content parts become file attachments (`tool/mcp.ts toContent()` →
  `data:<mime>;base64`), and `tool-output.ts` gates by model modality — models
  that cannot receive bytes inline get a retained file path. So "return the
  visual state" works with zero protocol change whenever the box gains a vision
  model; today (all GGUFs `is_vision=False`, zero mmproj) the loop closes on
  `inspect` numbers + ASCII previews, not pixels.
- **Exact pixels by construction**: diffusion gen is legal only at 16-multiples;
  the toolbox accepts exact px, generates at the nearest legal size, and the
  Pillow/numpy bench (the ComfyUI venv already ships them) delivers the exact
  target. OpenAI's API solves the same pain with fixed presets; the bench is
  our preset. **Alpha is data, not luck**: chroma-key gen (magenta) + numpy
  Lab-ΔE keying + premultiplied resize + despill, with `inspect` probes
  (alpha coverage, fringe count, border purity) as the numeric eyes;
  Qwen-Image-Layered (native RGBA layers, Apache-2.0, ~45 GB peak on-demand —
  not downloaded) is the phase-2 research gate.
- **Prior art reads as verbs**: Stability's ops (`inpaint`, `outpaint`,
  `remove_background`, `replace_subject`), OpenAI's alpha-zero masks +
  `background:"transparent"` + quality dial, Firefly's iterative passes. The
  proposal: ONE MCP server (stdlib stdio like `kb_mcp`), nine tools, canvas =
  manifest + version files (undo = pointer; harness restarts kill in-memory
  state — the journal's own lesson), fixed workflow templates (the plugin
  supply-chain lesson), ComfyUI on-demand with `/free` after and confirm-gated
  start against the 108 GiB aperture.

##### 6.14.1e mmproj truth: the router has been seeing all along, the library table was blind (2026-10-25)

Owner noticed (correctly): the HF cache carries `mmproj-Swift-Qwen3.8-Flash-Next-BF16.gguf`
beside the session model's quant, while the portal's facts said vision everywhere was
false. Measured the chain end to end:

- **llama.cpp auto-pairs HF-cache projectors by substring.** `common/preset.cpp`:
  the cache loader scans the snapshot dir, `is_mmproj_file` = filename contains
  "mmproj", and sets `LLAMA_ARG_MMPROJ` on the preset. The router journal proves it
  ran: `load_model: loaded multimodal model, '…/mmproj-Swift-Qwen3.8-Flash-Next-BF16.gguf'`
  (loaded on every model start since Oct 20), and `/v1/models` reports the live
  model's `architecture.input_modalities: [text, image]`. **The session model IS
  vision-capable today.** No new launch parameters are needed: the filename
  mismatch (base-model naming, `mmproj-Swift-…` vs quant `Swift-1.5-…-IQ3_S`)
  is irrelevant to llama.cpp's substring rule — and the per-model INI accepts any
  llama-server arg key (`key_to_opt`), so `mmproj = <path>` is a legal
  personality-section override, by format, today.
- **The portal's cache scan already pairs them too**: `modelindex.scan_cache`
  ranks sidecars by directory depth (`_best_sibling` handles the root-level
  mmproj beside per-quant files — the same naming case). Live read: both Swift
  quants return `has_mmproj=True, has_vision=True, sidecar_type=mmproj,
  mmproj_bytes=907543520`; 7 of 19 cache rows are vision.
- **The blindness was scope**: `INDEX.json` is the *library* table (N3,
  `scan_library` over `~/ai/models/llama`), and cache models are not in it.
  Reading that file (as owner and as this session did) says "is_vision=False
  everywhere" — true of the library, false of the box.

**Queued (portal, small run)**: surface the cache-scan's vision facts in the
models tree the way the library rows show them (the card template already renders
`vision: mmproj` from `has_mmproj/sidecar_type`; the tree's quantizer→model→quant
branch is INDEX-driven, so cache rows need the same field path), add the
`mmproj = <path>` line to the personality editor grid (validate: file exists,
sniffs as projector; override semantics = "replace the auto-pair"), and show the
live `input_modalities` beside the disk fact. No launch-argv plumbing needed —
measured that llama.cpp owns it.

**Image-forge consequence** (IMAGE_FORGE_PROPOSAL §1, rewritten): the visual
feedback loop closes in-model from day one (MCP image tool-result → file
attachment → pixels; source-verified engine path). The gate is memory, and it is
measured: ~10 GiB MemAvailable with the ~40 GB IQ3_S resident — qwen-image
(~32 GiB pipeline) and z-image-turbo (~22 GiB) do not fit beside it. Binding
contract on the design: single queued job, pre-load gate (refuse with printed
arithmetic), `POST /free {"unload_models":true,"free_memory":true}` after every
job, idle-stop ComfyUI, short-lived bench subprocesses. First feasible pairing:
a forge session on `Qwopus3.5-4B-Coder-GGUF` (its snapshot carries
`mmproj-F32.gguf`, ~5 GiB resident) → z-image-turbo fits with margin. The gate
refuses with exactly that sentence.

##### 6.14.1f The box measured at full load: quant pipelines, the real footprint, and the embed-model myth (2026-10-25)

Owner asked three research questions (quant-tuned generation/editing pipelines;
what the portal stack actually costs; whether unloaded-RAM-from-MCP-services
buys a generation) and guessed the full coding loop needs a second machine or
a 27B primary. Measured all of it (IMAGE_FORGE_PROPOSAL §1b now carries the
tables):

- **Quant tuning is real and current**: GGUF diffusion quants for exactly the
  models on this box (city96/QuantStack/unsloth): qwen-image-edit Q4_K_M
  **13.1 GiB** ("excellent quality, widely recommended"), Q3_K_M 9.8 ("good",
  dynamic quant keeps first/last blocks Q8), TE Qwen2.5-VL-7B GGUF Q4_K_M
  **4.4 GiB** (vision tower included; Qwen2.5-VL, not Qwen3-VL). Needs the
  ComfyUI-GGUF custom node — a supply-chain exception decision, recorded.
  The fp8 pipelines on disk cost ~33 GiB; the 2511-Q4 path ~22-23;
  z-image-turbo (on disk, zero downloads) ~22. HiDream-E1.1: 22 GB + 14 GB TE
  stack for 768² — skipped, no advantage.
- **The full-box audit**: router unit anon 16.5 GiB (IQ3_S loaded: KV+compute
  buffers; the 40 GiB model itself is **file-backed mmap** — the kernel evicts
  it under pressure, trading inference speed for RAM), portal 0.5, harness 0.3,
  desktop ~4, **ai-embed: inactive, 0 GiB — the RAG embed model was never the
  hog** (owner's hypothesis, disproven by measurement; `kb_mcp` degrades to
  FTS + vectorize queue while it is down). Swap: /swap.img 7.9/8 G — pressure
  history of the 40 G model, not a leak. MemAvailable swings 10→35 GiB with
  load state — the forge gate must read MemAvailable + swap fill, never
  `free`-`used`.
- **The answers**: (1) a z-image-turbo generation passes the gate **today**
  (22 G + 8 G safety < 35 GiB MemAvailable) with IQ3_S resident, zero
  downloads; qwen-image fp8 (~33+8) is refused by ~6 G. (2) Lazy-load +
  idle-eject adopted for the embed sidecar (portal setting, minutes) —
  safe today because kb_mcp already queues vectors. (3) The full loop runs on
  **this** machine as 27B-Q8 primary (31 G file-backed; the 27B snapshots
  carry their mmproj) + qwen-edit-Q4 (~22 G) ≈ 57 G of 122 G. A second
  machine buys speed, not feasibility.

Queued: embed sidecar load-on-first-request + idle-eject setting; ComfyUI-GGUF
node exception decision + 2511-Q4 download gate (proposal §6 #6/#7); swap-fill
observation (watch, not act).

##### 6.14.1g The idle second box slots into the forge as a remote backend (2026-10-25)

Owner revealed the second machine — 7950X3D, 64 GB DDR5, **RTX 2080 Ti (11 GiB,
Turing)**, Windows 11, often idle — and asked whether it can contribute compute,
skeptically. It fits the forge with almost zero architecture cost: the engine
backend is the ComfyUI HTTP API, so the remote is **a config value** (endpoint
URL) — workspaces, bench, gate arithmetic all stay local; the gate reads the
remote's `/system_stats`. The fit (proposal §1c): z-image-turbo fp8 (~10-11 GiB
peak — the fast path), qwen-edit **Q3_K_M GGUF** 9.8 GiB fits via ComfyUI-GGUF,
and qwen-edit **Q4 13.1 GiB fits by hybrid block-swap** — ComfyUI's built-in
block-swap is exactly the owner's "MOE hybrid, minimal swapping" pattern, in
diffusion form. 2080 Ti facts that bound the table: 11 GiB (not 12), fp16
tensors fast, fp8 weights dequant to fp16 (no fp8 compute). Security line:
ComfyUI is unauthenticated — remote bind = LAN with a Windows firewall rule
scoped to 192.168.1.228; same trust domain the harness joined on Oct-25, not a
new opening. Test protocol T0 is zero-cost (portable ComfyUI + ~12 GiB of
downloads + two admin firewall commands): measure s/gen and peak VRAM running
the forge's own frozen templates against it; the gate gets measured remote
numbers. Side probe recorded, no commitment: a Qwen3-30B-A3B-class MoE under
64 GB RAM hybrid on llama.cpp/Windows — a t/s benchmark answers "what is that
box worth as an LLM peer" without touching the portal's router rules.

##### 6.14.1h Qwen-Image-2.1: the owner's find collapses four pipelines into one (2026-10-25)

Owner flagged `Qwen/Qwen-Image-2.1` ("strangely small compared to prior
versions") and asked if the generator+editor+transparency claims are real.
Verified (HF card, docs.comfy.org, Unsloth, community GGUF guides): the
smallness is a **new architecture** — 7B visual component, 32 single-stream
DiT layers, mixed-granularity attention + prefix-KV reuse — not a shrunk 20B.
One model: text→image, instruction editing (10 references, **circles /
painted annotations / separate masks**, identity preservation), **native RGBA
generation + transparent-layer editing + subject extraction**, improved
typography; native to 2048²+, guidance multiples of **32** (VAE 16×).
ComfyUI supports it natively from day one — official templates + **Turbo
8-step** checkpoints; the official package ships **int8_convrot** diffusion
and TE files (the same family as krea2 on this box; Turing has int8 tensor
cores, so the int8 tier is fast, not just compact). Quant ecosystem: 156
repos, GGUF Q8_0 7.6 G, **Q4_K_M ~4.6 G**; Unsloth: INT8 > FP8 by LPIPS,
Q4+Q4-TE runs CPU-only in 12-16 GB RAM.

Forge consequence (proposal §1b/§3.3/§3.4/§1c/§6#7): the unified engine the
forge was going to assemble from gen+edit+Layered+chroma-keying is one
download — ~27 GiB (base int8 + Turbo int8 + Qwen3-VL-8B int8 TE + 2.1 VAE)
for ~15-17 GiB per pipeline, Q4 tier ~11-12, **fits the 2080 Ti at ~10-11
peak**. Native RGBA becomes the primary alpha path with the bench probes as
verifier (coverage/fringe/histogram get numbers before a canvas accepts a
model's own alpha claim; the magenta-key pipeline stays as the independent
comparator + photo/legacy fallback). Per-model legal rounding joins the
template registry (16-mult for the 20B/z-image family, 32 for 2.1). License
caveat recorded: qwen-research (non-commercial) — fine for this personal
box, lands on the inventory license line. Layered (~45 G) and 2511-Q4 drop
behind 2.1 in the download queue.

##### 6.14.1i The links rule + the Windows forge kit (2026-10-26, overnight batch)

Owner's two overnight asks: (a) stage everything the image forge's Windows
backend needs into a folder, automated with PowerShell as far as is safe, and
(b) make every link the portal serves reachable **by the system clicking it** —
a name (halo.local) over a flat IP where resolvable.

**The links rule (measured, then enforced).** The portal's payload URLs are
loopback because the services that consume them (agent configs, harness
baseUrls, probes) live on this box — those stay loopback **by design**. The
defect was the BROWSER jump-offs: a page opened over halo.local handed out
`http://127.0.0.1:4096/` harness links, and a LAN browser follows that to
itself. The rule, implemented in `main.py`: `_link(request, port, path)`
builds every browser-facing URL from the **host the client used** (by
definition reachable by that client), wired into the projects payload
(`harness_web`), the dashboard hero (router web/api via `router_web`, ComfyUI
via `comfy.link`), `/api/settings/services` (`this_host_url` per service), and
the router apikey status (`web_ui_url`). Pinned by
`test_jump_off_links_echo_the_clients_host` (network, computes ports —
earlier tests move them) and `test_browser_jump_off_links_echo_the_clients_host`
(contract: the dashboard ships **no** literal loopback jump-off href).

**deploy/windows-forge/** — the whole Windows half of §1c in one folder,
SFTP-copyable to RainbowBlock.local: `model_files.json` is the SINGLE model
inventory (the Qwen-Image-2.1 int8_convrot + Turbo + Qwen3-VL-8B int8 TE + 2.1
VAE hf rows; the Q4_K_M optional tier; the z-image-turbo bring-up trio marked
`copy_from_halo` so nobody re-downloads what the Halo box already owns);
`install_forge.ps1` (git/Python ≥3.10 via winget when missing, ComfyUI clone,
ComfyUI-GGUF — the one accepted custom node, §6 #4 — venv + CUDA cu124 torch
(sm_75), HF downloads driven BY the inventory, idempotent, prints the SFTP
copy list it cannot do itself); `start_comfyui.ps1` (`--listen 0.0.0.0
--disable-smart-memory --preview-method none` — the memory contract's
Windows twin); `firewall_forge.ps1` (self-elevating, admin; inbound 8188
allowed **only** from the portal IP — ComfyUI answers unauthenticated, the
rule is the gate); `register_autostart.ps1` (user-level AtLogOn task — the
GPU lives in the desktop session); `verify_forge.ps1` (torch-CUDA, engine
reply, VRAM, every inventory file). README.md is the owner's step list. The
Portal side consumes the endpoint as a config value (§1c) when P0 lands.

**Suite repairs the full run surfaced (1212 green, 5 red → all green):**
1. `docs/IMAGE_FORGE_PROPOSAL.md` was never in `kb.DOC_ORDER`, so the
   proposal's own `§3.4` citations dangled — the citation test is exactly the
   guard that catches this; registered the file, and fixed two real dangling
   refs that named sections that never existed (3.4 / 2.4 for Layered →
   `§1b`, the download gate that actually discusses it).
2. `OPENCODE_CONFIG_DIR` leaked from the operator's shell (it is in
   `~/ai/config/harness.env`, and `seed_cache_home` prefers the env over the
   redirected HOME) — three seed tests accepted a seed their gate exists to
   refuse. conftest now drops the variable at import, beside the home
   ownership block; the seed tests pass with the leak supplied on the command
   line as a witness.

##### 6.14.1j Help becomes a two-pane documentation browser with a friendly front door (2026-10-26)

Owner's overnight brief: re-organize Help as the classic docs browser — narrow
collapsible outline left, document viewer right, search easy to find; the dev
documents stay (they are good) but belong in the outline's correct context,
visible when selected; and the whole thing must read as a friendly welcome for
a first-time, non-expert user. A future run (declared, not this one): make the
other pages feel like a tool, not a text-heavy web form.

**The corpus gained the layer it never had.** All 13 indexed documents were
engineering — a newcomer's first search landed in the plan journal. Two new
docs lead the registry now: `START_HERE.md` (welcome, the pages in use-order,
five minutes of useful things, how search and citations work) and
`GLOSSARY.md` (every machine word in plain sentences — GGUF, quantization,
context, KV cache, mmproj, slot/swap, personality, router, harness, MCP —
each pointing at the page, the setting key, or the deep section that owns it).
Glossary terms are slug sections on the settings-reference precedent
(`/help#gguf`), so anchors are stable and collision-free; friendly docs sit
first in DOC_ORDER, which also decides `/help#<bare number>` precedence.

**The page is a browser, pinned where it was measured.** `.kb-app` grid: 300px
rail + viewer, both scroll independently (the outline is a tree scanned
top-to-bottom; the viewer is a document). Groups and documents are native
`<details>` — keyboard and screen reader for free, JS only enhances. The
search box is pinned at the rail top: typing filters the outline live, Enter
posts the server-ranked search (`/help?q=` stays deep-linkable). Reference &
history is one collapsed group — the owner's "visible if selected like any
other link" is `open: False`, never hidden. Deep links (`/help#6.12.22`,
per-key settings anchors) land in the viewer and the rail reveals the owning
document (aria-current + forced expand). The rail collapse persists
(localStorage), Escape closes the section, the sanitizer allowlist survives
untouched (pinned). The multi-column ban from PORTAL_POLISH 9 travels with
the redesign: the pin now targets `.kb-outline` and keeps the measured proof.

**kb learned two things.** `outline()` is the group tree (group → documents →
sections; a doc no group claims is appended, never dropped — the outline is a
reading order, not a filter). And `search()` dropped question words and
counts short terms at word boundaries: "what is a slot" used to score on
"is" inside "this" and land in the journal; now the glossary answers.

**Measured, the honest way (P2 run, .scratch/help-browser/).** The packaged
design-pipeline machine gates are not installed on this box (router/validator
scripts absent — only SKILL.md + references), so the run ran in prose
artifacts with the limitation recorded; no machine verdict claimed. The
fixture taught two rules worth keeping: (1) the ui sweep probes EVERY
`a[href]` in the DOM, hidden included — a permalink with a placeholder href
of "/help" is a dead control, and (2) a control probed twice must answer
twice: the permalink is now a toggle that reveals/copy/confirms the stable
address on every click. And `_slug` turns "you'll" into "you-ll": an anchor
is what the slug function says, not what the prose suggests — the anchor
tests caught it before the page shipped.

Pins moved with the design (contract tests encode decisions; the owner
changed the decision): the TOC-scroll pin retargets `.kb-outline`, the
outline-of-documents pin now asserts group→document→section, and new pins
hold the two-pane grid, the rail-top search, the collapsed reference group,
the sanitizer, and the welcome card's anchors resolving. Full suite green
(see the commit).

##### 6.14.1k Queued directives: projects-launch model default + the inventory unification session (2026-10-27)

Owner queued two things; neither is built, both are decided direction.

**Projects-launch default model rule (owner, Oct-27).** New projects should
default to the **currently loaded personality**, and its model chosen by:
first **agent-capable model currently loaded and available** ("agent" =
a model able to use tools — the real minimum for a coding harness); when the
personality defines a model that is unloaded, fall back to the **largest
defined agent model that fits in available RAM**. Trigger evidence: a raw
`create_session` binds the *host default*, which points at an IQ3_XXS
registered-but-not-resident — the turn died as a provider 400 "model is not
loaded" in the transcript with no guidance. The personality path dodges this
today (launch-through-card pins a real model); the rule generalizes it.
Dependencies: an agent-capability field on model rows (chat template with
tool support) and the fit math for "largest that fits" — the same data model
the next item plans. Do not bolt on ad hoc.

**Inventory unification — a collaborative session, not an agent plan (owner,
Oct-27).** Manage together: the HF hub cache (low-effort library), the
llama.cpp model library in the portal's file structure, the model db, and the
personalities library. Owner wants research + real cost/benefit balancing in
the session. Evidence collected the same day, filed here:

1. `ai/state/llama/INDEX.json` persisted `files: 20` (library) and
   `hf_cache: 0` while the router served **20 cache models** — the cache view
   exists at runtime (`scan_cache` pairs mmprojs) but the durable artifact is
   library-only. The two views are real and unreconciled.
2. The agent's self-report of its own model was **wrong by family-name
   inference**: "resident 27B" — the 27B repos exist on disk as *separate*
   files. Sniffing the file the router actually serves: arch `qwen4exp`,
   size_label `512x56B`, 83.74 GB IQ3_S pair, ctx 262144; the paired mmproj
   declares `clip` + `has_vision_encoder = true` (449M), audio not declared.
   A name-derived label attached a size belonging to other files to this one.
3. The same file carries a declared-vs-named quant collision (header
   `IQ1_M`, filename `IQ3_S`) — known to `modelmeta` ("the maker names them
   so"), a live example of why name-derived facts are quarantined.

Early rule worth adopting independently (cheap): **every model fact on every
surface is sniffed from the file (`modelmeta.sniff_gguf`), never derived from
a name, and declared-vs-named conflicts are displayed, not silently
resolved.**

Same day, shipped: `~/ai-projects/legal-ai-plan/` — CONTEXT.md (distilled
grounding discussion), PLAN.md (219-line vendor-neutral case-assistant plan
written by a forked opencode session on the resident model), COVER-SHEET.md
(what to ask the builder for, non-builder language). The plan's Tier gates,
linter mapping, and golden-query moat are the discussion's ideas made
handable.

##### 6.14.1l Remote compute relays — the borrowed-machine contract (2026-10-27, design-refined, not scheduled)

Owner raised the partner's MacBook Pro (M4 Max class, 128 GB): a future
service like the image forge's remote backend, but a **protected llama.cpp
router** for sub-agents, extension of portal abilities, and model testing the
Halo box cannot host beside a resident agent — first named use: the
ai-voice-chat **roleplay tournament**, harness+judge local, the candidate
model chain (e.g. Flash-Next IQ3_S, an 83.74 GB pair) resident on the Mac,
engine tuning co-evolving between rounds. Owner's hard requirements: idle
unload and **system "in use" detection** — a borrowed machine is not a
server, and the human always outranks the job.

The idea is written up as `docs/REMOTE_COMPUTE_PROPOSAL.md` (registered in
the kb corpus, Reference & history): the **relay contract** — one shape for
every borrowed machine (ComfyUI-on-Windows is its image instance):
authed OpenAI endpoint (llama-server `--api-key`; Comfy stays keyless +
firewalled), control plane (status/load/unload, the `/free` and
closed-session-swap disciplines applied remotely), **remote fit-gate
arithmetic** (`vm_stat`/`memory_pressure`/swap, never `used`), idle contract,
and the **etiquette tier** — presence gates (green = locked/idle, amber =
active, red = call/battery; unknown fails toward the human), a reserved
memory floor for the partner, scoped `caffeinate`, pressure auto-preempt, a
menu-bar kill switch, job-start notifications, quiet hours, and a
zero-file-access boundary (the relay agent is outbound state only). The
"two AI connections for one prompt" question answered honestly: no harness
fuses two backends per completion; the real shapes are **model-as-tool**
(MCP `consult`) and **sibling sessions** (character sessions bound to relay
models, judge on Halo's resident model — the projects-launch model binding
measured 2.0.29 makes this the zero-harness-change path). P0-P3 phased; §8
is the consent checklist the owner and partner fill together before anything
is built — the partner's floor GB, tiers, quiet hours and kill switch are
config values, not manners.

##### 6.14.1m The sparkline became a sliding time window (2026-10-28, overnight)

Owner's read of the router hero: the generating number beside the sparkline
resized and shifted the canvas every time its digits changed — "ruining the
feeling of motion" — and the chart's poll-cadence jumps made time feel
stepped. The llama.cpp report rate (~3 s per slot's print_timing, 5 s bins)
stays as is; the *window* is what moves now.

The row is the chart: the `.tps-row` draws the sparkline full width, and the
big generating number moved to the totals row (kept, `font-variant-numeric:
tabular-nums`, so its digits stop jittering the layout). `#tps-now`, its
setText line, and the UI-sweep probe all survive untouched.

`drawPulse` is now time-x arithmetic on the **client's clock**: rlog anchors
journal stamps to epoch and every payload carries `time.now`, so the client
extrapolates now = payload clock + elapsed-since-arrival (`PULSE_POLL`), and
a 1 s redraw loop (`every()`, hidden-tab-skipped like everything on this
page) slides the window continuously between 2.5 s polls. The grid spans
exactly `[now − window, now]` — points older than the window are culled by
construction. Idle slots and breaks in a slot's line draw as **zeros** (the
band falls onto the stack below it; a gap never reads as "stopped" as well
as a zero does). The owner delegated the in-flight-no-sample case: a slot the
log still believes is generating (`state` generate/prompt, `age_s` ≤ ~9 s —
three print cadences) carries its last reported rate to the right edge as a
**dashed tail** — "still generating, about this fast" — and past that
staleness nothing is extrapolated; the line falls to zero and the silence
keeps its label ("last sample Ns ago"). Falsities the chart must keep
telling: sample times, staleness, and the estimated stretch of line drawn
dashed, never solid.

The jsdom driver in `tests/test_hero.py` grew the behaviors this contract
promises — `zero-fills-idle-slot`, `carries-active-rate-to-edge`,
`culls-old-points` — beside the pre-existing `names-the-silence` honesty
pin, and `test_the_chart_row_is_only_the_chart` pins the layout (number out
of the chart row) and the sliding-window machinery as source markers.

Two artifacts the owner spotted in the live chart, both fixed the same night:

*The left gap that shrinks then pops.* The time grid started at the first bin
boundary **after** the window's left edge (`Math.ceil`), so the polyline's
oldest point sat up to one bin (5 s ≈ 7 px) inside the canvas: the stub
slid right with `now` and vanished when the grid advanced — "pops back to
maximum size." The grid now starts one bin *beyond* the left edge
(`Math.floor`) and the drawing is clipped to the canvas rect: the oldest
point scrolls out of view off-screen, and the point behind it is already at
the edge. Nothing to pop. The driver pins it as `extends-past-left-edge`
(a point drawn at x < 0, and `clip` called).

*The line disappears for a frame.* Not the client's clocks: the snapshot
builder's exception path (app/router.py) serves `rlog.parse([])` — a
zero-bin pulse indistinguishable from true silence — so one journal-read
hiccup flickers the chart to "no token activity." The client now keeps the
last payload that carried samples (`PULSE_KEEP`/`pulseForChart`): an empty
payload draws the last *real* bins a few seconds longer — aging out of the
window at the real clock, zeros behind them, the silence label ticking —
and the 1 s redraw loop runs off the kept payload, so the transient hole
never reaches the screen. Honest by construction: kept data is real data,
staling visibly.

The owner then asked for the chart to carry more than one metric and named
the traps: prompt t/s runs ~20x generation (scale crush), multi-thread
series can get busy, the grey corner text is nearly useless, and the right
endpoint dips look fake. What the pane grew:

* **Prompt-rate channel, end to end.** rlog now bins prompt rates on the
  same 5 s grid (`bins[].pr` — the progress and prompt-eval prints already
  carried them, time-stamped, un-binned), and the bins_out loop spans both
  channels (a prefill-only window is a window). The client draws a dashed
  slate-blue band normalized to *its own* peak in the bottom strip — the
  label says "prompt N t/s · own scale". Sharing the generation axis is the
  crush the owner named; normalizing per metric is the honest answer.
* **Events as shapes on the time axis.** The journal's own attention list
  (swaps, stalls, cache pressure, context limits) maps `age_s` back to bin
  time and draws small ticks along the top edge: red = error, amber = warn,
  blue = notice. They draw during silence too — a warning while the router
  is quiet is the one that matters.
* **The right edge, per the owner's own rule.** Generating → assume
  generation continues (the dashed carry-forward). Prompt/queued/idle →
  assume zero. The prompt-phase drop that looks like a "fake dip" is the
  true zero — prefill produces no tokens — and the band above now *says
  why*: "the thread is reading, not dead". The guess is replaced the moment
  the real rate prints (the redraw cycle), so no fabricated curve.
* **Labels earn their space.** Top-left = generation scale (peak t/s),
  top-right = the prompt band's scale (only when drawn), bottom = the time
  axis ("−7:00" … "now", "last sample Ns ago" when quiet). The old prose
  status line is gone; the shapes say it.

Driver pins: `marks-events` (one tick, in-window, amber; the out-of-window
error skipped), `draws-prompt-band`, `labels-both-scales`, `labels-the-axis`.
rlog pins: prompt bins on their own channel, separate from generation, and
the prefill→generate transition landing in one bin on both channels.

##### 6.14.1n The slot bars learned flow, not fake progress (2026-10-28)

Owner's read of the lane board: the bars "change color, rapidly fill and
empty" and a slow, honest fill is almost never seen. That is the physics of
what is measurable: **only the prompt phase has an end the log reports**
(prefill progress %), so prompt is the one bar that slowly fills — and it
only exists for a few seconds per request. Generation has no knowable
length (the sampler stops when it stops), so its bar parks at 100% and the
width transition animates 0→100→0 on every state change: the rapid
fill/empty. The old generation "animation" was a background-position
shimmer, nearly invisible at 7 px.

The board now speaks three states honestly: **prompt fills** (the real
percentage), **generating flows** (a soft band of light crossing the full
bar every 1.5 s — "tokens moving right now", low contrast, peripheral),
**queued flows slower and dimmer** (2.6 s — waiting is not streaming),
**idle rests** empty, and **stalled stops the flow** — the state claims
busy while the log went quiet, so the bar rests inside its warning ring.
`prefers-reduced-motion` kills the band entirely (the lane board's
kill-switch rule was already in the sheet; the flow joined it). Pinned by
`test_states_that_cannot_be_measured_flow_instead_of_faking_progress`.

##### 6.14.1o Motion pass: ten frames, a monotonic clock, an eased scale, ticks in front (2026-10-28)

The owner watched the new window slide and named what was missing: the
animation read as too few frames, the graph *jerked backward* — most
visibly when a slot changed state — and an error in the router log never
appeared as a tick on the chart's timeline. All three were real:

* **Frame rate**: the slide ran at one redraw per second — 1.3 px per
  frame at 560 px / 7 min. At ten frames a second (`every(100, …)`,
  hidden-tab-skipped as always) the same speed is a sub-pixel glide.
* **Reverse jumps**: `now` is extrapolated from the payload's log stamp —
  the timestamp of the **last logged line**, behind a 2 s snapshot cache.
  When a slot changes state, a fresh payload can carry a stamp *older*
  than the last frame's extrapolated `now`, and the whole graph slides
  right for a frame. The clock is now **monotonic** (`drawPulse._lastNow`):
  the window may pause, never rewind.
* **Hidden event ticks**: ticks drew *before* the stacked areas, and a
  peak-height fill painted right over the top band — an error hidden
  behind the data it explains is worse than no shape at all. Ticks now
  draw last, in front, slightly larger (3×9 px), in the silent window too.
* **Scale pops** (the same state-change jerk, vertically): the raw peak
  can double in one frame when a slot starts, and every y rescales
  instantly. The scale now **eases** toward its target (20 % per frame —
  a slow breathe at redraw cadence).

Cross-frame state lives on the function object (`_lastNow`, `_peakShown`)
so the extracted jsdom driver can reset it per scenario. New pins:
`now-never-rewinds` (an older stamped payload parks the sample in the
past, not at the right edge), `smooths-the-scale` (peak label steps
between the old and new targets), `ticks-in-front` (fillRect ordered after
the fills). The monotonic clock has one honest cost: during a long silence
the window pauses at the last extrapolated edge rather than advancing on
phantom time — which is exactly what the silence label is for.

##### 6.14.1p The Windows forge kit, audited against the authorities (2026-10-28)

Before the owner runs the kit on RainbowBlock.local, every artifact was
verified against what it claims to touch:

* **Manifest**: the real repo is `Comfy-Org/Qwen-Image-2.1` (the kit said
  `…-comfyui`: 404). File names confirmed by Comfy's own docs and repo
  pages; sizes corrected (TE 9.4 GB). The unsloth GGUF repo puts
  `qwen-image-2.1-Q4_K_M.gguf` at the **repo root** (4.2 GB, not 4.6) and
  the GGUF loader reads `models/gguf/` — the manifest gained a `dest`
  field and the installer moves the landed file there.
* **`/system_stats` schema** (verified live against the local ComfyUI
  0.39, which was started, probed, and idle-stopped again): the response
  is `system.comfyui_version` + `devices[]` with `vram_total` — the kit's
  verify script read `system.cuda.version`/`devices[0].total`, which do
  not exist, so the VRAM check could never pass. Fixed; README wording too.
* **Installer hardening**: `hf.exe` (venv) is the downloader, with the
  module entry as fallback; every pip/git/download step checks
  `$LASTEXITCODE` (PowerShell 5.1 does not stop on native exit codes —
  a failed 24 GB download used to flow on silently); the gated-license
  case (qwen-research: sign in + accept on the repo page + `hf auth
  login`) throws with those exact steps named.
* **Firewall**: the elevation hop now forwards `-RuleName` (idempotent
  delete-then-add survives a custom name).
* The `copy_from_halo` sources were confirmed on this box at the
  manifest's sizes (12.3 / 5.2 / 0.33 GB).

##### 6.14.1q The Machine detail panel is gone; the nav gained resource lights (2026-10-28)

Owner: retire the collapsed Machine detail panel, but the portal must
*throw* a visible warning when the box nears running out of RAM or disk —
visible without taking any action. The design follows the panel's flaw:
it hid the machine's condition behind a click and painted its disk/GPU
cards once at page load (the "sampled HH:MM:SS" contract tests existed to
apologize for exactly that). The replacement needs no apology:

* **The lights live in the nav** (base.html) — every page, visible on
  load, no panel to open. Two items: `RAM n G free` (MemAvailable, the
  same metric the fit gate uses) and `DISK n%` (the *worst* of root and
  the ai filesystem).
* **Healthy is quiet grey; the light is the alarm.** Warn: amber dot
  glowing, RAM under 12% free / disk over 85% used. Critical: red glow,
  RAM under 6% / disk over 92%. No green "all is well" noise — the eye
  learns to ignore a light that always says fine. `aria-live` announces
  state changes, the tooltip carries the arithmetic.
* **It rides `/api/metrics`** (server cache 10 s) on a patient 20 s
  global loop, visibility-guarded like every other loop on the portal.
  Zero server changes — `memory()` was already MemAvailable-based and
  `disk()` already covered both filesystems; the panel just never let
  them speak.
* The panel's JS, markup, and spec pins are gone; three contract tests
  that pinned panel-era promises were re-homed honestly (the dead-`/metrics`-link
  guard survives; "rendered-once numbers need sample times" now passes by
  the numbers' absence; the poll-budget test asserts the dashboard never
  re-acquires a metrics poll and the global strip stays at 20 s). A
  mutation pair pins that the lights *keep* updating (a frozen alert is
  the one that fails exactly when needed).

##### 6.14.1r The portal got a face: the HALO mark (2026-10-29)

Owner asked for retro-future logo candidates drawn as SVG. The process
mattered as much as the product:

* **Palette discipline** — every colour is the portal's own (`--acc`,
  violet, ok/warn/bad, chrome greys). A logo that borrows the chrome's
  colours belongs to the chrome.
* **True transparency** — no background plate in any file; every glow is
  a gradient fading to `stop-opacity="0"`, so the mark sits on the
  portal's surfaces, not a card of its own.
* **A vision loop** — render each SVG in the portal's own Chromium
  (Playwright), *look at the raster*, feed fixes back. It caught six
  defects invisible in source: two clipped taglines, a dead gate mouth,
  scanlines that read as venetian blinds, edges that vanished on light
  backgrounds, a broken-image first sheet (the sheet sat in /tmp away
  from the SVGs).
* **Naming** — the owner ruled that "AI Portal" is a functional
  description, not a brand; text in a logo is free creative. Four
  lockups were offered (HALO / GATE·228 / LOCALSKY / SPARKDRIVE); the
  owner chose the Halo Aperture mark with the name **HALO** — the
  machine's own name (Strix Halo), the ring the mark already drew.

Shipped: `web/static/portal-mark.svg` (nav), `favicon.svg` (the same
mark), `portal-wordmark.svg` (mark + HALO). The shipped wordmark has
zero font dependency — H, A, L are hand-built rects and an even-odd
triangle, O is a stroked ring, so the letterforms are the same geometry
language as the mark and render identically on every machine. The nav
brand is the 26 px mark + the name; the head carries the SVG favicon.
A contract test (`test_hero.py`) pins the wiring and forbids `<text>`
or `font-family` from ever returning to the shipped assets. Candidates,
preview page, and the final contact sheet live in
`~/ai/logs/portal-logos-20261028/`.

##### 6.14.1s The hero titles wear their services' official marks (2026-10-29)

Owner: "llama.cpp router" and "ComfyUI" are visually weak next to the
dense hero panes. The fix is borrowed recognition, honestly framed:

* **The official marks, unmodified.** The llama.cpp icon comes from
  `ggml-org/llama.brand` (CC BY-NC 4.0; its BRAND-USAGE.md explicitly
  permits embedding the mark "for the purpose of identifying or
  referring to" the project, with attribution). The ComfyUI icon comes
  from comfy.org; Comfy's trademark rules say *use as provided — do not
  distort, recolor, or outline* and forbid implying endorsement. Both
  files live in `web/static/third-party/` byte-for-byte (the only
  addition is an XML provenance comment), and `BRAND-LICENSES.md`
  records source, license and the compliance reading. A contract test
  fetches upstream when the network is up and fails if the served
  markup ever diverges — brand hygiene is enforced, not intended.
* **The framing is ours, legally and visually.** The marks sit beside
  text at a clean gap, never merged into it (llama.brand forbids
  combining the mark with other elements). The owner's ear for the
  "ComfyUI" repeat was right: the trademark rules also forbid naming
  our own product Comfy-flavored — HALO stays, and the pane is
  **ComfyUI · image forge**, house vocabulary since the forge kit.
  The contract test pins that a bare "ComfyUI" headline never returns.
* The vision loop did its usual shift-work: mock hero heads rendered in
  the portal's Chromium with the real assets and the real head CSS,
  three phrasing variants plus the bare control — the owner chose
  "image forge" from the render.

##### 6.14.1p+ The parse preflight ships in the kit (2026-10-29)

The owner asked for the parse-check one-liner as a real file:
`parse_check.ps1` now ships first in the kit's step list. It parses
every `.ps1` beside it through the PowerShell parser
(`PSParser::ParseInput`, reflection-only — executes nothing, works even
on a box whose PowerShell install is sick), prints per-line parse
errors, flags non-ASCII bytes, and exits with the count of failed
files. All three parse deaths the kit suffered on RainbowBlock.local
(ANSI mojibake, PS7 ternary, multi-line throw) are visible in seconds,
before an installer touches the disk.

A repo-side contract (`tests/test_windows_forge.py`) now keeps the
kit's hard rules true for every future edit: pure-ASCII bytes, no
PS7-only constructs, and the preflight present + wired into the README
steps. The contract earned its keep on day one: it flagged a
`IMAGE_FORGE_PROPOSAL ?1` fossil in `start_comfyui.ps1`'s header —
a `§` an earlier ASCII pass had scrubbed to a bare `?`, legal ASCII
that reads as mojibake. Fixed to `sec 1`. (README.md keeps typographic
prose — UTF-8, viewed in browsers; the kit's byte rule governs the
`.ps1` files 5.1 parses.)

##### 6.14.1p++ The preflight's own first run failed (2026-10-29)

The owner ran `parse_check.ps1` on RainbowBlock.local and it died:
"PSParser does not contain a method named 'ParseInput'". True — the
modern parse surface is
`System.Management.Automation.Language.Parser::ParseInput` (v3+, so
5.1); the legacy `PSParser` class has `Tokenize` only. Fixed, plus the
two property-shape traps that ride along: `ParseError` carries
`Extent.StartLineNumber`/`StartColumnNumber` (the legacy exception's
`Token.StartLine` does not exist), and interpolating the whole
`$PSVersionTable` in a string prints "System.Collections.Hashtable" —
print `$($PSVersionTable.PSVersion)`. The script now calls the modern
parser first with a `Tokenize` fallback in `catch`, and the contract
test pins the exact API surface (the lesson lives in a test, not in
folklore). Kit rule made explicit: `.ps1` files carry no `?`
characters at all — that is what makes the fossil-net (a `§` scrubbed
to `?` by an ASCII pass) safe to grep for.

##### 6.14.1t The Windows box gets its own agent (2026-10-29)

Owner's idea, and the right one: instead of the owner being the parse
loop, opencode runs ON RainbowBlock and thinks with the Halo router
over the LAN. Built as `deploy/windows-agent/`:

* **The provider is declared, not predefined.** opencode ships no
  llama.cpp provider and needs none — the router speaks the OpenAI
  wire. `opencode.json` declares `llamarouter`
  (`@ai-sdk/openai-compatible`, `baseURL http://192.168.1.228:8080/v1`,
  placeholder apiKey — the router is unauthenticated LAN infrastructure
  and carries no secret). The portal harness's own config on this box
  is the template, retargeted from loopback to the static lease IP.
* **The model list is a machine-safety artifact.** The agent gets the
  small coders (Qwopus 3.5-4B default, DeepSeek-Coder-V2-Lite,
  ThinkingCap-27B) that coexist with the resident model; the 84 GB
  IQ3_S MoE — the controller's own brain — is absent from the config,
  pinned by test: requesting it evicts the portal harness.
* **`AGENTS.md` is the session/instructions**: mission (bring up the
  forge, keep it up), kit steps, the hard rules earned by four failed
  runs (ASCII, 5.1 syntax, one-line throw, Language.Parser, run
  parse_check after every edit, never delete weights), and a
  definition of done (`verify_forge.ps1` all PASS + `/system_stats`
  answering).
* **The router is already LAN-bound** (`ss`: `0.0.0.0:8080`; LAN
  `/v1/models` answers 200) — no new exposure was created; the portal's
  amber hero pill has said "open to LAN" all along. `check_agent_link.ps1`
  proves reachability from Windows (TCP + model list) before any session.
* Honest trade-offs: the agent's brain is a small coder (fast, and it
  causes visible router model loads); deep reasoning stays in the
  portal with the resident MoE. The kit lane stays alive in parallel:
  tonight's install is one re-copy + the fixed preflight away.
