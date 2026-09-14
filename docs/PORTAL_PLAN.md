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

**Committed & verified:** Phases A–E + router hero + Phase I (Settings) +
**network exposure** + **Phase J steps 1, 4, 5, 6, 7** + **Phase L (UI
integration tests)**.
Green: 151 tests (`./scripts/check-ui.sh`), of which 9 drive the real pages in a
DOM (see §6.6), plus the tab/panel geometry contract (§6.7) and the stored-build
controls (§6.8).

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
|---|---|---|---|
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

## 1. Verified current state (post-cleanup)

| Component | Location | State |
|---|---|---|
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
|---|---|
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
  repoint+restart (`LlamaCpp.rollback`). Endpoints: `GET /api/updates/llamacpp`,
  `POST /api/updates/llamacpp` (`target`/`restart`/`smoke`/`dry`),
  `POST /api/updates/llamacpp/rollback` (confirm required), `GET /api/jobs/{id}`.
  The ComfyUI updater (Phase E) reuses `updater.start_job` with its own pipeline.
- **ComfyUI**: `git fetch` -> checkout tag/commit -> `pip install -r
  requirements.txt` in the venv (torch pinned, not re-downloaded unless
  required) -> restart service.
- **Hugging Face**: downloads per 4.2; cache pruning as a managed job.
- **Advisory only (never auto)**: apt deltas touching `amdrocm*|amdgpu*|
  mesa-amdgpu*` + kernel (reboot-pending flag), Vulkan SDK newer-version check
  (lunarg URL, known pattern), ROCm/torch version drift → offers
  "rebuild llama.cpp" / "sync ComfyUI venv".

### 4.5 Health & status pane
Inputs (UPDATES.md §"health pane"): apt pending set, reboot-pending, ROCm
version vs llama.cpp build vs ComfyUI torch, Vulkan SDK installed + latest,
llama.cpp local vs `origin` (git ls-remote), deployed release id, router
uptime, ComfyUI commit/venv versions, disk sizes (`~/ai`, HF cache,
rollback), model-index freshness, orphan files, broken hardlinks.

### 4.6 State & history (SQLite)
`~/ai/state/portal/portal.db` (WAL mode):
`tools`, `releases` (tool, id, path, commit, sdk, created, active),
`jobs` (kind, target, status, log), `downloads` (repo, files, placement),
`models` (id, dir, role files), `model_files` (path, size, sha256),
`presets_history`, `events` (audit log of every mutation).
`llama-releases.json` stays as the machine-readable release registry the
phase-3 tooling already writes; the DB indexes it.

### 4.7 Personalities (named router states) — user direction, 2026-08-31

A *personality* is a named, pre-planned router state for an activity (e.g.
"coding", "rag-qa"): a generated `presets.ini` plus a models dir (symlink
farm) exposing **only** the models that state is allowed to load. The memory
budget is computed from GGUF metadata (weights + ctx KV) up front, so a
personality can never OOM the box. Target scenario: VSCode-style multi-model
setups (agentic + autocomplete + RAG loaded at once) — that needs
`--models-max > 1` planning, so router start/stop (Phase B) and the GGUF
memory math (Phase D) must land before personalities become switchable from
the UI.

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

## 5. Libraries & stack (Python 3.14, uv-managed)

| Concern | Choice | Why |
|---|---|---|
| Backend | FastAPI + uvicorn | async (long jobs, SSE log tail), typed, small |
| Validation | pydantic v2 | request/config models, preset-key validation |
| HF | `huggingface_hub` | download + cache prune APIs (already on system: 1.2.2 / 1.29.0) |
| GGUF metadata | `gguf` | parses headers for inventory/matching (gguf-indexer already proves it) |
| DB | stdlib `sqlite3` | zero-dep, single file in `~/ai/state/` |
| HTTP probes | `httpx` | lunarg SDK check, `git ls-remote` alternative |
| Presets | `configparser` | the INI format the router reads |
| Frontend | **D1 — open** | see §8; recommendation below |
| Reuse | `tools/aiportal.py` → `app/core/paths.py`; gguf-indexer code → `app/models/` | don't rewrite proven logic |

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
- `tests/ui/mutation_check.py` — injects fifteen realistic regressions (renamed
  id, deleted function, unparseable block, renamed endpoint, removed panel,
  missing confirmation ×2, wrong request field, emptied handler, and six tab
  geometry breakages, §6.7) and requires all fifteen to fail the suite with a
  message naming the right thing. This is what keeps the harness honest: two of
  the first nine were missed when it first ran, and both were holes in the tests.
  It edits the real `web/` tree temporarily, so it refuses to start unless that
  tree is clean, and restores after every mutation.
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

* A Vulkan SDK path and a *host metrics* link sat stranded under the ComfyUI
  hero card, outside its border. The nav already links Metrics, and the SDK path
  is inventory. The line now carries only the reboot badge, and only when a
  reboot is pending; the SDK inventory (Vulkan, ROCm, HIP) lives in the GPU
  Drivers / SDKs tab, where the Vulkan explanation already was. The same SDK path
  was also a row in the llama.cpp panel — it appears once now.
  `test_sdk_inventory_lives_in_the_gpu_tab` asserts it (against the *template*:
  a running ComfyUI log line can legitimately contain the word "vulkan").
* The llama.cpp panel listed every build as its own row with its own rollback
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

## 7. Build-out roadmap

| Phase | Deliverable | Exit criteria |
|---|---|---|
| A | Skeleton: uv project, `app/core/paths.py`, FastAPI, `portal.db`, read-only status page for all three providers | `systemctl --user start ai-portal`; status page shows router/models/releases |
| B | Service manager: start/stop/restart for router + ComfyUI with confirm dialog, rollback note, audit event, per-bubble controls | stop/restart router from UI with confirm; rollback note written; ComfyUI controllable once its unit is installed |
| C (done, 2026-09-03) | llama.cpp updater: full build→stage→verify→swap→restart pipeline + rollback button | one click bumps a release (no-op path proven by `tools/live_smoke.sh`); old release restorable |
| D | Model manager: inventory, gguf metadata, presets generator, HF-command downloads, dedup/GC | paste an `hf download` cmd → model appears in router with correct preset |
| E | ComfyUI provider: status, venv sync, systemd service | start/stop from UI; update runbook verified |
| F | Health pane: apt/ROCm/SDK probes, disk, orphans, index freshness | all UPDATES.md inputs visible |
| G | Frontend polish per D1 | — |
| H | RAG service: per-project stores, ingestion jobs, MCP interface (§4.8) | two demo projects searchable, zero cross-project hits; start/stop from UI |
| I (done, 2026-09-11) | Settings page: shared runtime env (`ai-env.env`, e.g. ROCm `LD_LIBRARY_PATH`) + per-service host/port with live validation (TCP range, cross-service collision, system-wide binding probe, privileged warning) and external-access toggle (loopback ↔ 0.0.0.0) | change a port in the UI → validated live → persisted to env file → explicit restart applies it; shared var visible + editable for all three services |
| **K (done, 2026-09-13)** — network | Bind **modes** (loopback / lan / all) per service, live reachability probes, firewall state + scoped `ufw` hints without root, exposure warnings, optional router `--api-key-file`, login throttle; all three services on the generated env→wrapper→unit launch pattern | portal + ComfyUI answering on `192.168.1.228` (verified by LAN-address HTTP from this box), router env/unit prepared with argv verified against the live process, `pending_restart` reported until it is restarted |
| J (mostly done, 2026-09-13) | Project reorg + deployment model: source repo vs `~/ai/ai-portal/` deployment, idempotent install/update script, portal as a real user service, logging system (levels via launch param), documented debug tooling (§0). Remaining: `app/main.py` route split, `tools/` migrations move | redeploy-from-git-SHA works and **verifies the running sha**; `dev-run.sh` runs alongside the service on 8091; log level changeable without code edits |
| **L (done, 2026-09-13)** — UI tests | Integration tests for every interactable element: jsdom harness driving each page against a sandboxed portal (`tests/ui/`), static UI contracts (`test_ui_contract.py`), `scripts/check-ui.sh` as the single entry point, pre-commit hook, and a mutation check that proves the suite can fail | every control on every page is driven and must have an effect; tabs are a bijection; destructive requests are confirm-gated before the request; coverage gate fails on any visible control never driven; 15/15 injected regressions caught; found 3 real UI bugs (§6.6) |
| **L.1 (done, 2026-09-13)** — tab geometry contract | Tab/panel junction defined once in `web/static/tabs.css`, asserted as CSS text and measured through jsdom's cascade | tabs sit on the panel's top edge with one baseline that opens only under the active tab; 6 geometry mutations prove it; §6.7 |
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
- **D7 — Presets.** Portal edits `presets.ini` directly (validated + snapshotted),
  or only shows proposed diffs for you to approve?
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
8. **Phase J** — see §0 for the step list and target layout.
