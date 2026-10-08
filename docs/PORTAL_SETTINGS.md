<!-- GENERATED FILE — do not edit by hand. -->
<!-- Written by app/core/settingsdoc.py from the settings registry (app/core/personality.py, -->
<!-- read through app/core/settingrows.py). Regenerate with: -->
<!--     .venv/bin/python -m app.core.settingsdoc --write -->
<!-- tests/test_settingsdoc.py compares the committed file against what the registry renders, so -->
<!-- a registry change that skips this file is a failing test, not a stale reference. -->

# Portal settings reference

Every key the portal has a definition for — which is the same set of keys a personality
can set, the settings editor can offer, and the generator validates against. The table
is for scanning; each key's own section below carries the rule sentence, the side
effect, the command-line spellings, the `presets.ini` name, and the measured finding
that justifies the default.

Written from the registry, so it cannot disagree with the form: the rows here are
`app.core.settingrows.rows()`, the same rows the editor renders and the renderer
validates (§6.14.1).

## 1. How to read a key

- **scope** — *global only* keys live in the router's own section of `presets.ini`;
  *model only* keys are per-model sections; *global and per-model* keys can be set
  either way, and the per-model value wins for that model.
- **portal default** — what the portal writes when a personality does not override the
  key. `—` means the portal writes nothing and lets the server decide.
- **llama-server default** — what the binary on this box defaults to, read from its own
  `--help` (see §6.12.26 for how that was measured). The two defaults are printed side
  by side because the gap between them is the whole reason a portal writes a key at all.
- **the no-op line** — a key whose value equals the server's default is *omitted* from
  the generated file. `llama-server` treats an absent preset key and a false one the
  same way, so writing the default is decoration (§4.10.0, measured against the build
  in §6.12.26).
- **offerable per model when** — the editor hides a row on a model whose index facts
  say the setting does not apply, e.g. `sliding_window` for the checkpoint keys.
- **not writable here** — a key the router owns (`host`, `port`, `api-key`, `alias`,
  `model`, `model-draft`, `hf-repo`, `hf-file`) is set at launch and overwritten there,
  so the editor never offers a control for one. These are **not registry keys**: nothing
  in `SETTINGS` defines them, which is why §4 prints the refusals they do get and why
  the form's answer is the short one.

## 2. Keys at a glance

| key | scope | type | portal default | llama-server default |
| --- | --- | --- | --- | --- |
| `cache-ram` | global and per-model | number (int) | `8192` MiB | `8192` MiB |
| `ctx-checkpoints` | global and per-model | number (int) | `32` | `32` |
| `checkpoint-min-step` | global and per-model | number (int) | `8192` tokens | `8192` tokens |
| `cache-idle-slots` | global and per-model | switch (bool) | `True` | `True` |
| `threads` | global and per-model | number (int) | `-1` | `-1` |
| `fit` | global and per-model | choice (choice) | `on` | `on` |
| `kv-unified` | global and per-model | choice (choice) | `auto` | `enabled` |
| `kv-unified-per-slot` | global and per-model | number (int) | `0` | `0` |
| `fit-ctx` | global and per-model | number (int) | `4096` tokens | `4096` tokens |
| `fit-target` | global and per-model | number (int) | `1024` MiB | `1024` MiB |
| `stop-timeout` | global and per-model | number (int) | `10` seconds | `10` seconds |
| `load-on-startup` | model only | switch (bool) | `False` | `False` |
| `dedup-cache-models` | global and per-model | switch (bool) | `False` | `False` |
| `ctx-size` | model only | number (int) | `0` tokens | `0` tokens |
| `n-gpu-layers` | global and per-model | number (choice-or-int) | `auto` | `auto` |
| `flash-attn` | global and per-model | choice (choice) | `auto` | `auto` |
| `parallel` | global and per-model | number (int) | `-1` | `-1` |
| `temp` | global and per-model | number (float) | `0.8` | `0.8` |
| `top-k` | global and per-model | number (int) | `40` tokens | `40` tokens |
| `top-p` | global and per-model | number (float) | `0.95` | `0.95` |
| `min-p` | global and per-model | number (float) | `0.05` | `0.05` |
| `presence-penalty` | global and per-model | number (float) | `0.0` | `0.0` |
| `repeat-penalty` | global and per-model | number (float) | `1.0` | `1.0` |
| `dry-multiplier` | global and per-model | number (float) | `0.0` | `0.0` |
| `cache-prompt` | global and per-model | switch (bool) | `True` | `True` |
| `reasoning-budget` | global and per-model | number (int) | `-1` tokens | `-1` tokens |
| `reasoning-effort` | global and per-model | choice (choice) | `default` | `default` |
| `jinja` | global and per-model | switch (bool) | `True` | `True` |
| `keep` | global and per-model | number (int) | `0` tokens | `0` tokens |
| `lazy-mode` | global and per-model | choice (choice) | `auto` | `auto` |
| `spec-type` | global and per-model | text (list) | `none` | `none` |
| `spec-draft-sampling` | global and per-model | choice (choice) | `greedy` | `greedy` |

## 3. Keys

### cache-ram

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `int`)
- **accepted**: -1 or more — range: `-1` or more MiB
- **portal default**: `8192` MiB
- **llama-server default**: `8192` MiB
- **spellings on the command line**: `-cram`
- **written into `presets.ini` as**: `cache_ram_mib`
- **the no-op line**: writing `8192` MiB emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Prompt cache limit, in MiB of system RAM. Default 8192. 0 disables it. This is not GPU memory.

*Side effect.* Charged against host RAM, which on this box is the same memory the aperture is carved from. Raising it does not spend GPU memory, it spends the machine.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.6`

### ctx-checkpoints

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `int`)
- **accepted**: 0 or more
- **portal default**: `32`
- **llama-server default**: `32`
- **spellings on the command line**: `-ctxcp`, `--swa-checkpoints`
- **written into `presets.ini` as**: `checkpoint_mib`
- **the no-op line**: writing `32` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).
- **offerable per model when** the model index reports `sliding_window` — on a model without it the row is noise, so the editor hides it.

Copies of a slot's cache, kept in system RAM. 32 per slot by default. Each costs 19-50 MiB here.

*Side effect.* Cost is per slot and constant -- it does not scale with context size or with how long a session runs, and it lands in host RAM, not the aperture (§6.12.6).

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.6`

### checkpoint-min-step

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `int`)
- **accepted**: 0 or more — range: `0` or more tokens
- **portal default**: `8192` tokens
- **llama-server default**: `8192` tokens
- **spellings on the command line**: `-cms`
- **the no-op line**: writing `8192` tokens emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).
- **offerable per model when** the model index reports `sliding_window` — on a model without it the row is noise, so the editor hides it.

Minimum tokens between cache checkpoints. Changes how often they are made, not how big each is.

*Side effect.* Lower spacing means more checkpoints alive at once. The multiplier is this number, not the context length.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.6`

### cache-idle-slots

- **scope**: global and per-model
- **type**: switch (llama.cpp parameter type `bool`)
- **accepted**: true or false — the setting is a switch, and false is the server's unset state, so writing false changes nothing (§6.12.26)
- **portal default**: `True`
- **llama-server default**: `True`
- **spellings on the command line**: `--no-cache-idle-slots`
- **written into `presets.ini` as**: `cache_ram_mib`
- **the no-op line**: writing `True` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Save an idle slot's state when a new task starts. Costs system RAM, and needs cache-ram set.

*Side effect.* This is what lets a finished conversation keep holding RAM. Leaving it on is the reason an idle model can cost more than an active one.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.6`

### threads

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `int`)
- **accepted**: -1 or more
- **portal default**: `-1`
- **llama-server default**: `-1`
- **spellings on the command line**: `-t`
- **written into `presets.ini` as**: `threads_default`
- **the no-op line**: writing `-1` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

CPU threads for the prompt pass. Threads 1 is the fastest value measured here, so set it on purpose.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.6`

### fit

- **scope**: global and per-model
- **type**: choice (llama.cpp parameter type `choice`)
- **accepted**: one of: on, off
- **portal default**: `on`
- **llama-server default**: `on`
- **spellings on the command line**: `-fit`
- **the no-op line**: writing `on` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Shrink unset settings so the model fits the device. It reads what other programs hold. It can cut your context.

*Side effect.* May silently choose a smaller context than the model can address. A load can satisfy fit's target and still fail, because fit's projection omits the compute graph allocated after it (§6.12.7).

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.7`

### kv-unified

- **scope**: global and per-model
- **type**: choice (llama.cpp parameter type `choice`)
- **accepted**: one of: on, off, auto
- **portal default**: `auto`
- **llama-server default**: `enabled`
- **spellings on the command line**: `-kvu`, `--no-kv-unified`
- **the no-op line**: writing `auto` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).
- **preset-mapped**: emitted under the name the preset layer expects.

Shares one cache pool across slots. On: a client can use the whole pool. Off: it gets one slice. Auto also turns it on — measured, not server-decided. To let the server decide, remove this key from the preset.

*Side effect.* Decides whether ctx-size is a window each slot can reach or a total to be divided: unified, window = pool; sliced, window = pool / slots. The measured difference on the same file and the same ctx-size is 32768 versus 16384 (§6.12.20). In a preset the server rewrites the value: on, true and auto all reach the worker as the bare --kv-unified; off reaches it as --no-kv-unified (§6.12.21).

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.21, llama-cpp.cpp:353-361`

### kv-unified-per-slot

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `int`)
- **accepted**: 0 or more
- **portal default**: `0`
- **llama-server default**: `0`
- **the no-op line**: writing `0` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Tokens each slot may use. With ctx-size unset, the pool is slots x this number.

*Side effect.* It caps a slot below the model's trained context by design -- that is the point, and the cost: KV paid per slot caps each client, and a value above the pool's per-slot share is reported as having no effect. Set ctx-size alongside it to price the pool you mean (§6.12.20).

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.15`

### fit-ctx

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `int`)
- **accepted**: 0 or more — range: `0` or more tokens
- **portal default**: `4096` tokens
- **llama-server default**: `4096` tokens
- **spellings on the command line**: `-fitc`
- **the no-op line**: writing `4096` tokens emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

The smallest context fit may shrink a slot to. Works only when ctx-size is unset.

*Side effect.* It is the floor of a reduction nobody sees in a normal log: with ctx-size unset and the aperture short, fit interpolates the pool down toward slots x this number and logs it at trace level only. Lower it and a tight box can be allowed to shrink a model further (§6.12.20).

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.7`

### fit-target

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `int`)
- **accepted**: int
- **portal default**: `1024` MiB
- **llama-server default**: `1024` MiB
- **spellings on the command line**: `-fitt`
- **the no-op line**: writing `1024` MiB emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Margin in MiB the fit adjustment leaves per device. Comma-separated for a per-device list; one value applies to every device.

*Side effect.* Raising it makes fit shrink context sooner. It reserves aperture, not host RAM -- and the prompt cache and checkpoints live in host RAM, so a generous target does not prevent the kills this box actually gets.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.7`

### stop-timeout

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `int`)
- **accepted**: 0 or more — range: `0` or more seconds
- **portal default**: `10` seconds
- **llama-server default**: `10` seconds
- **the no-op line**: writing `10` seconds emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).
- **preset-only**: the server accepts this key through a preset file (a section of `presets.ini`), not as a loose server argument.

Seconds to wait after a requested unload before forcing termination. Preset-exclusive: it is not a command-line argument at all.

*Side effect.* A deadline that ends in a force-kill: unloading at the timeout stops a generation in flight rather than waiting for it (§6.12.2).

*Evidence.* `tools/server/server-models.cpp:43 (router-only key)`

### load-on-startup

- **scope**: model only
- **type**: switch (llama.cpp parameter type `bool`)
- **accepted**: true or false — the setting is a switch, and false is the server's unset state, so writing false changes nothing (§6.12.26)
- **portal default**: `False`
- **llama-server default**: `False`
- **the no-op line**: writing `False` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).
- **preset-only**: the server accepts this key through a preset file (a section of `presets.ini`), not as a loose server argument.

Load this model when the server starts, not on the first request. Read at startup only.

*Side effect.* Decides what is resident before the first request. Startup loads are not capped by models-max -- measured, two began with models-max = 1 in the same file. It belongs in a model's own section: in [*] it reaches cached models the personality never named (§4.10.2).

*Evidence.* `tools/server/server-models.cpp:43 (router-only key)`

### dedup-cache-models

- **scope**: global and per-model
- **type**: switch (llama.cpp parameter type `bool`)
- **accepted**: true or false — the setting is a switch, and false is the server's unset state, so writing false changes nothing (§6.12.26)
- **portal default**: `False`
- **llama-server default**: `False`
- **the no-op line**: writing `False` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).
- **preset-only**: the server accepts this key through a preset file (a section of `presets.ini`), not as a loose server argument.

When a preset's hf-repo points at a model already downloaded, hide the cached entry from GET /models. Preset-exclusive.

*Side effect.* Only the listing changes: the model stays on disk and still resolvable.

*Evidence.* `tools/server/server-models.cpp (router-only key)`

### ctx-size

- **scope**: model only
- **type**: number (llama.cpp parameter type `int`)
- **accepted**: 0 or more — range: `0` or more tokens
- **portal default**: `0` tokens
- **llama-server default**: `0` tokens
- **spellings on the command line**: `-c`
- **written into `presets.ini` as**: `ctx_trained`
- **the no-op line**: writing `0` tokens emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Total tokens the server shares among its slots. A client gets this divided by the slot count.

*Side effect.* Two opposite silences, in both directions (§6.12.20). UNSET: fit sizes the pool to slots x trained when it can afford it and interpolates it DOWN when it cannot, logging the reduction at trace level only -- that is the report of 'the router says this model has half the context it used to report', with no setting set. SET: fit is locked out (arg.cpp:1643-1645) and this becomes the model's ceiling; nothing raises it back, and a value below the trained context removes capability quietly (§4.10.1).

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.20`

### n-gpu-layers

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `choice-or-int`)
- **accepted**: or auto, all; 0 or more — range: `0` or more — specials: `auto`, `all`
- **portal default**: `auto`
- **llama-server default**: `auto`
- **spellings on the command line**: `-ngl`, `--gpu-layers`
- **the no-op line**: writing `auto` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Layers loaded to the GPU: 99 loads all of them. Lower numbers move work to system RAM.

*Side effect.* Weight layers moved out of the aperture become anonymous, unreclaimable RAM that cannot be paged out -- the signature every OOM kill in this box's journal carries (§6.12).

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.18`

### flash-attn

- **scope**: global and per-model
- **type**: choice (llama.cpp parameter type `choice`)
- **accepted**: one of: on, off, auto
- **portal default**: `auto`
- **llama-server default**: `auto`
- **spellings on the command line**: `-fa`
- **the no-op line**: writing `auto` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Speeds up attention. Needs v4: on a v3 GPU it makes output worse, not faster.

*Side effect.* A performance lever with a capacity side effect: it changes the attention path a long context is allocated through.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.19`

### parallel

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `int`)
- **accepted**: -1 or more
- **portal default**: `-1`
- **llama-server default**: `-1`
- **spellings on the command line**: `-np`
- **the no-op line**: writing `-1` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Slot count. Each slot gets its own window, so 2 slots halves what one client gets.

*Side effect.* THIS SETTING DIVIDES THE CONTEXT, and only when you set it. Naming a slot count turns the unified pool off (kv_unified defaults false), and ctx-size is then shared out: ctx-size 32768 at 2 slots is a 16384-token window per client, with llama.cpp logging 'the full capacity of the model will not be utilized'. It also multiplies the per-slot caches (§6.12.6). Leave it auto, or set kv-unified with it (§6.12.20).

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.20, server.cpp:161-166`

### temp

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `float`)
- **accepted**: 0.0 .. 2.0 — range: between `0.0` and `2.0`
- **portal default**: `0.8`
- **llama-server default**: `0.8`
- **the no-op line**: writing `0.8` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

0.0 repeats the same answer. 1.0 wanders. 0.7 sits between.

*Side effect.* Quality, not capacity. Nothing here limits context.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.5`

### top-k

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `int`)
- **accepted**: 0 or more — range: `0` or more tokens
- **portal default**: `40` tokens
- **llama-server default**: `40` tokens
- **the no-op line**: writing `40` tokens emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Pick from the N most likely next words. 40 is the default; 0 means no limit.

*Side effect.* Quality, not capacity.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.5`

### top-p

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `float`)
- **accepted**: 0.0 .. 1.0 — range: between `0.0` and `1.0`
- **portal default**: `0.95`
- **llama-server default**: `0.95`
- **the no-op line**: writing `0.95` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Sample from the smallest set of tokens whose cumulative probability reaches p; 1.0 disables.

*Side effect.* Quality, not capacity.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.5`

### min-p

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `float`)
- **accepted**: 0.0 .. 1.0 — range: between `0.0` and `1.0`
- **portal default**: `0.05`
- **llama-server default**: `0.05`
- **the no-op line**: writing `0.05` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Drop answers below this odds. 0.05 keeps more words than 0.1.

*Side effect.* Quality, not capacity.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.5`

### presence-penalty

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `float`)
- **accepted**: float
- **portal default**: `0.0`
- **llama-server default**: `0.0`
- **the no-op line**: writing `0.0` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Penalty on tokens already present in the context.

*Side effect.* Quality, not capacity.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.5`

### repeat-penalty

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `float`)
- **accepted**: float
- **portal default**: `1.0`
- **llama-server default**: `1.0`
- **the no-op line**: writing `1.0` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Push down words you just used. 1.0 is off.

*Side effect.* Quality, not capacity.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.5`

### dry-multiplier

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `float`)
- **accepted**: 0.0 or more
- **portal default**: `0.0`
- **llama-server default**: `0.0`
- **the no-op line**: writing `0.0` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Penalise phrases you repeat. 1.0 is off. 1.5 stops loops sooner.

*Side effect.* Quality, not capacity.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.5`

### cache-prompt

- **scope**: global and per-model
- **type**: switch (llama.cpp parameter type `bool`)
- **accepted**: true or false — the setting is a switch, and false is the server's unset state, so writing false changes nothing (§6.12.26)
- **portal default**: `True`
- **llama-server default**: `True`
- **spellings on the command line**: `--no-cache-prompt`
- **the no-op line**: writing `True` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Keep a finished slot's state so a similar request answers faster. Paid in host RAM after the request ends.

*Side effect.* The speed it buys is paid for in host RAM held after the request ends.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.19`

### reasoning-budget

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `int`)
- **accepted**: -1 or more — range: `-1` or more tokens
- **portal default**: `-1` tokens
- **llama-server default**: `-1` tokens
- **the no-op line**: writing `-1` tokens emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Tokens the model may spend on private reasoning. -1 means no limit. The server rejects 0.

*Side effect.* Session cost, not load cost: it cannot make a model cheaper to bring up.

*Evidence.* `docs/PORTAL_PLAN.md sec 4.10.3, server.cpp:360-363`

### reasoning-effort

- **scope**: global and per-model
- **type**: choice (llama.cpp parameter type `choice`)
- **accepted**: one of: default, minimal, low, medium, high, xhigh, max
- **portal default**: `default`
- **llama-server default**: `default`
- **the no-op line**: writing `default` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Reasoning level passed to the chat template. Qwen3.8-Flash-Next obeys this one, not reasoning-budget.

*Side effect.* Session cost, not load cost -- and only honoured at all if the model's template reads the field.

*Evidence.* `docs/PORTAL_PLAN.md sec 4.10.3`

### jinja

- **scope**: global and per-model
- **type**: switch (llama.cpp parameter type `bool`)
- **accepted**: true or false — the setting is a switch, and false is the server's unset state, so writing false changes nothing (§6.12.26)
- **portal default**: `True`
- **llama-server default**: `True`
- **spellings on the command line**: `--no-jinja`
- **the no-op line**: writing `True` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).
- **offerable per model when** the model index reports `chat_template` — on a model without it the row is noise, so the editor hides it.

Serve the model's own chat template, with tool calling. Off, tool calls stop working.

*Side effect.* Off removes the template's role and tool handling: the tokens are still in the prompt, nothing interprets them.

*Evidence.* `docs/PORTAL_PLAN.md sec 4.10.5`

### keep

- **scope**: global and per-model
- **type**: number (llama.cpp parameter type `int`)
- **accepted**: -1 or more — range: `-1` or more tokens
- **portal default**: `0` tokens
- **llama-server default**: `0` tokens
- **the no-op line**: writing `0` tokens emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Keep the last N tokens of a conversation. 0 ends a session when it runs out of room.

*Side effect.* Pinned tokens are charged against the slot's context for the whole session, not for the turns that used them.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.19`

### lazy-mode

- **scope**: global and per-model
- **type**: choice (llama.cpp parameter type `choice`)
- **accepted**: one of: on, off, auto
- **portal default**: `auto`
- **llama-server default**: `auto`
- **spellings on the command line**: `-lzm`
- **the no-op line**: writing `auto` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Read big tensors from disk when needed, not from memory. auto applies above 4 GiB. on applies always.

*Side effect.* Measured on this box: 80.5 GiB of aperture with lazy-mode unset against 54.5 GiB with it on, same model and build -- weights stay in reclaimable page cache instead of pinned buffers. A kernel upgrade landed at the same time, so that gap is not attributable to this flag alone.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.18`

### spec-type

- **scope**: global and per-model
- **type**: text (llama.cpp parameter type `list`)
- **accepted**: comma-separated of: none, draft-simple, draft-eagle3, draft-mtp, draft-dflash, draft-dspark, ngram-simple, ngram-map-k, ngram-map-k4v, ngram-mod, ngram-cache
- **portal default**: `none`
- **llama-server default**: `none`
- **written into `presets.ini` as**: `spec_types`
- **the no-op line**: writing `none` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

Speculative decoding to run: a draft guesses the next tokens and the target verifies them. Comma-separated.

*Side effect.* A draft file costs aperture alongside the target. A file carrying its own head (the index's nextn_layers, §4.9.3) needs no second model; the 177B daily model on this box does not carry one, so a draft spec for it buys speed with a second resident file.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.11, sec 6.12.17`

### spec-draft-sampling

- **scope**: global and per-model
- **type**: choice (llama.cpp parameter type `choice`)
- **accepted**: one of: greedy, probabilistic
- **portal default**: `greedy`
- **llama-server default**: `greedy`
- **the no-op line**: writing `greedy` emits nothing — that is what the server already does, so the line would look like a change while changing nothing (§4.10.0).

How the draft's guess is made: greedy takes its argmax, probabilistic samples it and lets the target verify by rejection sampling.

*Side effect.* Changes how many guesses are accepted, not what the draft costs. Meaningless with no speculative decoding running at all -- it is a knob on a draft, not a way to start one.

*Evidence.* `docs/PORTAL_PLAN.md sec 6.12.26`

## 4. Keys the page will never offer

The router passes these itself when it launches, so a personality that sets them is
setting something that gets overwritten. Two boundaries refuse them, and they are not
the same answer, so both are printed here rather than one of them:

- **the settings form** refuses with the message a key nothing defines gets:
  `host: no definition for this setting`
  They are absent from `SETTINGS`, so the form has no rule to check them against, and
  the message is honest about not knowing — which is the difference between refusing and
  accepting a value and then losing it.
- **the generator** refuses the whole file. `personality.render()` raises before writing,
  naming the keys and the reason (`the router overwrites them`), in the global section and
  in a model section alike. That refusal is the one that protects `presets.ini`, and it
  is guarded by tests, not by this document.

`alias` is the one exception, and it is a narrow one: the portal writes it as a model
config's *name*, never as a setting a person typed (§4.10.4).

- `alias`
- `api-key`
- `api-key-file`
- `hf-file`
- `hf-repo`
- `host`
- `model`
- `model-draft`
- `port`

Derived by the router's own launch, not stored as a setting:

- `metrics`
- `mmproj`
- `model`
