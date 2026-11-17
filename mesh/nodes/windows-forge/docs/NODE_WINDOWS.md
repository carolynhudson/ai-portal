# Windows node (image forge) -- install guide

A Windows node is a GPU box that joins the mesh as a ComfyUI image forge:
it serves generation over HTTP (ComfyUI), an MCP tool door (forge-mcp),
an LLM router (llama.cpp), and the mesh door (meshd), all gated by the
Windows firewall (portal-IP-only; ComfyUI has no auth). This kit is the
node implementation of NODE_CONTRACT: interfaces are uniform across the
mesh, implementations are per-OS, and this folder is the Windows one.

Layout: scripts at the top, `tools/` for health/smoke/bootstrap helpers,
this guide plus README.md (kit overview), ARCHITECTURE.md (service map),
BUILD.md (llama.cpp build law).

## Prerequisites

- NVIDIA GPU with CUDA (the reference node is a 2080 Ti, 11 GiB VRAM;
  Turing sm_75 is the compiled llama.cpp lane). 32 GiB RAM is comfortable
  for the router's idle law.
- Windows 10/11 with PowerShell **5.1** (the shipping shell). Every `.ps1`
  here is pure ASCII: 5.1 reads scripts as ANSI, so UTF-8 punctuation
  arrives as mojibake and breaks quoting. Check with `tools/parse_one.ps1`.
- git, Python 3.10+, and (for meshd) Rust stable. `install_forge.ps1`
  installs git/Python through winget and STOPS on purpose so you re-run
  it with the new PATH in place.
- A portal (HALO) that knows this node: the enrollment bundle
  (`cert.crt`, `cert.key`, `root_ca.crt`) and the bring-up model trio.
  See `docs/mesh-forge-enroll.md` at the repo root.

Run everything as `powershell -ExecutionPolicy Bypass -File .\<name>.ps1`.
After editing any `.ps1`, run `.\parse_check.ps1` -- every file must print
`PARSE OK` before it runs. That is the parse law; it costs seconds and
cures whole install runs.

## 1. Bring up the engine

    # copy this folder to C:\forge\kit on the node, then:
    cd C:\forge\kit
    .\parse_check.ps1
    .\install_forge.ps1 -IncludeGGUF        # ~35 GB from public HF, no login
    .\install_forge.ps1 -IncludeGGUF        # re-run after a winget install

Copy the bring-up models from the portal box (the list is
`model_files.json` under `copy_from_halo`, source `halo.local` over
SFTP, ~17 GB). Never delete model weights -- disk is cheap, deletion is
the owner's call.

The memory contract for the 11 GiB tier is
`--disable-smart-memory --preview-method none` with the bring-up trio
(z-image-turbo bf16 + qwen3vl 4b fp8 + the matching VAE). The engine
dies mostly from VRAM pressure; read the log tail (`C:\forge\logs\`)
before theorizing.

    .\start_comfyui.ps1                     # engine, port 8188
    .\start_forge_mcp.ps1                   # MCP door, port 8096

## 2. Gates and autostart

The firewall IS the security model: each listener is reachable only from
the portal's IP. Keep the IP in the rule, never a name (name rules freeze
the IP at add time). These self-elevate.

    .\firewall_forge.ps1                    # 8188
    .\firewall_forge_mcp.ps1                # 8096
    .\firewall_forge_meshd.ps1              # 8092
    .\firewall_llama.ps1                    # 8082

    .\register_autostart.ps1                # ComfyUI at logon, hidden
    .\register_forge_mcp_autostart.ps1
    .\register_forge_router_autostart.ps1
    .\register_forge_meshd_autostart.ps1

The router starts COLD and self-manages (`--sleep-idle-seconds 900`,
`--models-memory-margin 16384`): a personal PC gives RAM/VRAM back when
idle, nothing busywheels. It runs `--models-max 1`: exactly ONE model is
resident, so the model in `opencode.json` is the resident brain and any
other catalog id costs a swap. Do not edit `opencode.json` by hand -- it
is generated on the portal box.

## 3. llama.cpp

`.\update_llama.ps1` is the build law: fetch upstream, build at a ref
(MSVC, CUDA arch for this GPU), install to a self-contained release dir,
verify the tool set, then `-Swap` with a `.bak` of the previous release.
On demand only, never scheduled; a full CUDA build takes a long while, so
run it in the background. Rollback = restore the `.b<build>` backup.

The update writes a version record (`C:\forge\llama\version.json`, from the
ref it built) and meshd reports it at `GET /healthz` under `"llama"` --
that field is how the portal compares llama.cpp versions across nodes. No
file = `null`, so a node never claims a build it does not run; a hand-made
install records itself with `.\tools\llama_version.ps1`. After any update,
re-measure (`.\tools\bench_llama.ps1` runs the BUILD.md lanes: GPU home lane
+ CPU lane); BUILD.md keeps the measured baselines.

Smoke the router with `.\tools\router_smoke.ps1 -Ids flint` (load time and
tok/s for named ids). The router runs `--models-max 1`, so naming ids
matters: a broad smoke forces model swaps and costs minutes each.

## 4. Join the mesh (meshd)

Build the node daemon from the repo (source lives in `mesh/meshd`;
Windows needs a Rust toolchain -- `tools/install_buildtools.ps1`,
`tools/rust_env_setup.ps1`, then `cargo build --release` in `mesh/meshd`).
Deploy with `.\tools\deploy_meshd.ps1`: it stops the listener, backs up the
exe, replaces it, restarts the task and proves the new version answers --
that order, because Windows refuses to overwrite a running image:

    .\tools\deploy_meshd.ps1
    .\register_forge_meshd_autostart.ps1     # task at logon
    .\start_forge_meshd.ps1                  # 8092, MCP, ONE-token bearer

The door reads the node registry at `C:\forge\etc\services.json` (rows,
ports, auth mode, idle policy -- it names token FILES, never values). The
kit carries `services.json` as the template to install there.

Enroll per `docs/mesh-forge-enroll.md` (bundle in `C:\forge\etc\mesh\`,
`meshctl enroll --name <node>`). Three laws the cheap way:
(1) copying cert files is NOT registration -- the hub keys by
fingerprint, so the enroll handshake must run; (2) the enroll source must
differ from its destination (`meshctl` copies INTO `%USERPROFILE%\.mesh`,
source==dest = `os error 32`); (3) `X509Certificate2` holds a handle on
the cert file -- `.Dispose()` before moving it.

Leaves are short-lived (24 h) and expiry IS revocation, so the portal
mints a fresh one nightly. `.\mesh_selfheal.ps1` pulls the staged bundle
when the local leaf has < 6 h left and re-enrolls it; it is wired into
the router and meshd launchers, so every start self-heals. A failed heal
is survivable by design: the node keeps serving on the token file
fallback. Never print tokens or key material in posts, logs, or commits.

## 5. Verify

    .\verify_forge.ps1                      # torch-CUDA, VRAM, engine, models
    .\tools\meshd_probe.ps1                 # meshd MCP wire smoke
    .\tools\meshd_lease_yield.ps1           # lease -> render -> yield E2E

All PASS plus `http://<node>:8188/system_stats` answering is the
definition of done. Then write the owner a short report: what passed,
what you fixed on the way, what needs the portal.

## Health surface (after bring-up)

Logs in `C:\forge\logs\`. `.\state_check.ps1` and `.\audit_forge.ps1`
summarize services and tasks; `tools/forge_smoke.py`,
`tools/router_smoke_new.py`, `tools/llama_health.ps1`,
`tools/verify_health.py` probe each door; `tools/restart_*.ps1` cycle
one service without touching the others.
