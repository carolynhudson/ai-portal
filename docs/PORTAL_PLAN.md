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
