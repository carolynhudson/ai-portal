# linux-forge - image-forge for Linux nodes

The Linux mirror of `../windows-forge/`'s forge half. Scope per owner
(2026-11-17): install + operate, nothing one-off.

- `install_forge.sh` - ComfyUI + venv + lane (auto-detect cuda/rocm/cpu;
  `FORGE_LANE` overrides; `DRY=1` previews). No model downloads here:
  models come from the ONE shared manifest `../windows-forge/model_files.json`
  with free-space-first checking and typed-folder rows (bench #46 law:
  installer and verifier must resolve rows identically).
- `start_comfy.sh` - the launcher with the **RAM law** in code
  (owner, 2026-11-17): refuses under a RAM floor (default 24 GiB
  available, `FORGE_RAM_FLOOR_GIB`) or while a llama-router model is
  resident (llama-server RSS > 4 GiB - mesh law: router work and image
  renders are exclusive). Gates REFUSE; they never unload, never kill.

Status: kit only - halo bring-up (clone, venv, ROCm lane for gfx1151,
models ~35 GB) runs on explicit owner GO, gate tests DRY-only first.
