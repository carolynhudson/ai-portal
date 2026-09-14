# Debugging the portal

Where things run, where the logs are, and how to check a claim the UI makes.

## 1. What runs where

| What | Source (edit here) | Runtime (this is what answers) |
|---|---|---|
| Portal | `~/ai-projects/ai-portal` | `~/ai/ai-portal` + its own `.venv`, unit `ai-portal` |
| llama.cpp router | `~/ai/llama.cpp` (source only) | `~/ai/bin/llama-server` → `~/ai/releases/llama.cpp-<ver>` |
| ComfyUI | — | `~/ai/comfyui` (repo + venv), unit `ai-comfyui` |
| Dev instance | `~/ai-projects/ai-portal` | `./scripts/dev-run.sh` → `:8091`, same `~/ai` state |

```bash
systemctl --user status ai-portal ai-llama-router ai-comfyui   # all three
python3 -m app.deploy --status                                 # recorded sha + unit state
```

**The golden rule of this tree:** a config change is *written* to
`~/ai/config/*.env` and only becomes real when the unit restarts. When the UI
and reality disagree, that is almost always the reason — the exposure report
calls it `pending_restart`.

## 2. Logs

| Log | Where | Notes |
|---|---|---|
| Portal, structured | `~/ai/logs/portal.log` | rotating, 5 MB × 3; every audit event (`db.event`) is mirrored here |
| Portal, stdout/stderr | `journalctl --user -u ai-portal -f` | what systemd captured, lost on journal rotation |
| Update jobs | `~/ai/logs/update-<kind>-<id>.log` | one per job, also tailed live at `/api/jobs/{id}` |
| Router | `journalctl --user -u ai-llama-router -f` | the dashboard's activity feed is this, classified |
| ComfyUI | `journalctl --user -u ai-comfyui -f` | raw (its log format is not parsed) |

### Changing log verbosity (no code edits)

Level resolution: explicit `logsetup.setup(level=…)` → `AI_PORTAL_LOG_LEVEL` →
`info`. `trace` is accepted (maps below DEBUG) for "why is this probe empty".

```bash
# live service: edit the env file, then restart
editenv() { sudoedit "$1" || $EDITOR "$1"; }
sed -i 's/^AI_PORTAL_LOG_LEVEL=.*/AI_PORTAL_LOG_LEVEL=debug/' ~/ai/config/portal.env
systemctl --user restart ai-portal

# one-off, without touching the installed service (port 8091)
LOG=debug ./scripts/dev-run.sh
```

## 3. Probe the API by hand

Everything the UI shows is JSON. Session cookie is `aipp`.

```bash
PW=$(cat ~/ai/state/portal/PORTAL_PASSWORD.txt 2>/dev/null)   # or your AI_PORTAL_PASSWORD
BASE=http://127.0.0.1:8090
curl -s -c /tmp/c.txt -X POST "$BASE/api/login" -H 'content-type: application/json' \
     -d "{\"password\":\"$PW\"}"
curl -s -b /tmp/c.txt "$BASE/api/network"    | python3 -m json.tool
curl -s -b /tmp/c.txt "$BASE/api/exposure"   | python3 -m json.tool
curl -s -b /tmp/c.txt "$BASE/api/status"     | python3 -m json.tool
curl -s -b /tmp/c.txt "$BASE/api/router"     | python3 -m json.tool
curl -s -b /tmp/c.txt "$BASE/api/settings/llamacpp/apikey" | python3 -m json.tool
curl -s -b /tmp/c.txt "$BASE/api/health/detail" | python3 -m json.tool   # the /health pane (read-only)
```

Open endpoints (no auth): `GET /healthz` — used by monitoring and by
`tools/live_smoke.sh`.

## 4. Dry-run flags (things that check without changing)

| Command | Proves |
|---|---|
| `./deploy/install.sh --dry-run` | the whole deploy: export, venv, launch files |
| `python3 -m app.deploy --print-units` | the generated unit + wrapper text for all three services |
| `AI_PORTAL_PRINT_ARGV=1 ~/ai/bin/ai-llamacpp.sh` | the router's exact argv, no model load — this is how a launch-config change is verified before a restart |
| `POST /api/updates/llamacpp {"dry":true}` | build → stage → verify, without swapping `~/ai/bin` or restarting |
| `POST /api/settings/services/llamacpp/ensure` | regenerate router env/wrapper/unit + verify argv (never restarts) |
| `bash tools/live_smoke.sh` | end-to-end against the live router, including the update no-op path |
| `uv run --with pytest pytest tests/ -q` | 109 unit/API tests, all on a throwaway `AI_PORTAL_HOME` |

## 5. Network problems

Symptom → check, in order:

1. **What is bound?** `ss -tlnp | grep -E '8080|8188|8090'` — `127.0.0.1` means
   loopback-only, `0.0.0.0`/`*` means open, `192.168.x.x` means a single-interface bind.
2. **What does the portal want?** `~/ai/config/{llamacpp,comfyui,portal}.env`.
   Diff the two: mismatch ⇒ restart the unit (`pending_restart` in
   `/api/exposure` says the same thing).
3. **Does it answer from the LAN address?**
   `curl -sS http://<lan-ip>:8090/healthz` — from *this* box this still proves
   the bind, because the kernel routes it through the real interface; a
   loopback-only bind refuses it.
4. **From another machine?** `curl http://<lan-ip>:8090/healthz`. If step 3
   worked and this fails, it is the firewall or routing, not the service:
   `sudo ufw status`, then the exact rule is printed per service by
   `/api/exposure` (`allow_command`).
5. **IPv6 client?** `0.0.0.0` is IPv4-only; a v6-only peer needs the daemon on
   `::`. This box has a global v6 address, so a client that prefers AAAA can be
   pointed at it, but the services themselves listen for IPv4 unless configured.
6. **Router returns 401 after you enabled a key?** expected — clients need
   `Authorization: Bearer <key>` (`/api/settings/llamacpp/apikey` prints it, and
   the Web UI link carries `?api_key=`). The portal's own probes send the key;
   if the dashboard went red right after enabling, the router has not restarted
   with the key yet, or the wrapper has no `--api-key-file` (check with
   `AI_PORTAL_PRINT_ARGV=1`).

**Trust boundary.** All three are plain HTTP with, at best, a shared password
(portal) or a shared key (router); ComfyUI has no authentication at all and can
run arbitrary Python through `/prompt` plus custom nodes. They are for a trusted
LAN. Do not port-forward any of them, and if the network stops being trusted,
bind back to `loopback` and reach them over an SSH tunnel instead
(`ssh -L 8090:localhost:8090 host`).

## 6. Rollback paths

| What | Undo |
|---|---|
| Portal deploy | `./deploy/install.sh --rollback` (redeploys the sha in `~/ai/state/portal/deployment.json` history) |
| Unit file replaced | copies in `~/ai/state/rollback/units/<unit>.service.<timestamp>` → restore + `systemctl --user daemon-reload` |
| Service stop/restart | pre-action state in `~/ai/state/rollback/service-<unit>-<ts>/` |
| llama.cpp release | `POST /api/updates/llamacpp/rollback {"release": "...", "confirm": true}` (repoints `~/ai/bin` + restarts) |
| ComfyUI update | `POST /api/updates/comfyui/rollback {"commit": "...", "confirm": true}` (git reset; pip is *not* rolled back) |

## 7. Known traps

* **`HOME` in an agent/sandbox shell.** Every path derives from
  `AI_PORTAL_HOME` → `HOME`. If a shell has `HOME` pointed at a scratch dir the
  portal happily "manages" an empty tree and every service shows as missing.
  `scripts/dev-run.sh` and `tools/live_smoke.sh` pin it; when in doubt, read the
  startup line in `~/ai/logs/portal.log` (`home=… ai_root=…`).
* **Two portals.** The service on `:8090` and a dev instance on `:8091` share
  `~/ai/state/portal/portal.db` (WAL, fine) but each has its own in-memory
  caches and job runner — a job started on one is not visible on the other.
* **Restarting the portal from itself** works but the request dies mid-flight;
  the UI fires it without waiting. Same for the router — and restarting the
  router drops any agent session riding on it, which is why both need `confirm`.
* **`0.0.0.0` vs a LAN address.** The LAN address here is DHCP-assigned: a
  service bound to it explicitly fails to start after a lease change.
  `validate_bind` warns about exactly that.
* **The router is also this box's inference backend.** Never debug with
  `systemctl --user restart ai-llama-router` on a hunch.

---

## 6. UI tests: when a page renders but its buttons do nothing

The UI has no build step, so a template edit can leave a page that looks fine
and is inert. The failure mode is silent: a browser skips a `<script>` block
that does not parse, every function in it becomes undefined, and each button in
it is a dead rectangle.

```bash
./scripts/check-ui.sh                  # everything (run after every change)
./scripts/check-ui.sh --ui-only        # UI suites alone
uv run python -m tests.ui_sandbox dashboard   # one page: full JSON report + timings
uv run python -m tests.ui_sandbox --serve     # leave the sandbox up and curl it yourself
./scripts/check-ui.sh --mutations      # prove the suite can still fail (edits templates!)
```

What each failure means:

| Failure | What actually happened |
|---|---|
| `uncaught error while loading` | an inline `<script>` threw; everything after the error in that block is undefined. `test_ui_contract` names the template file, the line and the functions lost |
| `dead control: …` | the control was clicked/type-in/changed and nothing happened: no request, no dialog, no navigation, no DOM change |
| `looked up missing id #foo` | JS asks for an element that is not in the page — one side of a rename |
| `sent POST … without asking for confirmation first` | a destructive endpoint was called with no `confirm()` before it (`alert()` afterwards does not count) |
| `N panels active, expected 1` / `panel #x is reachable from no tab` | the tab group is no longer a bijection |
| `controls never driven` | something is interactable that the sweep never reached — the suite's own coverage hole |

Reading a failure: the report lists the failures first, then the load errors,
every request the page made, the controls exercised and which were dead. The
harness (`tests/ui/lib.mjs`) explains its own rules in comments — notably that
mutating requests are **intercepted** (answered with a synthetic success so the
page takes its success path) and that no `systemctl` action can reach the real
machine, which `test_ui_never_asks_systemd_to_start_or_stop_a_unit` asserts.

When you add UI:

- new page → add it to `tests/ui/spec.json`; `test_every_served_page_has_a_ui_spec`
  fails until you do
- new control → nothing to do; the sweep finds it. It must *do* something
- new tab → add it to `tabGroups` with its expected count
- new mutating endpoint → put it in `requireConfirmFor` (and, if it is the same
  path as an additive call, `requireConfirmUnlessBodyContains`)
