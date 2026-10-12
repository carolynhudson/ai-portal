# Portal design and workflow debt — the operator's list, measured

The operator enumerated the portal's visible design and workflow debt on 2026-10-12, page by page.
This document is that list kept as the authority for polish, with each item paired to a measurement
taken on this box the same day, a proposed shape, and a canonical queue number
(`docs/PORTAL_PLAN.md` §0.1). Nothing here is scheduled ahead of the harness work (30–32); the
personalities workflow (33) needs the operator's agreement before code, and the model inventory
rework (45) is deliberately the slowest item because of how this library was lost once.

**How the measurements were taken.** Geometry: `tools/layout_measure.py` (Chromium at 1280×800,
read-only — it navigates, logs in, measures; it never clicks a verb). Named-item detail: a
Playwright probe against the live portal (`/tmp/probe_debt.py`, `/tmp/probe_grid.py`), reading the
rendered boxes of specific tables and panels. Server-side numbers: `/api/metrics` on the live
portal. Everything below is reproducible; the numbers are from 2026-10-12.

**The general shape of the complaint, in one measurement.** `/personalities` is **6,800 px** of
scroll at 1280×800, and one block — the settings editor — is **5,298 px**, i.e. **6.6 viewports in
a single block**. The layout brief (`docs/PORTAL_LAYOUT.md` L6) asks for at most three. That is the
"features packed together" measure, and it is the reason polish gets queue numbers instead of being
folded into feature slices.

## 1. Status page (`/`) — canonical queue 40

| Operator's observation | Measured on this box |
| -------------------------- | -------------------------------------------------------------- |
| The router hero is very important and very busy, and takes most of the initial view | Hero block is **769 px** of a **1,796 px** page: 43% of the page and the only block above the fold |
| Break it up, collapsibly, moving less important data out of the way | The hero carries state + census + slots + tokens + TPS chart + controls in one card; the page's other blocks are 417, 57 and 331 px |
| Machine detail starts closed, buried under the heroes, and the chart only populates once you expand it | Confirmed in code: `startMachinePoll()` polls only `if (d && d.open …)`, and history (`machineHist`) is browser-side, so every page load starts with zero samples. Open the pane and you get a flat line you waited for |
| AI work pegs GPU/memory and leaves CPU and disk idle, so a constantly polled CPU line chart is the wrong instrument | `/api/metrics` on this box right now: CPU **3.9%**, load 2.66, RAM **58.2%** used, **VRAM 523,329,536 / 536,870,912 = 97% used**, **GTT pool 61.1 GB / 115.9 GB = 53%**, disk 35% |
| We need the critical metrics visible, occasionally updated, lighting up when things get dangerous | Accepted as the requirement. The parts that already exist: `metrics_poll_ms` and `router_poll_ms` portal settings; `health.aperture_advisory` compares `gtt_used`/`gtt_total` to the amdgpu default |

**Proposed shape (queue 40).**

- **Vitals strip, always visible, above the hero.** Four tiles, no chart: **GPU pool** (GTT used of
  total), **VRAM**, **System RAM** (used of total, available), **Disk** (`/` and `~/ai`). Each tile
  carries a state — `ok` / `watch` / `danger` — from thresholds that are portal settings (queue 41),
  and a last-sample stamp. The strip polls at `metrics_poll_ms`; it is not a 5-second line chart.
- **Who is using it.** One line: the top RSS processes with their model id, which the router census
  already names, so "GPU pool at 53%" is answerable with "the 77.9 G model in slot 8080".
- **The hero becomes three sections** — *Serving* (state, model, slot, controls), *Who the router
  is* (the census sentence, queue 6.14.1l), *Load* (tokens/TPS) — the last two collapsed by default
  and opened on demand. The hero keeps the state, the model name and the start/stop verbs.
- **Machine detail keeps the chart, fixed at the source**: a server-side ring buffer (samples taken
  on every `/api/metrics` read, so the dashboard's own polling builds the history, ~180 points at the
  current interval) served by `/api/metrics/history`. Open the pane and the trend is already there.
  Disk and GPU stay as they are: read at page load, stamped with when.

**Thresholds to be decided with the operator** (proposed defaults, as settings): GPU pool ≥ 85%
danger, ≥ 70% watch; VRAM ≥ 90% danger; RAM ≥ 90% danger, ≥ 75% watch; disk ≥ 90% danger. On this
box, VRAM sits near 97% by design (an iGPU with 512 MiB), so the *pool* is the number that
matters — the strip should show both and warn on the pool.

## 2. Tool Repository Update Status — canonical queue 40b

Solid and functional as designed; no rework. One addition: a **harness row** (opencode) beside
llama.cpp, ComfyUI, huggingface-cli, Python/venv and GPU drivers, so the host's version and update
path sit where the operator already goes for tool health. It shares queue 30's unit and its health
probe; no new page.

## 3. Models page — the Hugging Face search / download flow — canonical queue 45

The operator's report is confirmed: the search-and-select path sends you into the downloads pane in
**ComfyUI safetensors mode** when you chose a llama.cpp model. Read the flow in
`web/templates/models.html` and `app/core/modeladd.py` before touching it.

**This is the slowest item on the list, deliberately.** The llama.cpp model library was wiped once.
Any inventory system for it inherits that fact, so the rules are written before the features:

- **The portal never deletes a model file.** No verb removes anything from the hub cache or
  `~/ai/models/llama`; a "remove" is a portal-side bookkeeping change with the file path printed and
  the operator told what it now points at.
- **Every mutation is planned, shown, then run** (the Projects page's pattern: plan → preview the
  exact writes → apply), and writes are additive only: new file, new section, new index entry.
- `presets.ini` keeps `*.portal-prev`; the index keeps a timestamped copy per write (it already
  does, §6.12.10).
- The download flow names the target: which library the file lands in, which section the preset gets,
  and what the router will be told. A llama.cpp pick must not inherit a ComfyUI destination.

Connected to personalities and MCP by design: the inventory answers "which models exist, which are
registered, which a personality can afford" — the same three reads the personalities deck and the
harness launch already use (`routerctl`, `modelindex`, `personality.fits`). Queue 45 is planned as a
design brief first, reviewed by the operator, then built in slices.

## 4. Models page — the llama.cpp datagrid — canonical queue 43

| Operator's observation | Measured on this box |
| -------------------------- | -------------------------------------------------------------- |
| The frozen "model id" column is too long to be a row identifier | Column 2 is **352 px** wide; its content is **487–542 px** (`scrollWidth`) — it overflows by 135–190 px on every row |
| Model names exceed the column's right edge and sit on top of the scrolling columns | Column 2 is `position:sticky; left:2.5rem`, so the overflowing text is the frozen paint — the overlap the operator sees is structural, not a rendering glitch |
| The source tag "(hub cache)" belongs in its own column, or with the "live" tag | Confirmed: the source tag shares the model-id cell |
| "paired" and "capabilities" look like repeats with inconsistent formatting | Confirmed in one row: `paired` renders `chat system tool-results tools`, `capabilities` renders `vision · ssm · 2-part · autocomplete · tools` — two columns of adjacent words, no shared vocabulary, 304 px and 499 px |

**Proposed shape (queue 43).**

- **Row identity is the short model name** (the last path segment, quant dropped —
  `Qwen3.8-Flash-Next`), in a narrow sticky column that cannot overflow, with the full router id
  (`ISTA-DASLab/…:IQ3_XXS`) in the row's detail line where it is copyable.
- **Source becomes a column** (`hub cache` / `library` / `path`), sitting beside the live light,
  because both answer "where is this model and is it usable right now".
- **One capability column, one vocabulary.** `paired` stops being a list and becomes *what the
  pairing adds* — `+ embed:embeddinggemma(Q8)` / `+ rerank:Qwen3-Reranker(Q8)` — and a plain
  `none` when nothing is paired. `capabilities` keeps the badge set (§4.9.3) with its
  "why this is absent" notes in the detail line. Two columns, two jobs, no repeated words.
- Frozen columns get a **measured** width: the grid contract test asserts `scrollWidth ≤ clientWidth`
  for sticky cells, so this class of overlap cannot come back silently.

## 5. Models page — the ComfyUI section — canonical queue 46

Sparse beside the llama.cpp grid: file sizes only, inside collapsible sections that mirror ComfyUI's
own folders. Proposal: a **columnar tree** — folder group rows (the ComfyUI subdirectory, count,
total size, mtime stamp) that expand to per-file rows (name, size, format/quant where readable,
scheduler-visible flag, and a "used by" column once the personality inventory knows). The tree keeps
the operator's stated liking for the folder-connected grouping and adds the details that are
currently invisible.

## 6. Health page — canonical queue 44

Mostly good, needs a refresh to match what the portal is now. The operator's specific suspicion is
confirmed by measurement.

- **The model store card reads as empty.** It renders `model directories 0` while also rendering
  `files in index 20 (274.1G)` and `Hugging Face …` — because it counts directories under
  `~/ai/models/llama`, which is where the library lived *before* the move to the hub cache. Fix: the
  card counts what the index knows, split by source (`hub cache 20 · library 0 · paths N`), with the
  "invisible to llama.cpp" line kept and a stamp.
- Add the harness row (opencode host: version, unit state, port, port-clash statement) and reflect
  the queue 30–32 features as they land. Re-read every card against the current subsystems: cards
  that describe a subsystem the portal no longer has get removed, not patched.

## 7. Personalities page — canonical queue 33 (design brief first)

The operator's requirement: **you know just by looking how to make a personality active, how to
create one, and how to change one.** Measured: 6,800 px page, editor block 5,298 px, deck 484 px,
and a reference table `Settings a personality can set` (420 px + a 68 px note) sitting between the
deck and the editor — the operator is right that it is reference material, and it already has a home
(`docs/PORTAL_SETTINGS.md`, rendered on `/help`).

**Proposed workflow shape — the four-state page.**

1. **Now.** A single strip, always at the top: the running personality, the model it is serving, the
   slot, and the verb that stops it. If nothing is running it says so and points at the deck. This is
   the "just by looking" answer for *what is active*.
2. **Deck.** The cards, and the primary verb on a card is **make active**. Each card states, without
   a click: which models it covers, the largest model's peak RAM against the aperture, and — when it
   cannot be served — the reason (the fits calculation the deck already computes). Create is a quiet
   card at the end of the deck, not a button that opens a 5,298 px block.
3. **Editor, opened by a verb.** Create/edit opens a panel with the three steps the editor already
   has (identity → models → settings) as **steps you move through**, each with its own confirm, the
   summary kept as a diff (`§6.14.1i`), and *Close* that returns to the deck. Nothing on the page is
   a 6-viewport block.
4. **Reference, not on this page.** The keys table moves to Help; the editor keeps one line — "what a
   personality may set, and where each key comes from → Help §4.7".

**The connection the operator asked for, named explicitly.** Personalities are how a project launches:
the harness (queue 32) binds a session to a personality, the personality supplies the model and its
settings, and the Projects page shows which personality a session launched with. So the personality
editor carries a **used by** line (projects and sessions that name it, from the projects registry)
and the launch pickers list personalities, not bare model ids. Design consequence: activation is a
first-class verb, not a side effect of the deck's raise action.

## 8. Settings page — canonical queue 41

The named annoyance is confirmed, numerically. The Network access table is **1,026 px** wide with
columns `service 84 · current 68 · bind 186 · port 116 · change 247 · (blank) 75 · live reachability
250`. The **service** cell carries **104 characters in 84 px**
(`llama.cpp router ai-llama-router · ~/ai/config/llamacpp.env no auth by default (optional
--api-key-file)`), and **live reachability** carries **358 characters in 250 px**. The **change**
column is **247 px for an empty input**. The layout doc's own measurement on `/settings` reports
**1 intersecting pair of boxes** at 1280×800 — text painted over text.

**Proposed shape.**

- **One mini-table per service**, in the operator's form: `service · bind · port · reachability` with
  footnote markers, and the details (unit name, env file path, auth note, ufw command, LAN probe
  results, restart state) below the mini-table at the matching marker. No cell carries prose.
- Column widths from content, asserted by the grid contract test: a cell's `scrollWidth` must not
  exceed its `clientWidth`.

**Settings as control, which is the operator's larger point.** The rule going forward: *every feature
asks whether its value belongs on this page.* Candidate keys for this slice, all already computed
somewhere in the portal today and none of them editable by the operator:

- `projects_root` — already a portal setting, editable, validated (queue 18). Pattern to copy.
- `metrics_poll_ms`, `router_poll_ms`, `router_metrics` — the polling rates exist in the settings
  defaults; the dashboard reads them. They need controls.
- **Vitals thresholds** (queue 40) — created as settings keys with the same validator discipline.
- **Services started at login** — which portal-managed units `default.target` wants
  (router, ComfyUI, portal, harness). Today that is a systemd fact the operator edits by hand.
- Presets page verb, harness config sync (queue 18's endpoints) as an explicit control with its
  "what will change" preview.

## 9. Help page — canonical queue 42

Confirmed hard: `.kb-toc` measures **1,024 px client width against 13,749 px of content** — the
section list is **13.4 viewports of horizontal scrolling**, 208 nodes in one ribbon — and the
**41 document links go nowhere** (`/help#4.7` style targets inside the same page, no viewer).

**Proposed shape.**

- **Left column: a tree of documents and their top-level headings**, expanded only to the highest
  importance headings, click to open in the viewer. Vertical, scrolling normally, deep-linkable.
- **Right: the document viewer**, which the operator gets today only as prose about the layout.
  Welcome text moves to the bottom of the Status page's KB row, not the whole right pane.
- **Search as a collapsed affordance**: a small "Search help" control that expands into the ribbon,
  runs over the KB index, and shows hits with their document and heading, then collapses back.
- Queue 33's key reference and queue 45's inventory notes land *here*, which is the point of fixing
  this page before sending reference material to it.

## Order, and what blocks what

| Queue | Item | Blocks / blocked by |
| ----- | ------------------------------------------- | ------------------------- |
| 30–32 | harness host, interface, `/pi` | the operator's stated priority; in progress |
| 33 | personalities workflow (brief first) | needs the operator's agreement on the four states |
| 40 | vitals strip + hero sections + server-side history | thresholds (41) as its settings |
| 41 | settings page as control (network mini-tables, rates, thresholds, start-up units) | 40's thresholds |
| 42 | help page as a documentation site | 33's reference move; §6.14.2 (queue 39) |
| 43 | models datagrid identity, source column, one capability vocabulary | 45 (inventory) |
| 44 | health page refresh, model store card by source | 30–32 for the harness row |
| 45 | model inventory management (design brief first, additive-only rules) | 43, 33 |
| 46 | ComfyUI models tree | 44 for shared "used by" reads |

Two of these are decisions before code, and the operator said so: the personalities workflow (33)
and the model inventory (45). The rest are fixable in place.

## What is deliberately not polish

- Per-job progress percentage — a cmake build has no honest denominator (plan §6.9).
- A desktop notification for finished jobs — the job bar is the notification.
- Removing the router hero — it is the page's answer to "is it up and what is it serving". Queue 40
  sections it, it does not remove it.
