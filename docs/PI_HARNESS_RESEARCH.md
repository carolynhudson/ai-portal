# Pi harness research — running a portal-launched pi session from a browser

The operator's requirement, in their words: a portal-launched pi session that behaves **as close to a
terminal pi session as possible** — they can type, control it, prompt models, and use extensions like
the Q&A tool "without extra work" — that keeps running when they disconnect, that they can check in on
from any machine on the LAN, and that (after a **host** reboot) leaves behind the session id and context
so the Projects page can offer it for resume. They asked explicitly for deep research before we write
our own host, and for a pivot if the honest answer is that embedding pi well is not feasible.

Everything below is from pi's own docs (installed 1.1.0), the packages' own READMEs/docs, and the npm
registry. Dates are the publication dates the registry reports.

## 1. What pi itself offers for embedding (measured from the installed docs)

| surface | what it is | fit for a portal terminal |
|---|---|---|
| `pi --mode rpc` (`docs/rpc.md`) | long-lived subprocess, JSONL commands/responses/events on stdio | full agent control **without a UI**: prompts, model/tool changes, session management, events |
| `docs/rpc-extension-ui.md` | extension `ctx.ui` calls forwarded to the client | `select`, `confirm`, `input`, `editor`, `notify`, `setStatus`, `setWidget`, `setTitle`, `set_editor_text` |
| `@earendil-works/pi-coding-agent` exports `.`, `./rpc-entry`, `./client` | in-process SDK + typed `RpcClient` | Node host embedding; the portal's web layer is Python, so this is a bridge, not a page |
| `docs/session-format.md`, `~/.pi/agent/sessions/<encoded-cwd>/` | session transcripts on disk, shared by CLI/TUI | **resume is already solved** — `--session <id>`, `--session-id <id>`, `--continue`, `--resume`, `--fork`, `--name`, `--session-dir` (all in `pi --help`) |
| `docs/tmux.md` | pi inside tmux needs extended keys (`extended-keys-format csi-u` on tmux ≥ 3.5) so `Shift+Enter` ≠ `Enter` | the fidelity bar any browser terminal must meet |

**The RPC degradations are decisive** (verbatim from `rpc-extension-ui.md`): `custom()` returns
`undefined`; `onTerminalInput()` is a no-op; `setFooter`, `setHeader`, `setWorkingIndicator`,
`addAutocompleteProvider`, `setEditorComponent`, `setToolsExpanded` are no-ops; `getAllThemes()` returns
`[]`; `setTheme()` returns an error ("Theme switching not supported in RPC mode"); `setWidget` supports
"only string arrays … component factories are ignored".

So an RPC-based portal UI would be a *second implementation of pi's interface*, permanently behind pi's
own feature set: no themes, no autocomplete UI, no custom extension widgets, no component widgets. That
is precisely the "stuck chasing problems" outcome the operator asked to avoid. **RPC is the right tool
for headless jobs and for the portal's own automation; it is the wrong tool for the interactive
session.**

## 2. The three browser UIs for pi (measured from npm + their docs)

| | **PI WEB** `@jmfederico/pi-web` | **pi-web-ui** `pi-web-ui` (xing-shuyin) | **Pi Web** `@agegr/pi-web` |
|---|---|---|---|
| npm | 26 deps, 3 peers (`pi-coding-agent`, `pi-ai`, `pi-agent-core`), `node-pty`, `@xterm/xterm` 6, fastify, lit, CodeMirror | 11 deps, 2 peers (`pi-coding-agent`), `node-pty`, express, `ssh2`, `tar`, `electron-updater` | 1 dep (`ws`) |
| Node | ≥ 22.19.0 | ≥ 22.19.0 | ≥ 22.19.0 |
| licence | MIT | MIT | MIT |
| releases | 33, 2026-05-09 → 2026-10-02 | 196, 2026-08-04 → 2026-10-11 | 64, 2026-02-26 → 2026-10-03 |
| **where the agent lives** | **a session daemon (`pi-web-sessiond`), separate from the web/API server**, joined over a Unix socket (`~/.pi-web/sessiond.sock`) | in the web server process (the SDK "runs in-process") | in the web server process, **with `PI_WEB_IDLE_TIMEOUT_MS` default 600000 — idle sessions shut down** |
| disconnect | "A browser disconnect should not stop an agent. PI WEB keeps the session runtime separate from the browser and web/API service" | conversations "keep running in the background after you switch away" (same process) | viewer; idle shutdown |
| restart semantics | "Restarting [sessiond] can interrupt active sessions. A web/API restart or browser reload is not a substitute" — i.e. **a web/API restart does not interrupt them** | "If the server was shut down mid-answer, the next attach reports it once (last run was interrupted)" | server restart ends runs |
| Q&A forms | question forms "up to 20 questions, up to 12 choices … **survive browser/web restarts while its owning session daemon remains running**"; extension dialogs always supported | `ask_user_question` added with rich previews, "an unanswered questionnaire survives a page refresh or reconnect" | n/a |
| projects | `~/.pi-web/projects.json` (`PI_WEB_PROJECTS_FILE`), projects + worktrees + sessions, remote "machines" fleet | workspace root switchable at runtime (`/cwd`, folder picker, file-tree right-click) | grouped by project from session files |
| pi session files | `<agentDir>/sessions/<encoded-cwd>` (`PI_CODING_AGENT_DIR` = `~/.pi/agent`) | "sessions are read from `<agentDir>/sessions/--<cwd>--/`, i.e. the same transcripts the pi CLI/TUI writes, so the browser and a terminal `pi` session share one list per project" | "uses the same local configuration and session files as pi" |
| binding / auth | `host` `127.0.0.1`, `port` `8504`, `PI_WEB_HOST/PORT/ALLOWED_HOSTS`; "not a sandbox … do not expose it directly to the public internet without a trusted network, firewall, VPN, SSH tunnel, or authenticated reverse proxy"; URL-prefix proxying supported | loopback by default, `PI_WEB_HOST=0.0.0.0` for LAN, `PI_WEB_TOKEN` (Bearer / header / `?token=` / cookie), WebSocket origin check (`Origin` hostname **and port** must match `Host`), `PI_WEB_ALLOW_ORIGINS`, nginx sub-path example | loopback, `PI_WEB_PASSWORD` login + Basic Auth |
| service | `pi-web install` creates **per-user services** (web/API + sessiond), `pi-web status/logs/restart/doctor` | `pi-web-ui server install --port --cwd` (service), `PI_WEB_SUPERVISOR` enables the update panel's restart button | `pi-web` runs the server; `pi-web status/stop/open` |
| extras | worktrees, terminal tabs, plugins, fleet of remote machines | Git panel, file editor, vision bridge, subagents + templates, plugins, themes, MCP GUI, skill market, PWA, sounds, i18n | file previews, worktrees, provider/model config UI, i18n |
| known sharp edges | login-shell PATH for services (their docs devote a FAQ section to it); `node-pty` needs `npm install -g --allow-scripts=node-pty`; pi **virtual models are unsupported** (`registerVirtualModel()` raises) | 8.3 MB extension, `ssh2`/`tar`/`electron-updater` in the dep tree; agents die with the server; it pins its own bundled pi SDK copy (`PI_WEB_SDK=bundled\|global`) | idle shutdown by design |

Node on this box is **v22.22.1** — above every floor. `node-pty` is a native module (needs a compiler
and an approved install script), `@xterm/xterm` 6 supports the **Kitty keyboard protocol**, which is
what pi requests for `Shift+Enter` vs `Enter` (see `docs/tmux.md`; xterm.js landed Kitty support in
6.1).

## 3. What this means for the portal

**The pi harness page does not need a PTY host written by us.** The load-bearing property the operator
named — the session outliving the portal, the browser, and the page — is the *decoupled session daemon*
in PI WEB: web/API restart ≠ session interruption, browser disconnect ≠ session interruption, and
sessions resume from pi's own session files. Adopting it removes the two pieces of our code we were
least equipped to make robust (a PTY relay and a terminal), and the terminal that comes with it is
`node-pty` + `@xterm/xterm` 6, i.e. the same stack pi's own keybinding fidelity needs.

The portal's own scope stays, and is what the packages do **not** do: create the project folder with
the init shortlist (uv/git/README/AGENTS.md/…), own `projects_root` as a portal setting, list projects
with their portal context (which ones the portal knows, which have live sessions, which have records
from before a reboot), and link into the harness with the project already chosen.

**Remaining verification, by spike on this box, before the layout spec's L4 is called done:**

1. install `@jmfederico/pi-web` with `--allow-scripts=node-pty`, `pi-web install`, `pi-web doctor`
   (PATH/lingering checks), loopback on `:8504`;
2. start a session in a sandbox project, close the browser, restart **only** the web/API service,
   reconnect: the session must still be running and attached;
3. restart **sessiond**: the run is interrupted and the session is resumable by pi session id, and the
   Projects-page record we keep is what makes it findable afterwards;
4. confirm the deep link/URL shape for "open this project/session" (so the portal can hand off), and
   whether an HTTP endpoint reports live sessions per project for the page to read;
5. confirm `Shift+Enter` inserts a newline and `Enter` submits in the web terminal with the browser
   used on this network;
6. confirm the Q&A tool opens as a browser form with previews and survives a page reload.

If the spike shows the terminal cannot reach the operator's fidelity bar (item 5), the fallback is the
shape in `docs/PORTAL_LAYOUT.md` as previously written — our own PTY host (a systemd user unit owning a
`pty.openpty` pair and a Unix-socket relay, `@xterm/xterm` vendored by us) — which is ~200 lines of
host code and no pi feature lag, and is exactly what both packages avoid writing themselves.

## 4. Access from other machines (the operator's HTTPS / auth question)

Both packages bind loopback by default and both document the same supported answer: a **same-host
reverse proxy** (their examples use nginx) that terminates TLS at a sub-path and forwards HTTP +
WebSocket, keeping the service loopback. PI WEB states the proxy must strip the prefix and redirect to
a trailing slash; pi-web-ui ships an nginx block for `/pi/` and a WebSocket origin check that requires
the `Origin` hostname *and* port to match the `Host`.

That gives the portal's network plan a concrete shape: a local CA + certificates for `portal.local`,
`pihole.local`, `pihole2.local`, nginx on the portal host terminating TLS, and the portal's own password
gate plus a shared token for the harness. It is a queue item of its own, recorded in
`docs/PORTAL_PLAN.md sec 6.14.2`, and the operator's position — not required for proof-of-concept, needed before production —
is recorded there.

## 5. Spike on this box (2026-10-11) — what passed, what did not

Installed `@jmfederico/pi-web` 1.202610.1 into `~/.npm-global` (npm's global prefix on this box is
root-owned, so a user prefix plus symlinks into `~/.local/bin`, which the login shell already carries);
`pi-web install` created **`pi-web-sessiond.service`** and **`pi-web.service`** as systemd user units
(`ExecStart=/usr/bin/env bash -lc "exec pi-web-sessiond"` — a login shell, which is why their doctor
cares about PATH), both active, web on loopback `127.0.0.1:8504`. `pi-web doctor` reported: node
22.22.1 ✓, npm ✓, `pi` 1.1.0 found in the login shell ✓, ripgrep ✓, `node-pty` native module loadable ✓
(it compiled — `build/Release/pty.node`), systemd user lingering enabled ✓.

**What the portal can drive (measured over HTTP):**

- `GET /api/pi-web/health` → `{"ok":true}`; `GET /api/pi-web/runtime` → web + sessiond versions,
  `piVersion 1.1.0`, `activeAgentProfile.dir = ~/.pi/agent`; `GET /api/pi-web/status` → per-component
  install/runtime versions and staleness.
- `GET /api/projects` → registry; `POST /api/projects {"path": …, "name": …, "create": bool}` →
  `{id, name, path, createdAt}` (the schema is stated in its own validation error), and the UI showed
  the project in its sidebar from the same registry. `DELETE` by id works.
- `POST /api/sessions {"cwd": …}` → `{id, path, cwd, persisted, created, modified, messageCount}`, where
  `path` is the pi session file — `~/.pi/agent/sessions/--tmp-spike-proj--/<ts>_<uuid>.jsonl`.
  **This is the resume record the Projects page needs: the harness hands back pi's own session id.**
- `POST /api/sessions/:id/prompt {"cwd", "text"}` → `{"accepted":true}` (field is `text`; `message`,
  `prompt` are rejected by name). `GET /api/sessions?cwd=…` lists sessions for a project.
- `GET /api/machines/local/status` → live epoch/revision map over projects/workspaces (what the page
  would poll, alongside the `ws://…/api/machines/local/events` socket the UI itself uses);
  `GET /api/machines/local/plugins` → bundled `pi-web.terminal` (required) and `git`, both `healthy`.

**Web/API restart does not touch the daemon** (measured, not asserted from docs): restart
`pi-web.service` → web pid 191849 → 192615, `pi-web-sessiond.service` pid **191848 unchanged**, and the
daemon answered throughout. That is the property the operator named; it holds.

**What did not pass: a session run against the portal's llama.cpp router.** The UI's model picker
("Select Model" → Enabled / All models / Search) listed **no models**; the composer chip read
`unknown/unknown`; a prompt was accepted and the transcript answered:

> No API key found for the selected model. Use `/login` to log into a provider via OAuth or API key.

`GET /sessions/:id/models?cwd=…` on the daemon socket returned `{"models":[]}`. Adding a systemd drop-in
(`LLAMA_BASE_URL=http://127.0.0.1:8080`, `PI_PROVIDER=llama.cpp`) and restarting sessiond did not change
it. The diagnosis is not pi-web's: **terminal pi in the same login shell behaves the same way** —
`pi -p "…"` with no model selects a Hugging Face route and fails (`403 … Inference Providers …`), while

```
LLAMA_BASE_URL=http://127.0.0.1:8080 PI_PROVIDER=llama.cpp \
pi --model "llama.cpp/ukisai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-GGUF:IQ3_XXS" -p "Reply OK"
→ OK
```

works. So the provider is reachable and the router serves it; **an explicit model is mandatory** on
this box, and pi's provider list for llama.cpp is ambiguous by design (the operator's own experience:
`llama.cpp` vs `Llama.cpp`, `/login llama.cpp=http://127.0.0.1:8080` with a blank key, the loaded model
appearing last in `/model`, `/models` listing the router's library, load/unload from `/models`
unreliable). The portal's router already knows which model is loaded, so the portal is the component
that can supply the explicit `provider/model` at session start — the harness API accepts `model`,
`modelId`, and `provider`+`model` on `POST /api/sessions` (all three accepted; which one binds is
unverified).

**Parity gap found, unmasked:** typing `/model …` in the composer produced the system message
**"/model is not implemented in the web UI yet"** — pi's `/model` works in a terminal and not there.
`/new /name /cwd /thinking /resume /reload` are listed as their built-ins; `/model` is not among them.
That is a concrete divergence from "as close to a terminal session as possible" and it lands exactly on
this box's weak point (model selection), which is the interaction the operator described fighting with.

**Not yet verified (need a session that can run):** session continuity across a web restart *while a run
is in flight*; the Q&A form with previews and its survival of a reload; `Shift+Enter` vs `Enter` in the
web terminal. The tooling for them exists and is measured working on this box — **Playwright + Chromium
headless-shell 156 drives the page** (the UI is Lit with open shadow roots, so selectors must walk
`shadowRoot`; that is why the first scripted attempts read an empty body).

**One more thing Playwright unblocks** (relevant to the design baseline, `docs/PORTAL_UI.md` §6):
layout, spacing, density and contrast claims on the portal's own pages become *measurable* — the harness
that exists today is jsdom, which cannot see layout at all.

## 6. Sources

- `docs/rpc.md`, `docs/rpc-extension-ui.md`, `docs/sdk.md`, `docs/session-format.md`, `docs/tmux.md`,
  `docs/termux.md`, `pi --help` — installed pi 1.1.0 under
  `~/.pi/agent/install/releases/1.1.0/node_modules/@earendil-works/pi-coding-agent/`.
- PI WEB: <https://pi-web.dev/>, <https://pi-web.dev/config>, <https://pi-web.dev/remote-first>,
  <https://pi-web.dev/faq>, <https://github.com/jmfederico/pi-web>.
- pi-web-ui: <https://pi.dev/packages/pi-web-ui>, <https://github.com/xing-shuyin/pi-web-ui>.
- Pi Web: <https://github.com/agegr/pi-web>.
- npm registry metadata for `@jmfederico/pi-web`, `pi-web-ui`, `pi-web` (versions, deps, engines,
  licence, release dates), read 2026-10-11.
