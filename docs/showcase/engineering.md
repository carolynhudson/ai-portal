# Engineering

How a portal built by AI agents stays trustworthy enough to run a house.

## The stack, small on purpose

FastAPI, Jinja templates, inline JavaScript — no front-end build step, no
framework, no node_modules. A page is a template and a fetch. The reason
is testability: without a build step, the test suite can drive the real
pages. One SQLite base holds jobs, the model index, memories, mesh
identities, events, and the Workroom; FTS5 gives every store a word
recall, and an embedding sidecar (a small GGUF embedder on the router)
gives memory a semantic one. Hybrid recall blends the two; when the
embedder is down, the words still answer.

- Backend: [app/](../app/) — `main.py` routes, `core/` modules, one
  module per subject.
- Frontend: [web/templates/](../web/templates/) — server-rendered,
  progressive-enhanced, mobile-adaptive.

## Tests that touch the real thing

1,366 tests, five skipped, ~7 minutes. Three of them are the reason the
UI is good:

- **The DOM harness.** A sandbox portal runs against a throwaway home
  (fake models, fake services, a proxy that blocks the internet). Every
  page is then driven in a real DOM: every control is clicked; a control
  with no request, dialog, navigation, or visible change is a **dead
  control** and fails. POST shapes are recorded, not sent. Destructive
  requests must be confirm-gated *before* they happen.
- **The viewport probe.** Every page at phone, tablet, and desktop: zero
  horizontal overflow, 44-pixel tap targets, a nav that hides its words
  on small screens. The nav's seven-slot law exists because a probe once
  named an overflow in pixels.
- **The suite sandbox.** The tests cannot touch the real machine: a home
  that resolves to the operator's, or a database outside the suite's
  throwaway, stops the suite before it runs. The audit trail for that
  rule is two test runs that wrote into the live database in October.

The pre-commit hook runs the UI contract, so a template that breaks a
page never reaches the deploy.

## Laws written down

The journal ([PORTAL_PLAN.md](../PORTAL_PLAN.md), ~8,000 lines of
dated records) is where decisions become law, and the law is where the
tests come from. A few that shape the product:

- **Migration**: copy and verify before any delete; size + integrity +
  newer-wins; never move a router-loaded file; every move updates the
  index and the references.
- **Downloads**: plan the bytes, then verify them at the door; the
  receipt is the feature.
- **Mesh**: remote writes are quarantined until the owner promotes;
  answers that hide the quarantine are lies by omission.
- **Machine safety**: the portal reports apt and never applies it;
  nothing restarts without a confirm; a router model swap is the owner's
  act, not a side effect.
- **Secrets**: the password is generated once and never printed; tokens
  are shown once and stored hashed; the Workroom refuses secrets at
  write time; the secret net passes placeholders, because a net that
  fires on placeholders trains agents to route around it.

## The journal, the KB, and the agents

Every design doc, survey, and proposal is searchable from inside the
portal — and from the mesh door, so the coding agents (on this box and
remote ones) cite it instead of re-deriving it. The portal's own sessions
read yesterday's lessons; the Windows box stopped paying four install runs
re-learning PowerShell the day its lessons were recorded. The knowledge
base is the project's memory, and the door makes it shared.

- [START_HERE.md](../START_HERE.md) — the guided tour of the repo.
- [ENVIRONMENT.md](../ENVIRONMENT.md) — the machine, surveyed.
- [docs/](../) — the whole shelf.

## Deploy

Commit first — the deployment copies the checkout, so the commit *is* the
release. `./deploy/install.sh`, a unit restart, and the journal records
the round. Unpushed commits are the owner's to push; the portal never
touches the remote.

## Numbers, on the current build

- 1,366 tests passing, 5 skipped (deliberate, documented).
- 10 pages, 0 front-end dependencies, 0 telemetry.
- 21 models indexed on the live box; 2 mesh
  identities; 1 Windows box reporting measured results; 1 MacBook queued.
- An 88 GiB flagship model under the portal's law.
- 0 cloud inference calls. That is the point.
