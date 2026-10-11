# Portal layout Interaction Design Spec

<!-- spec-schema: 2 -->

Written against `docs/PORTAL_UI.md` (the portal's own baseline) and the measurements in it. The
shaping answers are the operator's, recorded 2026-10-10: a **rich hero dashboard with sub-pages for
detail**, **a design spec before the build**, and **`~/ai-projects/*` as its own portal page**.

## L1 Positioning and intent

- **User-visible goal:** from one screen the operator can say what the system is doing right now,
  which of its states needs a decision from them, and reach the page that owns that decision — and
  every fact has exactly one owner page. The finding this answers is "disconnected, cramped in
  places, not well laid out": the fix is a stated owner for each fact, not a new look.
- **Target user:** the operator — one human, root on their own box, keyboard and mouse, LAN clients
  reaching the router's API, and an agent (pi) that reads the same pages. Not a visitor, not a
  prospective user, not an evaluator of screenshots.
- **Scene list:**
  - S-A *is it serving what I think it is* — router state, loaded model, slots busy, aliases
    answering.
  - S-B *what needs me* — a preset drift, an Apply pending, an unregistered build, a server outside
    the unit, an index problem.
  - S-C *does the machine have room* — RAM aperture, per-model slot cost, disk, GPU.
  - S-D *change a personality and make it real* — edit → preview → Apply, with the file's sha in
    front of them.
  - S-E *get a model onto the box and make it usable* — inventory, download/build, index, register,
    available.
  - S-F *update a tool and know the router is safe* — llama.cpp release, ComfyUI, huggingface-cli,
    python/venvs, GPU drivers.
  - S-G *run work in a project* — a `~/ai-projects/*` project, a pi session pointed at it, its log.
- **Non-goals:** a new palette (baseline §1: every token pair clears WCAG AA — colour is not the
  problem); a client-side framework (the portal is server-rendered Jinja + page scripts, and the
  harness drives that); multi-tenant or remote-user work (auth is one operator password); a visual
  redesign of `/help`; replacing the job bar (it is the model the ribbon copies); anything that
  writes `presets.ini` or restarts the router outside Apply.
- **Behavior boundary:**
  - **Always:** state facts in sentences; name degradation as "did not look", never as a reassuring
    default; keep one fact in one renderer; make every mutating verb confirm and name what it
    changes; state why a control is disabled.
  - **Ask first:** anything destructive to a model file, a build, a release, or a personality;
    starting the router while the census lists servers outside the unit; enabling a
    capture/monitoring agent on this box.
  - **Never:** write `presets.ini` outside Apply; restart or signal the router from a page verb;
    kill a process the portal does not own; claim an appearance property that no tool measured.

## L2 Information architecture

- **Spatial region definitions:**
  - **Ribbon** — a persistent statement strip in `base.html`, in the same place on every page,
    computed once server-side: router state · loaded model · slots busy · jobs running · alerts
    needing a decision. Like the job bar, it reserves its own space so it never overlaps content.
  - **Hero** (`/`) — four tiles and one attention queue, answerable without scrolling: **Serving**,
    **Machine**, **Work**, **Attention**. Each tile is a card of `.kv` rows and links to its owner
    page. Each line is a sentence, not a number alone.
  - **Owner pages** — `Service`, `Models`, `Machine`, `Portal`, `Projects`: each owns a set of facts
    and the verbs that change them.
  - **Detail sub-pages** — the depth that used to sit on `/`: the router's pulse, census and slot
    detail; hardware and probe detail; the five tool/driver update cards; a model's own detail.
  - **Job bar** — unchanged, global, on every page, with the machine lock it already enforces.
- **Region boundary rules:** a fact is rendered by its owner page or by the ribbon, never both with
  the same wording; a tile *summarises* and links, it does not host a form; a detail sub-page never
  duplicates its owner page's verbs (verbs live on the owner page); `/` hosts no settings form.
- **Content growth rules:** a page earns more content by adding a *sub-page*, not by adding a card
  (measured cause of the crowding: `/` at 9 cards / 10 headings, `/personalities` at 221 KB with 94
  buttons); a card grows rows before it grows columns, and rows stay one-sentence (`.kv`); the
  spacing scale in `docs/PORTAL_UI.md` §3 is the only spacing source, and a new size is written
  there first.

### Page duties

| Page | Duty |
| --- | --- |
| `/` | Answer "what is running and what needs me" in one screen, and route to owners. Hosts no verb that changes state. |
| ribbon | State the five live facts, in the same place, in the same words, on every page. |
| `/service` | Own the llama.cpp service: router unit, personalities deck, model configs, presets file state, Apply. |
| `/service/router` | The router's own detail: pulse per slot, process census, journal feed, start/stop/restart with the census gate. |
| `/models` | Own what is on disk and in the index: inventory, budget, download, builds, strays. |
| `/models/detail` | One model: its header facts, its quant list, what it can run, its registrations. |
| `/machine` | Own the box: load, RAM aperture, disk, GPU, health probes, alerts settings. |
| `/machine/updates` | Own what changes the software under it: llama.cpp release, ComfyUI, huggingface-cli, python/venvs, GPU drivers. |
| `/portal` | Own the portal itself: auth, bind, rate limits, polling, jobs history, events, the documentation entry. |
| `/projects` | Own the projects root: create a project from the init shortlist, list projects, and launch a pi session scoped to one. Inactive state only. |
| `/pi` | Own the live pi session: the terminal, its state, and `/quit`. Shows the session and nothing else while one is running. |

## L3 Core flow

- **State list:** page load · loading (fetch in flight) · populated · degraded (probe/file/list
  unreadable) · empty (no such thing here yet) · machine-busy (a job holds the lock) ·
  unauthenticated · mutating-verb pending confirm · job running (bar visible, lock applied).
- **Primary path:** hero → read the four tiles → open the attention item that needs a decision →
  land on the owner page → act (confirm names what changes) → the ribbon and job bar show the
  consequence → back to the hero, which now states the new fact.
- **Branch paths:** a server outside the unit holds the router's port (census names it; start
  confirms with that list, and the page states the start will not move it) · a preset file the
  router did not load (drift statement, Apply is the way to close it) · a probe failed (degradation
  sentence + a re-check verb) · a job holds the machine (controls inert with the reason, bar names
  the job) · unauthenticated (login, then return to the page the link came from).

### Paths

| Path | Steps |
| --- | --- |
| P1 | `/` → tile Serving → `/service/router` → census names a foreign server → start → confirm (names pids/ports) → unit starts → ribbon states active |
| P2 | `/` → Attention: preset drift → `/service` → pick personality → edit rows → Preview → Apply (confirm) → `presets.ini` sha changes → ribbon/hero state the loaded file |
| P3 | `/models` → download/build a model → index reports it → register it in a personality → `/service` → hero Serving lists the alias |
| P4 | `/machine/updates` → check a release → apply with confirm → job runs (bar visible, machine locked) → job names the result and rollback path |
| P5 | `/projects` → project detail → start a pi session job scoped to it → job bar names it → log readable on the project page |
| P6 | any page → a stated fact cites the documentation → `/help#section` lands on the cited section |
| P7 | `/machine` → a probe degraded → "did not look" sentence → re-check → populated or degraded again (never a default) |
| P8 | `/projects` → name a project → tick the init shortlist (git, uv, README, .gitignore) → create → the project row appears with the init results |
| P9 | `/projects` → pick a project → launch (confirm names the working directory) → `/pi` operational (terminal only) → work → `/quit` → `/projects` inactive |
| P10 | `/projects` → pick a project → resume a prior session by id → `/pi` operational, reconnected to the live session after a portal restart |
| P11 | `/machine/updates` → the pi harness card → check pi version and extensions → update (confirm) → card states the installed versions |

## L4 Component behavior detail

- **Component role and function list:** ribbon (statement strip, five facts, links); tile (card of
  `.kv` rows + one owner link, never a form); attention queue (dated statements, each linking to the
  owner page, each with the verb that closes it); deck card (personalities step 3, one card per
  model, strip names who else uses the config); census panel (process statements + re-check verb);
  `.pill` (state badge, colour *and* word); `table` (inventories); form rows (`.kv`-shaped rows
  generated from the settings registry, values typed by the row's declared type); tabs (multi-panel
  pages); job bar (global job + machine lock); `.mono` (ids, paths, shas, pids, ports).
- **Default / hover / loading / disabled / error states:**
  - Ribbon: populated sentence set; degraded fact reads "unreadable", never stale-styled as fresh;
    the fact carries the time it was read.
  - Tile: loading shows the row with the value pending; degraded states the probe that failed;
    empty states "nothing registered" plus the verb that creates one.
  - Attention queue: empty says "nothing needs a decision right now" (a statement, not a blank);
    every item carries its age.
  - Buttons: disabled controls state why, in the page; machine-busy applies the existing
    `data-jobbusy` inertness to `[data-needs-machine]`; every mutating verb confirms.
  - Census and probe panels: re-check verbs are non-mutating and never confirm.
  - Focus: keyboard focus is visible as a ring, not a 1 px border-colour change (baseline §6 item 4
    is a defect this spec fixes, not a style preference); `prefers-reduced-motion` covers the job
    bar spinner on every page, not only the dashboard.
- **Session host (new component, and the load-bearing one).** A pi session is a process the portal
  *connects to*, never a process the portal *parents*. Shape, with each part justified by a
  measurement on this box:
  - The host runs as a **systemd user unit** (`systemd-run --user -u ai-pi-<id> …`): not a child of
    the portal, so `systemctl --user restart ai-portal` cannot kill it; discoverable with
    `systemctl --user list-units 'ai-pi-*'`; supervised, with a journal. `Linger=yes` is set, so
    user units keep running when the operator logs out of the desktop — which is the "I close my
    browser / log out / drive home" case.
  - The host owns a **PTY** and serves it over a Unix socket. Clients attach and detach freely; a
    detaching client never sends EOF to the agent. `tmux` and `dtach` are **not installed** (and
    `systemd-run --pty` proxies a caller's terminal, which is the opposite of detachable), so the
    PTY host is ours: `pty.openpty`, the child's stdio on the slave, a socket relay on the master.
  - Two records, because they answer different questions. A **registry** under
    `~/ai/state/portal/pi-sessions/records.jsonl` (session id, unit, project path, started, ended,
    exit, log path) and the **PTY stream log** in the same directory. The user journal is persistent
    here (`/var/log/journal/…/user-1000.journal`, measured), so a unit's transcript survives a
    reboot; the registry is what makes a session *findable* afterwards.
  - After a **host reboot** the live agent is gone and nothing pretends otherwise: the Projects page
    lists the session with its id, project, end state and log path, and Resume starts a *new* host
    with `pi --session <id>` in that project directory. `pi` supplies the identity —
    `--session-id <id>` creates, `--session <id>` / `--continue` reopen, sessions under
    `~/.pi/agent/sessions/<project-dir>/` (all measured from `pi --help`).
- **Terminal (new component).** `@xterm/xterm` over a WebSocket to the host. It is the only part of
  the portal that streams; it authenticates with the same cookie as the REST API, it shows the
  session's state in words (starting · live · ended · exited-n), and it closes with `/quit`.
- **Two states only.** Inactive: projects list, create form, launch and resume verbs. Operational:
  the terminal, and nothing else — no nav to a dashboard that hides a live session, no second launch
  control, no settings form on the same page.
- Reuse is the rule: new surfaces compose the twelve baseline primitives; page-local CSS is allowed
  only for a layout the primitives cannot express, and it is written as a named rule in
  `docs/PORTAL_UI.md`, not as a one-off selector. The exception to "no internal change to existing
  components" is the focus and reduced-motion fixes above, which are declared here precisely because
  they conflict with the current primitives.

## L5 Edge conditions

- **Empty state:** every list has a sentence naming what is absent and what would fill it — no
  personalities, no registered models, no builds, no jobs, no projects, nothing needing a decision.
  An empty hero tile links to the page that can create the thing.
- **Loading state:** fetches show a pending row, not a spinner in place of a fact; the hero does not
  blank while `/api/router` is in flight; a long job is the job bar, which is already global.
- **Error state:** a failed probe names the probe and the reason, and offers the re-check; a page
  never renders the reassuring default it would render if the probe had succeeded.
- **Permission downgrade:** unauthenticated requests get 401 on the API and a redirect on pages
  (both measured behaviours, asserted in tests); a session without the machine unlock (no password
  posted) sees the machine verbs inert *with the reason on the page*; the built-in personalities
  (`safe`, `default`) show their lists read-only with Save/Delete locked and the refusal named.

### Five-state matrix

| Page | initial | loading | success | failure | empty |
| --- | --- | --- | --- | --- | --- |
| `/` | ribbon + four tiles + queue, all server-rendered | tile rows show "reading" per fact | tiles state live facts with times | degraded fact names its probe; queue states it could not be computed | "nothing needs a decision right now" |
| ribbon | five facts, read at page load | "reading" for the fact being refreshed | live facts, same words on every page | "unreadable: <reason>" per fact | n/a (a unit that is absent is a fact, not an empty state) |
| `/service` | deck of personalities + presets state | deck rows pending | deck + selected personality's editor populated | preset file unreadable stated; deck states it read none | "no personalities yet — create one" |
| `/service/router` | unit state, pulse, census, verbs | census pending row | census names router, workers, presets, served counts | census states it did not look; verbs stay usable with that warning stated | n/a |
| `/models` | inventory + budget + strays | scan row pending | inventory with budget figures | index unreadable stated; budget states "no aperture measured" | "nothing in the library — download one" |
| `/models/detail` | header facts + quant list | row pending | facts populated | header unparseable stated with the parse error | n/a (a model with no header is a failure, not an empty) |
| `/machine` | load, aperture, disk, GPU, probes | chart polling only while the panel is open | populated with sample times | probe-by-probe degradation sentences | n/a |
| `/machine/updates` | five tool cards with installed/available | per-card check pending | installed == available stated with shas | a failed check names the command and its output tail | n/a |
| `/portal` | settings form + jobs history + events | form save pending | saved values read back from the store | save refused states the validation reason | "no jobs have run" |
| `/projects` | project list with git state | per-project read pending | projects with state and work links | a project that cannot be read is named with the reason | "no projects under ~/ai-projects" |

## L6 Acceptance criteria

- Given a browser on `/` with the portal authenticated, When the page finishes loading, Then the
  ribbon shows the router state, the loaded model, the slots busy, the running jobs, and the count of
  alerts needing a decision — each as a sentence with the time it was read — and the four hero tiles
  link to owner pages. (path: P1)
  - Required evidence: `tests/ui/spec.json` expectations driven by `tests/ui/run.mjs` against the
    sandbox portal (`expectVisible`, `expectText`, `expectRequests`), plus `test_ui_contract.py` for
    the source invariants; target-viewport render at 1280×800 and 390×844 (capture seed
    "hero-populated screenshot") once the evidence gate is enabled.
- Given a page other than `/`, When it loads, Then the ribbon renders the same five facts with the
  same wording as on `/`, and no page re-states a fact the ribbon already owns in different words.
  (path: P1)
  - Required evidence: contract test asserting the ribbon is in `base.html` (one renderer for the
    five facts) and the harness asserting the ids on two different pages; mutation check that a
    page-local restatement of a ribbon fact fails an assertion.
- Given a hero with all four tiles populated plus the attention queue, When the page is measured in a
  layout engine at 1280×800, Then the hero is answerable without scrolling and no card overlaps
  another. (path: P1) (path: P2)
  - Required evidence: a layout-engine measurement — Playwright capture at the two target viewports
    (capture seeds "hero 1280 viewport screenshot", "hero 390 viewport screenshot") with reported
    bounding boxes. Declared, not yet available: jsdom has no layout engine, so this criterion is
    the reason Playwright is in the plan (`docs/PORTAL_UI.md` §6 item 7).
- Given the census lists a `llama-server` process outside the unit, When the operator presses start,
  Then a confirm names those processes and states that starting will not move what already holds
  those ports, and the harness records no `POST /api/services/llamacpp/start` before that confirm.
  (path: P1)
  - Required evidence: automated check — the harness's `requireConfirmFor` gate on
    `/api/services/llamacpp/start` and the mutation check "starting the router stops consulting the
    process census", which must stay caught.
- Given every page listed in the L5 matrix, When the harness drives it in the sandbox, Then no
  control is reported dead, no request is made before its confirm, every panel id asserted exists,
  and the coverage gate reports no unreachable panel. (path: P2) (path: P3) (path: P4) (path: P5) (path: P7)
  - Required evidence: `tests/test_ui_integration.py::test_every_control_works[<page>]` for each
    page in the matrix, and `tests/ui/mutation_check.py` catching the injected regressions.
- Given a page states a rule, a limit, or a refusal, When the operator follows a link from that
  statement, Then it lands on the documentation section that carries the rule (not the top of a
  document). (path: P6)
  - Required evidence: the KB link guard tests (`tests/test_kb_links.py`) over the new sections
    added to `docs/PORTAL_UI.md` and `docs/PORTAL_LAYOUT.md`.
- Given a fact cannot be read, When the page renders it, Then the sentence says the read failed and
  names the reason, and no field shows the value a successful read would have produced. (path: P7)
  - Required evidence: contract test on the degradation sentences, harness expectation on the
    degraded branch in the sandbox (the sandbox's portal *is* outside the unit, so the census's
    foreign branch is the deterministic one), and the mutation check
    "the census stops naming the servers it does not control".

- Given no pi session is running, When the operator opens `/projects` and launches a session in a
  project directory, Then `/pi` shows the terminal and nothing else, the session's own id is on the
  page, and the process is not a child of the portal (it is started with `setsid` and a pidfile under
  the portal state dir). (path: P9)
  - Required evidence: an automated check that launches a session, reads `/proc/<pid>/stat` parentage
    against the portal's pid, and asserts the pidfile; interaction record for the launch confirm.
- Given a live pi session, When the portal service is restarted, Then the session process is still
  running, the portal discovers it on startup from the pidfile, and `/pi` reattaches to the same
  session id. (path: P10)
  - Required evidence: an automated check — start a session, `systemctl --user restart ai-portal`
    (portal unit only), assert the session pid is alive and `GET /api/pi/session` names it; state
    "portal restarted with a live session" as the capture seed.
- Given a running session ends (via `/quit`), When the page observes the end, Then `/pi` returns to
  the inactive state with the session id and exit code stated, and `/projects` offers resume by that
  id. (path: P9)
  - Required evidence: automated check on the end-of-session transition; harness expectation that
    the operational controls are gone and the inactive controls are present.
- Given the projects root is a setting, When it is unset, missing, or a path that is not a directory,
  Then the create form is disabled with that reason on the page and the endpoint refuses with the
  same reason. (path: P8)
  - Required evidence: `test_ui_contract.py` on the disabled-with-reason markup, an endpoint test on
    each refusal, and a harness expectation that a project name escaping the root is refused.
- Given the pi harness has a version and installed extensions, When the operator opens
  `/machine/updates`, Then the pi card shows the harness version and the extension set with the
  same check/update shape as llama.cpp and ComfyUI. (path: P11)
  - Required evidence: endpoint test on the pi card payload, harness expectation on the card text,
    and `test_ui_contract.py` that the pi card lives on the updates page and not on `/pi`.

- Given a session ended by a host reboot, When the operator opens `/projects` after the portal comes
  back up, Then the session is listed with its id, project, end time and log path, and Resume starts
  a new host in that project with that session id — and no part of the page claims the session is
  live. (path: P10)
  - Required evidence: an automated check that writes a registry record, restarts the portal, and
    asserts `/api/pi/sessions` reports the record with `live: false`; harness expectation that the
    resume control carries the id.

Design done definition: the layout is done when the ribbon is the only place the five live facts are
restated, every one of the operator's seven concerns has exactly one owner page, `/` answers "what is
running and what needs me" at 1280×800 without scrolling with no overlap, every mutating verb
confirms with the objects named, every degraded read says it did not look, and the harness plus the
mutation checks pass for every page in the matrix — with the layout-engine criterion satisfied by a
measurement, not by prose.
