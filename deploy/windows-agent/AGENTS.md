# Mission: make the image forge live on this box (RainbowBlock.local)

You are an agent on the Windows box with a 2080 Ti. Your job: bring up the
ComfyUI image forge from the kit in this folder, and keep it working. You
think with the Halo box's llama.cpp router (see `opencode.json`). The Halo
box runs the HALO portal (http://192.168.1.228:8090) and is the controller
of this whole setup: the forge exists so the portal can generate images on
this GPU over HTTP.

## Hard rules (each one was learned the expensive way)

1. **The shell is Windows PowerShell 5.1** (`powershell.exe`), NOT
   PowerShell 7. No ternary `? :`, no `??`, no PS7 syntax in any `.ps1`.
2. **All `.ps1` files stay pure ASCII.** 5.1 reads scripts as ANSI; UTF-8
   punctuation arrives as mojibake and breaks quoting. When editing, never
   introduce non-ASCII bytes; replace any you find (e.g. a `§` left as a
   bare `?` - write "sec 1").
3. **Keep expressions on one line inside `throw(...)`** and similar call
   sites; multi-line concatenation there breaks 5.1 parsing.
4. **The parser API:** `[System.Management.Automation.Language.Parser]::ParseInput`
   (the legacy `PSParser` class has `Tokenize` only).
5. Run `.\parse_check.ps1` (in this folder) after EVERY edit to any `.ps1`,
   before running that file. It must print PARSE OK for every file.
6. **The model physics of the router.** The router runs
   `--models-max 1`: exactly ONE model is resident. The model in
   `opencode.json` is that resident model - the same brain the portal
   harness runs on - so your session costs no load and no swap. Any
   OTHER catalog id you request forces the router to swap: unload the
   resident (the portal controller's brain), load the request, minutes
   of downtime for the portal. So: use the configured model, never
   `/model` to anything else. The config is GENERATED (by
   `tools/sync_agent_models.py` on the portal box) - if the owner swaps
   the resident model, they regenerate it; you never edit it.
7. **Never delete model weights** under `C:\forge\ComfyUI\models`. Disk is
   cheap here; deletion is the owner's call.
8. Run scripts as `powershell -ExecutionPolicy Bypass -File .\<name>.ps1`.

## Boot etiquette (do this before touching anything)

You are not the first machine to work on this system, and the Halo portal
remembers what it learned. Start from that memory, and add to it:

1. **Search before you invent.** `kb_search` for the task's nouns
   (for a PowerShell problem: `powershell ASCII parser`), and
   `memory_recent` for what the last sessions recorded. The lessons that
   cost four install runs are in there; citing a ref beats rediscovering it.
2. **Cite, don't paraphrase.** KB answers arrive with a `ref`;
   `kb_read` it before acting on a summary.
3. **Write one durable fact, not a transcript.** After a fix that a future
   machine would otherwise re-derive, `memory_add` it: what broke, the
   cause, the fix, the tag. Say which machine you are on in the `source`.
4. **Never write a secret.** Tokens, passwords, key-file contents. The door
   refuses them; do not test it.

The door is the portal's MCP over HTTP (`halo-kb` in your `opencode.json`,
issued on the portal's Settings page as an identity for this box). A read
identity can search and read; a write identity can also record.
`memory_remove` is on no token — forgetting is the portal's job.

## Steps (the kit is in this folder; its README.md has the full detail)

1. `.\parse_check.ps1` - all files must PARSE OK. If a file fails to
   parse: fix it per rules 1-4, re-run. The error line/column is given.
2. `.\install_forge.ps1 -IncludeGGUF` - idempotent; ~35 GB downloads from
   public Hugging Face repos (no login). If it installs git/Python via
   winget it stops on purpose: re-run to continue.
3. Copy the three bring-up models from the Halo box if the owner has not
   (they are listed in `model_files.json` under `copy_from_halo`; source
   is `halo.local` / `192.168.1.228` via SFTP; ~17 GB - the owner usually
   does this step by hand, ask in your report if they are missing).
4. `.\firewall_forge.ps1` (self-elevates) - inbound 8188 allowed ONLY from
   `192.168.1.228`. ComfyUI has NO authentication; this rule is the gate.
   Keep the IP, never a name (name rules freeze the IP at add time).
5. `.\register_autostart.ps1` - starts ComfyUI hidden at every logon.
6. `.\start_comfyui.ps1` - launches the engine (logs: `C:\forge\logs\`).
7. `.\verify_forge.ps1` - ALL PASS is the definition of done. It checks
   torch-CUDA, VRAM (11 GiB = RTX 2080 Ti), engine reply, and every model
   file in `model_files.json`.

## Definition of done

`verify_forge.ps1` prints all PASS, and `http://RainbowBlock.local:8188/system_stats`
answers. Then write a short report for the owner: what passed, what you
fixed along the way (paste parse errors you cured), what needs the owner.

## When something is broken (after bring-up)

Logs live in `C:\forge\logs\`. The engine dies mostly from VRAM pressure:
the memory contract is `--disable-smart-memory --preview-method none`, and
the bring-up trio is z-image-turbo bf16 + qwen3vl_4b_fp8 + ae VAE. The
fast tier is the `int8_convrot` files; the Q4_K_M + int8-TE combo is the
fits-native tier. Check the log tail before theorizing.
