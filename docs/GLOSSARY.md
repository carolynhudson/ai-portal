# Words you'll meet

Every technical word the portal uses, explained in two or three plain
sentences, with a pointer to where you'll meet it and — where it matters —
the exact setting it explains. Terms are alphabetized inside their group;
the outline on the left lists them all. This glossary is friendly by
design: the deep engineering documents state the measurements; this page
states what the words mean.

## Machines and memory

### vram

Video RAM: the fast memory on the graphics card. Models run fastest when
they fit entirely in it. This machine's card is modest, so big models live
in system RAM instead — slower, but workable; the portal's fit verdict
tells you which case a model is in.

### ram-and-swap

System memory: the box's working shelves. When RAM fills, Linux moves
idle pages to **swap** (disk), which is far slower. A machine that feels
fine can be swapped in ways only the [Health](/health) page shows.

### memavailable

The honest “how much room is left” number. It counts memory that can be
freed instantly, unlike “used”, which counts file caches Linux gives back
on demand. The portal's fit gate reads `MemAvailable` plus swap headroom —
which is why a refusal names its arithmetic instead of shrugging.

### mmap

Memory-mapping: loading a model file as if it were memory, letting the OS
page it in lazily. It makes “loading” a 20 GB model nearly instant and
keeps copies down, and it is why file-backed memory shows as evictable
rather than consumed on the [Health](/health) page.

## Model files

### gguf

The file format llama.cpp (this box's model server) reads: one file
holding the model's weights, its trained context limit, and its
vocabulary. The [Models](/models) page reads those facts out of the file
without loading it, which is how it can show sizes and limits instantly.

### quantization

Shrinking a model by storing its numbers with fewer bits. A “Q4_K_M”
model is a large model compressed to roughly a quarter of full size —
slightly worse quality, far less memory. The letter-quant pairs you see
(Q8_0, Q4_K_M, IQ3_S) are different recipes on that quality/size curve.

### precision-words

fp16/bf16/fp8/int8: formats for storing weights, each a size/quality
trade. On this box's AMD card, bf16 and fp8 run well; **int8 is the fast
tier on the newer NVIDIA card** (it has integer tensor cores). The portal
labels each file with its format so the fit math matches reality.

### safetensors

The format ComfyUI (the image studio) loads: model weights plus their
shapes, safe to memory-map. Model files on this box are GGUF for the text
router and safetensors for image pipelines — two ecosystems, two formats.

### mmproj

The small sidecar file that makes a text model *see*: it connects a
vision encoder to the model. A model with its mmproj can take image
attachments; the [Models](/models) page shows a “vision” marker on the
rows that have one. The harness picks the pairing up automatically.

### lora

A small add-on file that steers a base model — a style, a faster
sampling schedule (turbo/Lightning LoRAs in the image studio). Think
“clip-on attachment”: cheap to swap, useless without its base model.

### vae

The image pipeline's translator: it converts between the small
mathematical space a diffusion model works in and real pixels. A diffusion
model plus a text encoder plus a VAE is the standard three-part image
setup; the [Models](/models) page keeps them inventoried together.

### text-encoder

The model that turns your prompt into the instructions a diffusion model
understands. In image work, a weak text encoder is the usual reason a
prompt “didn’t listen”; the forge plans keep this fact visible.

### diffusion-model

The image generator's engine: it starts from noise and refines it over
some steps (8 for turbo models, 25-40 for base ones) into a picture. Fewer
steps = faster drafts; more steps = final quality.

## Running and talking to models

### context-window

The most tokens a model can consider at once — conversation, code, and
attachments included. Exceeding it truncates the oldest text. The trained
limit is the hard cap; the router's `ctx-size` setting (per personality)
is the practical one, and the fit math multiplies it into memory cost —
see [ctx-size](/help#ctx-size) and the plan's
[window sweep](/help#6.12.22).

### kv-cache

The model's short-term memory during a conversation: a table of computed
attentions it reuses so it doesn't recompute the whole history per token.
It costs real RAM proportional to context size — one reason a long context
makes a model heavy. The unified KV pool is how the router shares it
between slots: [kv-unified](/help#kv-unified).

### token

A small piece of word (~¾ of an English word). Model limits, speeds
(tokens per second), and costs are all counted in tokens.

### slot

A seat at the router where a model answers requests. Personalities map to
slots; the Status page shows which slot is loaded, busy, idle. Seats share
the machine, so the portal's memory math plans for them together.

### swap

Replacing the model in a slot with a different one. Expensive and
disruptive, so the portal refuses to swap mid-conversation: it waits for a
closed session. The Status page labels a queued swap “waiting”, not
“stuck”, and the personality row that requests it says so.

### personality

A saved model configuration card: which model, how big a context, what
system prompt, what sampling, vision on/off. Starting a personality is the
normal way to put a model to work; the [Personalities](/personalities)
page owns the cards and their settings rows.

### sampling-and-temperature

How the model picks its next word. **Temperature** is the dial
(≈0 = predictable, ≈1 = creative); top-k/top-p/min-p and penalties refine
the choice. Defaults live on the personality card —
[temp](/help#temp) and friends — and the portal's registry explains each
key with its measurement.

### seed

The starting number for a model's randomness: the same seed plus the same
prompt reproduces the same output. Useful when you're tuning a result and
want everything else fixed.

### vision

A model's ability to accept images as input. Requires the
[mmproj](/help#mmproj) pairing; the Models page marks capable rows, and
the harness shows image attachments to models that can read them.

### speculative-mtp

A speedup where the model proposes several tokens at once and checks them
in bulk. Some models ship a built-in version (MTP heads); the portal shows
the capability on the model's facts row and lets a personality request it:
[spec-type](/help#spec-type).

## The portal's services

### router

The llama.cpp server that holds models and answers OpenAI-compatible API
calls on port 8080. Every tool here — the harness, agents, scripts —
talks to it, which is why “the model” on the Status page is really
“what the router is holding”.

### harness

The coding agent: `opencode`, running as a service with a shared session
store. It does the agentic loop (read, edit, run, ask) using the router,
and its own web app is what the [Projects](/projects) page hands you into.
Its bind mode (loopback only, or open to the LAN) is a
Settings decision with a password, not a secret.

### mcp

Model Context Protocol: a small standard for plugging extra tools into an
agent. The portal itself offers one (the knowledge base) so agents can
read the docs above; the planned image forge adds another. Tools appear
to the agent as callable verbs.

### comfyui

The image studio service on port 8188: loads diffusion pipelines, runs
generation workflows, and keeps the models inventoried. It starts on
demand, not always-on, because its models want the same memory the chat
models do.

### systemd-unit

Linux's service manager: the thing that “is-active” means. Each portal
service (router, harness, ComfyUI, the portal itself) is a *unit* with a
name like `ai-llama-router`; the portal's start/stop buttons drive these,
and the Health page reads their truth directly.

## The portal's rules

### fit-gate

Before loading a model, the portal does the arithmetic — model size + KV
cache for the requested context vs `MemAvailable` + swap — and refuses
with the numbers printed when it won't fit. Refusals are the machine
speaking, not the portal failing.

### closed-session-swap

The safety rule that a model swap happens only when no agent is mid-turn.
A swap requested during a live session queues until the session closes;
the UI says “waiting”, so you know the machine is obeying, not wedged.

### confirm-gated

Destructive actions (restarts, upgrades, plugin changes, service installs)
ask first, and the portal never runs `apt` by itself. The confirm dialog
names exactly what will happen; the audit log records it afterward.

### citation

The “§6.12.22” on a settings row is a pointer into this help system —
section numbers are the documents' own headings, so a citation is a stable
address that survives document growth. A citation that lands on “No such
section” is a defect, and the test suite treats it as one.

### registry

The single source of truth for every setting key: its default, its
allowed spellings, its help sentence, and its evidence. The Settings and
Personalities pages render from it, this reference is generated from it,
and no page is allowed its own opinion about a key.

## Numbers on the screens

### tokens-per-second

Generation speed: how fast a model writes. The Status page shows it for
the active model; expect big resident models on this box to run in the
tens, and offloaded layers to trade speed for fit.

### 429

“Too many requests.” The router returns it when its capacity — slots,
queue, context pool — is saturated. It is a traffic-light, not an error:
the callers slow down, the machine recovers, the portal's log names the
saturation.

### queue

Requests waiting for their turn. A queue is normal; a queue that only
grows means the model in the slot can't keep up — check the active model,
the context sizes, and whether a swap is waiting.

### pressure

How close memory is to the edge. The [Health](/health) page shows the
memory picture and swap use; sustained pressure is the signal to close a
model, shrink a context, or wait for a job.
