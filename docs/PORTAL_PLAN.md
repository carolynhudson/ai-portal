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
| `models` | the **available set** — a symlink farm of model dirs the router may see at all (§2 layout). Everything not in it cannot be loaded, cannot be OOM'd by, and does not appear in clients. |
| `models_max` | how many may be resident simultaneously — a hard router limit, not a hint |
| per-model preset | backend, context policy, extras (§6.12's rubric) |
| `budget` | the memory plan: the **heaviest `models_max` combination** in the set must fit with headroom. This is enforced by *nothing* but the portal: loaded weights are anonymous and unreclaimable (§6.12), `fit` is blind to other residents, and a model's footprint runs ~12 GB above its file size (§6.12.2). Measured, not assumed. |
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

**Deferred, deliberately:** the apply path. `presets.ini` on this box still holds only the
validated `[*]` globals; N4 has not changed what the router reads. Writing is a separate,
gated decision that goes through the restart job and records a snapshot first.

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

## 5. Libraries & stack (Python 3.14, uv-managed)

| Concern | Choice | Why |
| --- | --- | --- |
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
| **N (proposed)** — the model table | Finish the gguf-indexer port: `model_index` with real GGUF metadata, the memory estimate (weights + KV + runtime, hybrid-attention aware), incremental scan; smoke-test selection, models page and router personalities all read it (§4.9) | "smallest full model that fits in 9 GB" is a query, not a heuristic; the smoke test cannot fail on model choice twice; presets gain per-model backends from a diff'd, generated file |
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
