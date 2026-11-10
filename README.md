# HALO — the AI Tool Management Portal

**One desktop machine. All the AI. No cloud.**

A web console that manages, updates, and connects the AI tools on a single
AMD Strix Halo box: the llama.cpp router, ComfyUI, model files, projects,
coding agents, and the machines next door. The project began as a personal
challenge — build a fully functional Edge AI portal using only edge AI
tools, on the family's Framework Desktop — and the portal is planned,
written, and reviewed by coding agents running on the machine it manages.

**For the tour, the features, and the milestones, see the project
documentation → [`docs/index.html`](docs/index.html)**
(published: [GitHub Pages](https://carolynhudson.github.io/ai-portal/)
once Pages is enabled; the link works from the repo too).

This README is the technical document: requirements, installation,
configuration, tests.

## Requirements

| | |
|---|---|
| **OS** | Linux, systemd user services. The home is Ubuntu 26.04 on AMD Strix Halo (Ryzen AI Max+ 395, 128 GB); the portal's probes degrade honestly on other boxes. |
| **Python** | ≥ 3.14 (the deployed venv; `deploy/install.sh` creates it). stdlib + the pins in `requirements.txt`. |
| **Stack** | FastAPI, Jinja2, uvicorn, httpx, huggingface_hub, pydantic — pinned in [`requirements.txt`](requirements.txt) / `pyproject.toml`. No front-end build step; no node_modules. |
| **Services managed** | `llama.cpp` (`llama-server` router mode), ComfyUI, the opencode harness host. The portal runs as its own systemd user service and manages the others' units. |
| **Ports (defaults)** | portal `8090` · router `8080` · harness host `4096` · ComfyUI `8188`. |
| **Disk** | The portal itself is small; the model library under its management is tens of GB. State lives in `~/ai/state/portal/` (SQLite). |

## Install

```bash
git clone git@github.com:carolynhudson/ai-portal.git
cd ai-portal
./deploy/install.sh
```

The installer is a thin wrapper over `python -m app.deploy` (stdlib-only,
so it works from a fresh clone before any venv exists). It produces:

```
~/ai/ai-portal/                      exported git ref + .venv
~/ai/config/portal.env               launch parameters (bind, port, log level)
~/ai/bin/ai-portal.sh                generated wrapper the unit execs
~/.config/systemd/user/ai-portal.service
~/ai/state/portal/deployment.json    recorded deployment (for --rollback)
~/ai/state/portal/PORTAL_PASSWORD.txt  first-run password, mode 0600
```

Open **http://localhost:8090/**, log in with the generated password (the
installer prints its path; delete the file after reading). Useful installer
modes: `--dry-run`, `--ref <sha|tag>`, `--no-start`, `--status`,
`--rollback`.

### Run, watch, update

```bash
systemctl --user status ai-portal      # unit state
journalctl --user -u ai-portal -f      # live log (also ~/ai/logs/portal.log)
./deploy/install.sh                    # redeploy current HEAD
./deploy/install.sh --rollback         # back to the previous recorded sha
```

Deploys copy the checkout, so **commit first** — the commit is the release.

### Configuration

Launch-time variables live in `~/ai/config/portal.env` (shared with every
portal-managed service via `~/ai/config/ai-env.env`):

| Variable | Purpose |
|---|---|
| `AI_PORTAL_HOST` / `AI_PORTAL_PORT` | bind + port (default `0.0.0.0:8090`; the Settings page changes bind per service with the firewall lines shown first) |
| `AI_PORTAL_PASSWORD` | override the generated password (unit drop-in) |
| `AI_PORTAL_HOME` | relocate the `~/ai` tree (tests use a throwaway) |
| `AI_PORTAL_ROCM_ROOT`, `AI_PORTAL_APT_STAMP` | health-probe redirection |

The model layout the router and the portal share (`~/ai/models/llama/`,
one subdirectory per model, `presets.ini` sections as bindings) is
code-verified against llama.cpp and documented in
[docs/UPDATES.md](docs/UPDATES.md).

## Backup & recovery

The installer is box-specific by design (Ubuntu on the Strix Halo layout);
what survives a machine is the git history plus one archive. On a healthy
box:

```bash
./deploy/install.sh --backup      # -> ~/ai/state/portal/backups/portal-<ts>.tar.gz
```

The archive carries the state git cannot restore: the database (sqlite
backup API, safe on a live service), the launch envs, `presets.ini`, the
deployment record, and the portal password. **It contains secrets** — mode
0600, store it like a key, off the machine. Models are not in it; they
re-download under the `-hf` law. On the new box: clone, `install.sh`, then

```bash
./deploy/install.sh --restore /path/to/portal-backup-<ts>.tar.gz
```

which stops the service, backs up whatever is already here, lays the
archive down, restarts, and verifies `/healthz` reports the build.
`--uninstall` removes the service (unit/wrapper/env, backed up) and leaves
the state; `--rollback` redeploys the previous recorded sha.

## Tests

```bash
uv run python -m pytest -q            # the full suite: 1,366 passed, 5 skipped
uv run python -m tests.ui_sandbox status   # drive one page in a real DOM
./scripts/check-ui.sh                 # the UI contract (pre-commit runs this)
```

The suite runs against a throwaway home by construction: a home that
resolves to the operator's, or a database outside the suite's sandbox,
stops the run before it starts. See
[docs/engineering.html](docs/engineering.html) for what the UI harness
enforces (dead controls, confirm gates, viewport laws).

## Repository map

| Path | What it is |
|---|---|
| `app/` | the portal: `main.py` routes, `metrics.py`, `core/` modules (one per subject), `core/kb_mcp.py` (the mesh door's stdio twin) |
| `web/templates/` | Jinja pages, inline JS, the nav icon sprite |
| `docs/` | the shelf: journal (`PORTAL_PLAN.md`), surveys, proposals, research, and the published documentation (`index.html` + pages) |
| `deploy/` | installer, generated units, the Windows forge kit |
| `tools/` | phase scripts, probes, benches the laws lean on |
| `tests/` | the suite + the UI sandbox (jsdom harness, fixtures, spec) |

## Publishing this documentation

GitHub renders Markdown natively; the aesthetic documentation is the HTML
site in `docs/`. To publish it: **Settings → Pages → Deploy from a branch
→ `main` / `/docs`** — the site lives at
`https://carolynhudson.github.io/ai-portal/`, every link inside it is
relative, and the same files browse from any clone.

## License

**CC BY-NC 4.0** — free for non-commercial use, with attribution to
Carolyn Hudson. It is a work in progress, tested only on the author's own
machine, and provided as-is: install at your own risk, and keep a backup
(`--backup`). Full terms: [`LICENSE`](LICENSE). Model weights and
downloaded components carry their own licenses. Screenshots in the
documentation are scrubbed of private data at capture (addresses, account
paths, and token-shaped text are masked before the pixels exist).

*Written by Carolyn Hudson with the help of the machines it manages.*
