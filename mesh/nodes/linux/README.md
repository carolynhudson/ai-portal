# Linux mesh nodes (Ubuntu / Debian / Fedora; x86_64 / aarch64)

The Linux mirror of `../windows-forge/` (that kit is the reference and
stays canonical for the forge itself). One meshd source tree, two
platforms: meshd 0.3.3+ reads env vars for every path/URL that used to
be forge-hardcoded, so a Linux node needs no source edits -- see
`mesh/meshd/src/main.rs` defaults (all defaults = the forge's Windows
layout, behavior unchanged).

## What a Linux node is

- meshd (Rust, this repo) as a **user** service on `127.0.0.1:8092`;
  the firewall gates the MCP door to the portal IP. No root required.
- Identity: `~/.mesh/` (mTLS leaf, 24 h, expiry = revocation; stable
  age key for the secrets broker). The portal mints a leaf nightly per
  node and stages it at `halo:~/ai/state/mesh/<node>-bundle/`;
  `mesh_selfheal.sh` pulls it at launch (6 h floor; stale bundles are
  refused).
- Layout under `$MESHD_ROOT` (default `~/forge`, mirrors `C:\forge`):
  `bin/meshd`, `etc/mesh/meshctl`, `etc/services.json`,
  `etc/meshd.env`, `etc/mesh_leases.json`, `llama/version.json`,
  `logs/`.

## meshd environment (etc/meshd.env)

| var | meaning | default (forge-faithful) |
|---|---|---|
| MESHD_ROOT | node home | `C:\forge` -> set to `~/forge` |
| MESHD_MESHCTL | meshctl binary (absolute -- cron/systemd PATH law) | `<root>/etc/mesh/meshctl.exe` / `meshctl` |
| MESHD_BIND | MCP-door bind (consumed by the systemd unit as `--bind`) | CLI default `0.0.0.0` (forge: LAN door + firewall gate). **The Linux kit writes `127.0.0.1`** - a box without a host firewall must not expose the door; LAN exposure is opt-in and only with a firewall gate to the portal IP |
| MESHD_NODE_NAME | name on service cards | `rainbowblock` |
| MESHD_ROUTER_URL | llama.cpp router to list ids from | `http://127.0.0.1:8082` |
| MESHD_MCP_URL | forge-mcp door (image contracts) | `http://127.0.0.1:8096` |
| MESHD_SERVICES_FILE | node service registry | `<root>/etc/services.json` |
| MESHD_LLAMA_VERSION_FILE | llama build record | `<root>/llama/version.json` |
| MESHD_LEASES_FILE | lease ledger | `<root>/etc/mesh_leases.json` |

Deny-list (mesh_service refuses stop/restart): `llama-router`,
`opencode-agent`, `meshd`, `portal`. Rows with an empty `"launcher"`
refuse `start` -- observation only. **A Pi-hole pair node must ship
launcher-less rows only** (production control is owner-lane by the
Pi-hole safety law).

## Files

- `install_node.sh [root]` -- build meshd+meshctl from the tree, create
  the layout, write a starter `meshd.env`.
- `register_autostart.sh` -- systemd **user** unit + enable; prints the
  Linger reminder (boot-without-login law).
- `mesh_selfheal.sh` -- cert self-heal (port of `mesh_selfheal.ps1`,
  bench #43 laws: enroll from TEMP copies, keep the age key, refuse
  stale bundles, never print the token, failure-tolerant).
- `verify_node.sh` -- healthz, cert clock, hub lane, door smoke.
- `services.linux.json` -- observation template (router/comfyui/meshd/
  portal; no launchers).
- `llama_version.sh EXE` -- record the ACTIVE llama.cpp build
  (release-dir match; never reads the version off the build repo --
  after a staged build the repo sits at the incoming ref, and that
  probe lies).

## RAM law for image-forge on shared-memory boxes (owner 2026-11-17)

Unified-memory nodes (Strix Halo and friends) share RAM with the llama
router. `../linux-forge/start_comfy.sh` refuses to launch under a RAM
floor (default 24 GiB available) or while a router model is resident
(RSS > 4 GiB). Gates REFUSE; they never unload, never kill. Model
downloads respect the free-space-first migration law.

## Pi5-with-AI-hardware sketch (no kit yet, by owner scope)

meshd runs on aarch64 (the hub binaries already build on Pis). A
Hailo/Coral co-processor enters as cards: `requires.backend = "hailo"`
-- the card schema is backend-agnostic already, and `mesh.place`
matches on it. Pi-hole pair as mesh nodes: observation-only template
(services above with FTL/radvd/keepalived/meshhub rows, all launcher-
less, and add the ids to the deny-list row names in your services.json
plan before any control row exists, ever).
