# AI Tool Management Portal

Web portal for managing, updating and configuring the AI tools on this
Framework Desktop (Ubuntu 26.04, AMD Ryzen AI Max+ 395, 128 GB shared RAM):
**llama.cpp**, **ComfyUI**, and the **huggingface** CLI/libraries.

Backend is Python (FastAPI, no front-end build step: Jinja templates + inline
JS, which is why the test suite drives the pages in a real DOM — see *Tests*).
The repo also holds the earlier environment-consolidation planning and migration
tooling that the portal is built on top of.

## Contents

| Path | What it is |
|---|---|
| `docs/ENVIRONMENT.md` | Full inventory of the machine: hardware, GPU, disks, every AI-related file/dir with sizes, the llama.cpp router's actual behavior (verified from source + live API), findings F1–F11, and prior-session script triage |
| `docs/PLAN.md` | Target layout (`~/ai/`), the phased migration plan (who runs what, risk, rollback), and the update-policy the portal will implement |
| `docs/UPDATES.md` | Which components self-update (apt) vs need portal/user action, ROCm↔llama.cpp↔torch version-coupling watch items, the health-pane data list, model-download flow, and the code-verified model layout rules (incl. draft/MTP naming) |
| `tools/aiportal.py` | Shared constants + helpers for the phase scripts |
| `tools/phase1_build_model_tree.py` | Build `~/ai/models/llama/` (one subdir per model, hardlinks, 0 extra disk) + validated `presets.ini` |
| `tools/phase2_move_repos.py` | Move `~/llama.cpp`, `~/ComfyUI`, `~/comfyui` (venv) into `~/ai/`; merge the unique workspace models; compat symlinks at old paths |
| `tools/phase3_rebuild_llama_cpp.py` | Rebuild llama.cpp (pinned commit, HIP+Vulkan, `~/vulkan` SDK) into a self-contained `~/ai/releases/llama.cpp-b<ver>/` + `~/ai/bin/` symlink farm |
| `tools/phase4_router_cutover.py` | **User-run** cutover: stop old router, install `ai-llama-router` systemd user service on the new layout, verify model list, write rollback instructions |
| `tools/phase5_cleanup.py` | **User-run** cleanup (explicit flags, dry-run default): stale build trees, old clones, `~/.local/bin` llama junk, LM Studio tree removal, home-script archiving |
| `archive/` | (created by phase 5) legacy scripts from prior AI sessions |

`02-install-llama-cpp.sh` at the repo root is the previous (bash) attempt at the
llama.cpp update flow. It is superseded by `tools/phase3_rebuild_llama_cpp.py`
but kept for reference.

## Current state (after phases 1–3, run 2026-08-30/31)

```
~/ai/
├── llama.cpp/                    # source repo (commit 539f24529)
├── build/llama.cpp-539f24529/    # build tree (rebuildable, disposable)
├── releases/llama.cpp-b10641/    # self-contained release (ldd-clean)
├── bin/                          # symlink farm -> releases
├──
 models
/llama
/
<13
 model
 dirs
>
/
 
#
 router
 layout
,
 hardlinked
,
 verified
├── comfyui/                      # ComfyUI repo + venv (was ~/ComfyUI + ~/comfyui)
├── config/llama/presets.ini      # per-model tuning (validated live on port 18080)
└── state/llama-releases.json     # release tracking
```

Phases 1–5 are complete: the router runs as the `ai-llama-router` user service
on the new layout (`:8080`, 13 local models + 1 HF-cache duplicate), and the
LM-Studio rollback copy has been removed. See `docs/PLAN.md` / `docs/UPDATES.md`
for the history and `docs/PORTAL_PLAN.md` for the portal design.

## The portal application (Phase A — built)

A Python/FastAPI web console that manages **llama.cpp**, **ComfyUI**, and
**Hugging Face**. Frontend is **HTMX + Jinja** (decision D1, no JS build); every
read is also a JSON endpoint so charts (CPU/RAM/GPU) are added by dropping a
`<canvas>` + a `fetch()` in a template — no re-engineering. Access is **LAN + a
single app password** (decision D2, no cloud auth). Updates are **advisory** for
now (decision D3) — the health pane surfaces apt/reboot/version drift and offers
actions; one-click execution is a later phase.

Layout — the repo is where code is *written*; `~/ai/ai-portal` is where it
*runs* (deployed, own venv, systemd user service):

```
app/
  main.py            # FastAPI app: auth, HTML pages, JSON API
  deploy.py          # python -m app.deploy — deploy / update / rollback the service
  core/              # paths · config(auth) · db(sqlite) · systemd · units (env file +
                     # wrapper + unit generation) · serviceenv · serviceconf · network ·
                     # llamakey · ratelimit · logsetup · updater · modeladd · modelmeta
                     # · rlog (router journal -> live slots/throughput)
  providers/         # llamacpp.py · comfyui.py · huggingface.py  (one class per tool)
  metrics.py         # /proc + rocm-smi host metrics (stdlib)
  router.py          # live llama.cpp router state (dashboard hero)
web/                 # templates/ (HTMX+Jinja) + static/htmx.min.js (vendored, LAN-safe)
deploy/              # install.sh (thin wrapper over app.deploy) + GENERATED_UNITS.txt
scripts/dev-run.sh   # source tree on :8091, next to the service
tests/               # 185 pytest tests, all on a throwaway AI_PORTAL_HOME
  ui/                # DOM harness (jsdom): drives every control on every page
```

Runtime state (gitignored, outside the repo): `~/ai/state/portal/portal.db`
(SQLite, WAL), `portal.auth.json` (0600, password hash), `PORTAL_PASSWORD.txt`
(0600, printed once on first run), `deployment.json` (deployed sha + history).

### Run it

```bash
./deploy/install.sh --dry-run    # exactly what a deploy would do
./deploy/install.sh              # deploy HEAD -> ~/ai/ai-portal, enable + start
./deploy/install.sh --status     # recorded sha + live unit state
./deploy/install.sh --rollback   # redeploy the previously recorded sha
./scripts/dev-run.sh             # source tree on :8091 with debug logging
./scripts/check-ui.sh            # the whole suite, UI included — run after every change
bash tools/live_smoke.sh         # end-to-end against the live router
```

First run generates a password and writes it to
`~/ai/state/portal/PORTAL_PASSWORD.txt` (delete after reading). To set your own,
put `AI_PORTAL_PASSWORD=...` in `~/ai/config/portal.env`. Pages: `/` (status),
`/models`, `/metrics`, `/settings`; JSON API under `/api/*` — full endpoint and
curl list in [docs/DEBUGGING.md](docs/DEBUGGING.md).

### Network access (who can reach these services)

Each managed service binds one of three ways, chosen per service on
**Settings → Network access** and stored in `~/ai/config/<service>.env`:
`loopback` (127.0.0.1 — this machine), `lan` (one interface address), `all`
(0.0.0.0 — the local network). The page shows the machine's real LAN address,
whether the port is *actually* answering on it right now (a TCP probe of the LAN
address is what distinguishes "open" from "loopback-only"), whether a restart is
still pending, and the `ufw allow …` command the firewall would need — the
portal has no root, so it never changes the firewall itself.

**Exposure is stated, not hidden:** the router and ComfyUI serve unauthenticated
HTTP, and ComfyUI can execute code (`/prompt` plus custom nodes). Opening them
is an explicit operator decision, warned about in the UI. The router can be
given an `--api-key-file` from Settings (shared `Authorization: Bearer <key>`,
stored 0600 under `~/ai/state/portal/`, never in an env file).

### Tests

```bash
./scripts/check-ui.sh              # everything, UI included (~1.5 min). After every change.
./scripts/check-ui.sh --ui-only    # the UI suites alone
./scripts/check-ui.sh --mutations  # inject known UI bugs: prove the suite can fail
uv run --with pytest pytest tests/ -q    # same as check-ui.sh, without the jsdom setup
bash tools/live_smoke.sh           # boots the app on :18090 against the live router
```

The portal has **no build step**, so nothing type-checks the wiring between a
template and the code behind it: a page can render perfectly and still have
buttons that do nothing. `tests/test_ui_integration.py` loads each page in a
real DOM (jsdom) from a sandboxed portal and drives **every interactable
element** — clicks, typing, changes, submits, all tabs — requiring per control
that nothing threw, no id lookup missed, no endpoint 4xx'd, and that the control
actually *did* something. Tab groups must be a bijection; destructive requests
must be confirm-gated **before** the request (an `alert()` afterwards is not a
confirmation); a control unlocked by a selection is asserted in that selection
(`selectCases`, and `then_click` to drive it against a known choice); a final
coverage pass fails if any visible control was never driven. `tests/test_ui_contract.py` is the fast static half (every inline
`<script>` must parse, every `onclick` must name a function that exists, every
`fetch` target must be a registered route, `expectText` requires live regions to
show *data* rather than merely exist (a slot board stuck on `—` has a working id
and tells you nothing), and the tab/panel junction in
`/static/tabs.css` holds — asserted as CSS text there, and measured through the
DOM by the harness, since a frame edge meeting its panel is geometry, not taste).

Running them cannot disturb the machine: the portal under test gets a temp
`AI_PORTAL_HOME`, `tests/ui/bin` on `PATH` (`systemctl`/`journalctl` are shims
that log instead of acting), a dead outbound proxy, and a harness that
intercepts service/update mutations and answers them synthetically. A test
asserts no `start|stop|restart` ever reached systemd. Debug a page by hand with
`uv run python -m tests.ui_sandbox dashboard` (or `--serve` to leave it up).

`./scripts/install-hooks.sh` installs a pre-commit hook that runs the UI suites
covering what you staged, so a template edit that breaks a `<script>` block
cannot be committed.

### What's next (see PORTAL_PLAN.md §7)

F (full health pane: apt/ROCm/SDK probes, disk, orphans, index freshness),
G (remaining frontend polish), H (RAG service: per-project stores, ingestion
jobs, MCP interface). Everything through G in the roadmap table — service
controls, the llama.cpp and ComfyUI updaters with rollback, the model manager,
the settings page, LAN bind modes, deployment and the UI test harness — is done.
