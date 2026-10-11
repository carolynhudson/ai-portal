# Portal interface baseline

The portal's own design authority: what the interface **is**, measured from the source, plus the
rules every page slice has to keep. It is not an aspiration list — every number below was read out
of `web/templates/base.html`, `web/static/tabs.css` and the rendered pages, and every rule below is
a rule the portal already enforces somewhere in code. Where the portal does not yet enforce a rule,
the section says so under **Known gaps**.

Why this file exists: the portal grew by slices, each one adding real capability, and the operator's
finding (2026-10-10) was that it reads as *disconnected, cramped in places, not well laid out*. A
baseline is what makes the next polish pass reviewable — a change can be judged against a written
rule instead of a taste, and the next person can tell a deliberate density from an accident.

## 1. Tokens, measured

`:root` in `web/templates/base.html`:

| token | value | role |
| --- | --- | --- |
| `--bg` | `#0e1116` | page canvas |
| `--panel` | `#171c24` | card, nav, control fill |
| `--fg` | `#e6e9ef` | body text |
| `--mut` | `#8b95a7` | secondary text, keys in `.kv`, disabled text |
| `--acc` | `#5aa9ff` | links, focus border, primary button, checkbox accent |
| `--ok` | `#3fb950` | healthy / active / success |
| `--warn` | `#d29922` | attention, not failure |
| `--bad` | `#f85149` | failure, refusal, danger |
| `--line` | `#232a35` | borders, dividers, disabled fill, muted pill fill |

There is no fourth surface: `#0b0e13` (login field, canvas) and `#2a3340` (ghost button, login
field border) are **literal hex outside the token set**. Rule: a new surface gets a token, not a
literal — a literal is a colour that nothing can change.

**Contrast is measured and passes.** WCAG relative-luminance ratios, computed against the tokens:
`fg/panel` 14.06:1, `fg/bg` 15.55:1, `mut/panel` 5.66:1, `mut/bg` 6.26:1, `acc/panel` 6.97:1,
`ok/panel` 6.73:1, `warn/panel` 6.78:1, `bad/panel` 5.10:1, `acc/canvas` 7.87:1. Every pair clears
AA normal text (4.5:1). **Color is not this interface's problem.** That fact matters for triage:
polish effort goes to hierarchy, grouping and density, not to the palette.

## 2. Type, measured

One family, two stacks: `system-ui, "Segoe UI", Roboto, sans-serif` for prose at
`15px/1.5`, and `ui-monospace, Menlo, Consolas, monospace` for anything the operator reads
character-by-character — model ids, paths, pids, ports, shas — at `13px` (`.mono`).

Sizes in use today, with their roles: **18px** `h2` (page section, margins 24/12) · **15px** body
and `.card h3` (card title) · **14px** `.kv` rows and `table` · **13px** `.mono` · **12px** `.pill`
at `font-weight:600`.

There is no type scale and no weight scale — hierarchy is carried by size alone, and the sizes are
ad hoc. Rule for new surfaces: hierarchy comes from **size + role + position**, and a new size is a
decision written here, not a number picked in a template.

## 3. Spacing and layout, measured

`main`: `padding:20px`, `max-width:1100px`, centred. `.grid`: `gap:14px`,
`repeat(auto-fill, minmax(300px, 1fr))`. `.card`: `padding:16px`, `border-radius:10px`,
`border:1px solid var(--line)`. `.card h3`: `margin-bottom:10px`. `.kv` row: `padding:3px 0`,
`gap:12px`. Table cells: `padding:6px 8px`. Buttons: `padding:10px 14px`, `border-radius:8px`.
Inputs/selects/textareas: `padding:6px 8px`, `border-radius:6px`. Nav: `padding:14px 20px`, `gap:18px`.

Spacing values in use: 3, 6, 8, 10, 12, 14, 16, 20, 24, 64 (the job bar's reserved main padding).
**There is no scale** — this is the most direct measured cause of "cramped": 3 px rows against
16 px card padding against 64 px job-bar reserve, chosen per slice.

Breakpoints are three, and they are not one system: `base.html` at 600 px (job bar),
`dashboard.html` at 640 px (hero columns), `tabs.css` at 640 px. `main`'s 1100 px is the only
width authority.

## 4. Components, measured

The portal has twelve shared primitives, all defined in `base.html`/`tabs.css`, and pages add
page-local classes on top of them:

| primitive | shape | used for |
| --- | --- | --- |
| `.card` | panel, 1 px line border, 10 px radius | every block of content |
| `.grid` | auto-fill ≥300 px columns, 14 px gap | card decks |
| `.kv` | label left (muted), value right, 14 px | one fact per line |
| `.pill` | 12 px 600-weight, 999 px radius, `ok` / `mut` variants | state badges |
| `.mono` | 13 px monospace, `word-break:break-all` | ids, paths, shas |
| `table`, `th`, `td` | 14 px, 1 px bottom border per row | inventories |
| `button`, `button.ghost` | accent fill / literal `#2a3340` | verbs |
| form controls | panel fill, line border, accent focus, `color-scheme:dark` | editing |
| `.jobbar` + `body[data-jobbar]` | fixed bar, `main` gets 64 px bottom padding | the running job, on every page |
| `body[data-jobbusy="1"] [data-needs-machine]` | `pointer-events:none; opacity:.55` | machine-lock while a job runs |
| tabs (`.tabs` in `tabs.css`) | strip over a panel, `data-raised` | multi-panel pages |
| `canvas` | 100 % × 160 px, `#0b0e13` | the CPU/memory chart |

Rules that come from how these are used, not from taste:

- **One fact, one renderer.** Two places that render the same fact drift and end up saying different
  things about one machine. Server-rendered and JS-rendered sentences are written word-for-word
  identical, and the deck of cards in the personalities editor is one grid moved into the open card,
  not two grids.
- **No `innerHTML` with unescaped data.** Page data is set with text nodes, `textContent`, and
  `createElement` — the contract test asserts it, because a page that interpolates a model id into
  HTML is a page that can be made to render markup by a filename.
- **A control that changes nothing is a bug.** The harness reports dead controls (selector, text,
  handler) and a page fails on them. A second click on an open card closes it for this reason.
- **Disabled controls state why.** Save/Delete on the built-in pair are locked client-side *with the
  reason on the page*, and refused again at the endpoint naming the reason.
- **Degradation is a sentence.** A file that could not be read, a probe that failed, a list that
  could not be read: the page says it did not look, never reports the reassuring default.
- **Verbs confirm, and name what they change.** `requireConfirmFor` covers the mutating endpoints in
  the harness; the router's start button confirms *with the processes named* when the census lists
  servers outside the unit.

## 5. What the pages own

The operator's list of portal concerns, mapped to the page that owns each fact. A fact lives in one
page; other pages may *link* to it and may show the ribbon.

| concern | owner today | owner after the re-layout |
| --- | --- | --- |
| what is running right now | `/` (hero) | `/` hero, plus the ribbon everywhere |
| machine state (load, RAM, disk, GPU) | `/` (two cards) + `/health` | **Machine** |
| jobs and the job bar | `/` job bar + `/health` | ribbon (bar) + **Portal** (history) |
| llama.cpp service configuration (personalities, model configs, presets, Apply) | `/personalities` | **Service** |
| model inventory, index, budget, download, strays | `/models` | **Models** |
| tool, driver and library updates | five cards on `/` | **Machine** |
| system health, alerts, probes | `/health` | **Machine** (probes) + **Portal** (alert settings) |
| portal configuration (auth, bind, rate limits, polling) | `/settings` | **Portal** |
| documentation | `/help` | **Portal** |
| `~/ai-projects/*` and the pi harness | **nowhere** | **Projects** |

Measured density that drives the re-layout (rendered HTML from the live home):

| page | KB | cards | h2/h3 | buttons | inputs/selects |
| --- | --- | --- | --- | --- | --- |
| `/` | 103 | 9 | 10 | 20 | 3 |
| `/models` | 55 | 8 | 6 | 4 | 8 |
| `/health` | 21 | 8 | 8 | 2 | 0 |
| `/personalities` | 221 | 0 | 5 | 94 | 120 |
| `/settings` | 30 | 4 | 4 | 12 | 4 |
| `/help` | 38 | 1 | 2 | 4 | 1 |

`/` is 9 cards and 10 headings: the router hero (pulse, attention feed, process census,
start/stop/restart), the ComfyUI hero, disk, GPU, and five update/tool cards. Live state and release
management are two different questions, and they share one scroll. `/personalities` is 221 KB with
94 buttons and 120 fields: a deck, an editor, and a reference in one page.

## 6. Known gaps, measured

Written down so they are choices, not surprises.

1. **No spacing scale.** Ten ad hoc values (§3). The direct cause of "cramped".
2. **Three breakpoints, not one system** (§3): 600 px, 640 px, 640 px, in three files.
3. **Two literal colours outside the token set** (§1): `#0b0e13`, `#2a3340`.
4. **Keyboard focus is thin.** `input:focus,select:focus,textarea:focus{outline:none;
   border-color:var(--acc)}` — focus is signalled by a 1 px border colour change, and there is no
   `:focus-visible` for buttons, links, or tab controls. Contrast passes; *focus appearance* is the
   weak point, and `outline:none` removes the browser's own remedy.
5. **Reduced-motion coverage is partial.** `dashboard.html` carries
   `@media (prefers-reduced-motion:reduce){.pm-dot,.lane-bar i,.spin{animation:none!important}}`;
   the job bar's spinner in `base.html` has no such rule, so it spins on every other page.
6. **No ribbon.** The hero's facts exist only on `/`. The job bar proves the pattern works — a
   persistent element on every page, with `main` padding reserved for it — and it is the model for
   the status ribbon.
7. **Visual claims are measured now, and only by one tool.** `tools/layout_measure.py` opens the
   portal in Chromium (Playwright, installed 2026-10-10 with the headless shell) and reports box
   geometry, fold coverage, column gaps and intersecting boxes. Measured at **1280x800 on the live
   portal**, top-level blocks only:

   | page | scroll height | blocks | above the fold | tallest block |
   | --- | --- | --- | --- | --- |
   | `/` | 1796 px | 4 | 1 | 769 px (llama.cpp router hero) |
   | `/models` | 1743 px | 7 | 2 | 850 px (library table) |
   | `/personalities` | 6800 px | 3 | 1 | **5298 px (the editor, 6.6 viewports in one block)** |
   | `/health` | 2048 px | 8 | 5 | 408 px |
   | `/settings` | 833 px | 4 | 4 | 193 px, with **1 pair of blocks intersecting** |
   | `/help` | 800 px | 1 | 1 | 590 px |

   Three facts come out of that. The status page puts **one** block above the fold on a laptop, so
   "what is running" is a scroll, not a glance. The personalities editor is a single block 6.6
   viewports tall, which is cramping in its purest form. And `/settings` renders two blocks whose
   boxes intersect — a defect, not a style preference.

8. **Type sizes are fractional, not scaled.** The same tool reports the font sizes computed on each
   page: `/` uses 12, 13, 14, 15 px *plus* 12.5 and 13.12 px; `/personalities` is dominated by
   12.48 px (588 elements) and 13.12 px (352); `/help` has 195 elements at 12 px. Page-local
   `rem`-multiplied sizes produce the fractions, and that is what a missing scale looks like when
   measured.

9. **No PTY host, no web terminal.** The pi harness page (`docs/PORTAL_LAYOUT.md` L4) needs a
   portal-restart-surviving terminal session, so the box was measured for it: `tmux` **absent**,
   `dtach` **absent**, `/usr/bin/script` present, `node` v22.22.1 present, `@xterm/xterm` not
   vendored, systemd **259** (`--pty` exists but proxies the *caller's* terminal, which is the
   opposite of detachable, so it is not the answer), `Linger=yes` (user units keep running after
   the operator logs out), and the user journal is **persistent**
   (`/var/log/journal/…/user-1000.journal`), so a session unit's transcript survives a reboot.
   `pi` carries the session identity the page needs (`--session-id <id>` creates it if missing,
   `--session <id>` and `--continue` reopen, sessions under
   `~/.pi/agent/sessions/<project-dir>/`), so resume is solved by pi; the *detachable live
   process* — a PTY the portal can attach to and let go of — is ours to build.

## 7. Verification

The rules in §4 are not decorations: they are enforced. `tests/test_ui_contract.py` asserts
source-level invariants (inline scripts parse, no `innerHTML` with data, functions named as the
spec claims, panel ids present, verbs gated); `tests/test_ui_integration.py` + `tests/ui/run.mjs` +
`tests/ui/spec.json` drive every control of every page in jsdom against a sandboxed portal and
report dead controls, uncaught dialogs and coverage gaps; `tests/ui/mutation_check.py` injects
realistic regressions and requires the suite to name them. A page slice that removes a guard fails
in CI.

What is verified, and by what: behaviour by the jsdom harness and the contract tests; contrast by the
ratios in §1; geometry by `tools/layout_measure.py`. What is **not** verified: keyboard focus
appearance (no tool asserts a focus ring today), and anything about how a page looks beyond box
geometry — the design-playbook evidence server (`observe*`) would add screenshots and the
accessibility tree, and it needs a project `.mcp.json` to run on this box.
