# Start here — a friendly tour

New to this machine? This document is written for you: no jargon, no history,
just what this portal is and where to find things. Terms are explained in
[Words you'll meet](/help#words-you-ll-meet), and every claim here links to the
page it talks about — the navigation bar at the top reaches every page from anywhere.

## 1. Welcome

This is a personal AI workstation: one computer that runs large language
models (the things behind chat assistants and coding agents) and a few
related tools, all on your own network — no accounts, no subscriptions, no
data leaving the house.

**The portal** (the page you are reading is part of it) is the control panel
for that machine. It starts and stops the services, keeps the model library
tidy, launches coding agents inside your projects, watches health, and
explains itself — which is why every setting on every page carries a small
citation like “§6.12.22” that lands right here.

You are the operator. There is one password, kept in a file on the machine
(`/api/settings` tells you where), and the portal deliberately refuses to do
anything dangerous without asking: installs, restarts of live agents, and
model swaps are confirm-gated.

## 2. The portal in one paragraph

Three services do the work, and the portal is the panel that runs them. The
**router** is the model server: it holds one or more chat models ready and
answers OpenAI-compatible requests on port 8080, so any tool that can talk
to ChatGPT's API can talk to it. The **harness** is `opencode`, the coding
agent: give it a project and it reads, edits, and runs code using the router.
**ComfyUI** is the image studio on port 8188, off until you start it. The
Status page shows all three at a glance; [The pages](#3) walks the rest.

## 3. The pages, in the order you'll use them

- **[Status](/)** — the pulse. Which model is loaded right now, which agent
  slots are busy, whether the router/ComfyUI/harness are up, and the update
  panels. If you only open one page daily, open this one.
- **[Models](/models)** — the library: everything installed, everything in
  the Hugging Face cache, sizes and provenance. Upload files here; use the
  fit calculator here before loading anything big.
- **[Personalities](/personalities)** — saved model configurations ("slot
  cards"): context size, sampling, system prompt, vision support. Starting a
  personality is how a model becomes loaded and usable.
- **[Projects](/projects)** — your code folders. Launching an agent on a
  project opens a real coding session in the harness's own web app (the
  handoff link opens in a new tab).
- **[Health](/health)** — units, memory pressure, versions, disk. The page
  to open when something feels slow, stuck, or dead.
- **[Settings](/settings)** — the wiring: ports, hosts, passwords, service
  modes. Nothing here applies without a confirm, and most changes need a
  service restart (the page says which).
- **[Help](/help)** — this page: the tour above, the glossary, the settings
  reference, and the full engineering journal behind every decision.

## 4. Five minutes of useful things

1. On **[Status](/)**, press *start* on the router, then open its *Web UI*
   button — that is the raw model server, and the portal's button always
   gives your browser a link it can actually reach.
2. On **[Models](/models)**, open any installed model and read its facts:
   size, context limit, vision support. Big models show a fit verdict against
   this box's free memory.
3. On **[Personalities](/personalities)**, start one of the saved cards;
   watch the Status page's model line change to it. Starting a personality
   loads that model; the swap happens between sessions, never mid-conversation.
4. On **[Projects](/projects)**, press *open* on a folder — the coding agent
   opens in a new tab, already pointed at this machine's router.
5. Come back here and search “context” — every explanation, setting, and
   measurement about context windows answers in one list.

## 5. Words you'll meet

The [glossary](/help#words-you-ll-meet) explains the machine words in plain
sentences: [GGUF](/help#gguf), [quantization](/help#quantization),
[context window](/help#context-window), [KV cache](/help#kv-cache),
[vision (mmproj)](/help#mmproj), [slot and swap](/help#slot),
[personality](/help#personality), [router](/help#router),
[harness](/help#harness), [MCP](/help#mcp), and the rest. A term you meet on
any page links to the glossary entry; the glossary entry links to the exact
setting it explains.

## 6. How this Help page works

The left rail is the whole documentation set as an outline — this tour, the
glossary, the settings reference, and the deeper engineering documents
under *Reference & history*. The search box is at the top of the rail: type
and the outline narrows as you type; press Enter for ranked results across
every document. Clicking any heading shows it in the viewer on the right,
where **permalink** gives you a permanent link to exactly that section —
section numbers are the headings themselves, so a link made today keeps
pointing at the same prose after the docs grow. A citation like §6.12.22
from a settings page lands in this same viewer.

## 7. When something goes wrong

Open **[Health](/health)** first: it shows whether each service is running,
how much memory is free, and what the last jobs did. The common failure
patterns and how to read them: search [debugging](/help?q=debugging) and
[findings](/help?q=findings) — the guide and the environment notes answer in
ranked sections. Two facts cover most surprises: a model swap never
happens while an agent is mid-turn (the portal waits for a closed session),
and a refusal that names its arithmetic — a fit verdict, a 429, a
"busy" — is the portal protecting the machine, not failing.

## 8. Where the deep docs live

Everything in *Reference & history* in the outline is engineering: the
running journal (every measured finding, dated), the settings reference,
hardware notes, update pipeline, and research documents. They are written
for the operator who wants the measurement behind the decision — and the
short explanations on the pages themselves cite them, so a citation is
always one click from its evidence.
