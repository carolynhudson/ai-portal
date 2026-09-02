# AI Tool Management Portal

Web portal for managing, updating and configuring the AI tools on this
Framework Desktop (Ubuntu 26.04, AMD Ryzen AI Max+ 395, 128 GB shared RAM):
**llama.cpp**, **ComfyUI**, and the **huggingface** CLI/libraries.

Backend is Python. This repo currently contains the *environment
consolidation* phase (analysis + plan + migration tooling); the portal service
itself is the next milestone.

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

Layout (repo holds code; runtime data stays in `~/ai`):

```
app/
  main.py            # FastAPI app: auth, HTML pages, JSON API
  core/              # paths.py (~/ai constants) · config.py (auth) · db.py (sqlite) · systemd.py
  providers/         # llamacpp.py · comfyui.py · huggingface.py  (one class per tool)
  metrics.py         # /proc + rocm-smi host metrics (stdlib)
web/                 # templates/ (HTMX+Jinja) + static/htmx.min.js (vendored, LAN-safe)
deploy/              # ai-portal.service + install-service.sh
tests/test_smoke.py  # auth + endpoint smoke tests (pytest)
```

Runtime state (gitignored, outside the repo): `~/ai/state/portal/portal.db`
(SQLite, WAL), `portal.auth.json` (0600, password hash), `PORTAL_PASSWORD.txt`
(0600, printed once on first run).

### Run it

```bash
cd ~/ai-projects/ai-portal
uv sync                          # create .venv + install deps (already done)
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8090   # dev
# or install as a systemd *user* service (binds 0.0.0.0:8090, LAN):
./deploy/install-service.sh
```

First run generates a password and writes it to
`~/ai/state/portal/PORTAL_PASSWORD.txt` (delete after reading). To set your own,
put `AI_PORTAL_PASSWORD=...` in the unit's `Environment=`. Then open
`http://<host>:8090`. Routes: `/` (status), `/models`, `/metrics`, plus the JSON
API under `/api/*` (`/api/status`, `/api/models`, `/api/metrics`, `/api/health`).

### Tests

```bash
uv run --with pytest pytest tests/ -q
bash tools/live_smoke.sh        # boots the real app on :18090 and exercises the live router
```

### What's next (see PORTAL_PLAN.md §7)

Phase B (service start/stop/logs from UI, with the router-rollback safety model),
C (llama.cpp build→stage→verify→swap updater + rollback), D (model manager +
HF-command downloads + presets generator — deriving ctx from GGUF metadata, not
hard-coded caps), E (ComfyUI service), F (full health pane).
