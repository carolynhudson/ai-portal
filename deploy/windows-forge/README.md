# image forge — Windows remote backend (RainbowBlock.local)

The Windows half of the image-forge MCP plan
(`docs/IMAGE_FORGE_PROPOSAL.md` §1c): a CUDA ComfyUI backend the Halo box
drives over HTTP. This folder is the whole kit: inventory + install +
launch + firewall + autostart + verify. Copy the folder to the Windows box
and run the scripts in order.

## Layout

| file | what |
|---|---|
| `model_files.json` | the single model inventory: `hf` rows (installer downloads), `hf_optional_gguf` (the ~4.6 GB Q4 RAM tier, `--IncludeGGUF`), `copy_from_halo` (SFTP them, don't re-download) |
| `parse_check.ps1` | **run first.** Parses every `.ps1` in the folder with the real PowerShell parser, executing nothing; prints per-line parse errors and flags non-ASCII bytes. All three parse failures this kit died with on the target box (ANSI mojibake, a PS7 ternary, a multi-line `throw`) are visible here before an installer touches the disk |
| `install_forge.ps1` | git, Python ≥3.10 (winget if missing), ComfyUI clone, ComfyUI-GGUF node, venv + CUDA torch, model downloads. Idempotent; normal user; internet required (~35 GB) |
| `start_comfyui.ps1` | launches ComfyUI `--listen 0.0.0.0 --disable-smart-memory --preview-method none` (the portal's memory contract; `-LowVram` if needed) |
| `firewall_forge.ps1` | **admin** (self-elevates): inbound 8188 allowed ONLY from the portal box `192.168.1.228` — ComfyUI has no auth, the rule is the gate |
| `register_autostart.ps1` | user-level scheduled task: start hidden at logon (GPU lives in the desktop session) |
| `verify_forge.ps1` | torch-CUDA, engine reply, VRAM size, every model file from the inventory |

## Steps on RainbowBlock.local

1. **Copy this folder** (any SFTP tool) to e.g. `C:\forge\kit\`.
2. **Copy the bring-up models from halo.local** (the `copy_from_halo` rows
   in `model_files.json` — z-image-turbo bf16 + its Qwen3-VL-4B TE + the
   Flux `ae` VAE, ~17 GB total). Source paths are on the Halo box:
   - `~/ai/comfyui/models/diffusion_models/z_image_turbo_bf16.safetensors`
   - `~/ai/comfyui/models/text_encoders/qwen3vl_4b_fp8_scaled.safetensors`
   - `~/ai/comfyui/models/vae/ae.safetensors`

   Destination `C:\forge\ComfyUI\models\…` — that folder exists after step
   3, so copying them **after** the installer is the easy order.
3. **Install** (normal user, PowerShell):
   ```powershell
   cd C:\forge\kit
   Set-ExecutionPolicy -Scope Process Bypass -Force
   .\parse_check.ps1                   # preflight: every file must say PARSE OK
   .\install_forge.ps1                 # add -IncludeGGUF for the Q4 RAM tier
   ```
   If it installs git or Python via winget, it stops — re-run to continue.
4. **Firewall** (it self-elevates): `.\firewall_forge.ps1`
5. **Autostart**: `.\register_autostart.ps1` (starts at every logon)
6. **Start now**: `.\start_comfyui.ps1` (own window, logs to `C:\forge\logs\`)
7. **Verify on Windows**: `.\verify_forge.ps1` — expect all PASS.

## Verify from the portal (halo.local)

```bash
curl -s http://RainbowBlock.local:8188/system_stats
```

JSON with `system.cuda` = the 2080 Ti = the forge's remote backend is live.
Then point the portal at it (Settings → ComfyUI host, or
`~/ai/state/portal/` service conf) once P0 lands.

## Notes

- **2080 Ti is 11 GiB** (not 12) and sm_75: int8 tensor cores yes, fp8/bf16
  compute no → the official `int8_convrot` files are the fast tier; the
  Qwen-Image-2.1 Q4_K_M + int8-TE combo is the fits-native tier (~10-11
  peak). z-image-turbo fp8 is the bring-up test, tight fit.
- **Python**: installer prefers 3.11-3.13 via the `py` launcher, upgrades
  through winget if needed; the venv isolates everything.
- **ComfyUI-GGUF** is the one custom node installed (the Q4/Q8 loader).
  It is a supply-chain decision recorded in the proposal (§6 #4) — only
  city96's repo, pinned clone.
- **Security**: ComfyUI answers unauthenticated; inbound 8188 is firewalled
  to the portal IP alone. The rule pins an **IP, not `halo.local`**:
  `netsh` resolves a name once, at rule-add time, and the firewall does
  not re-resolve (and does not speak mDNS) — a name rule freezes whatever
  IP it saw and breaks silently when the lease moves. With a static lease
  the IP *is* the stable identifier. Keep the lease static.
- **File encoding**: all `.ps1` files are pure ASCII on purpose. Windows
  PowerShell 5.1 reads scripts as ANSI (system code page); a UTF-8 em-dash
  arrives as three mojibake bytes, breaks string quoting, and cascades into
  parse errors. Don't "prettify" these files with unicode punctuation.
- **Licensing**: the Qwen-Image-2.1 repos are public — no HF login needed
  to download. The qwen-research license text link on the repos currently
  404s (upstream metadata quirk); read the terms in the repo READMEs.
  Only if a repo ever turns gated would `hf auth login` be needed (the
  installer names that step on a 401/403).
- The portal's forge MCP (§5 P0) talks to `http://RainbowBlock.local:8188`
  as a config value — same HTTP API it uses locally.
