#!/usr/bin/env bash
# install_forge.sh - image-forge for Linux nodes: ComfyUI + venv +
# backend lane (ROCm / CUDA / CPU). Mirror of install_forge.ps1.
# Does NOT download models: use ../windows-forge/model_files.json via
# tools/fetch_models.sh discipline - ONE shared manifest, and the
# audit law from bench #46: every row resolves the same in install and
# verify (typed folder / dest). Disk-first law: check free space BEFORE
# any download.
#
# This script installs; it renders NOTHING. Rendering is gated by
# start_comfy.sh (RAM floor + router-busy) - the owner law for
# shared-memory boxes (2026-11-17).
set -eu
ROOT="${FORGE_ROOT:-$HOME/comfyui}"
LANE="${FORGE_LANE:-auto}"
DRY="${DRY:-0}"
run() { if [ "$DRY" = 1 ]; then echo "DRY: $*"; else "$@"; fi }

if [ "$LANE" = auto ]; then
  if lspci 2>/dev/null | grep -qi nvidia; then LANE=cuda
  elif ls /dev/dri/renderD* >/dev/null 2>&1 && lspci 2>/dev/null | grep -qiE "amd/radeon|amdgpu|ATI" ; then LANE=rocm
  else LANE=cpu; fi
fi
echo "lane: $LANE (override FORGE_LANE=cuda|rocm|cpu; DRY=1 to preview)"

[ -d "$ROOT/ComfyUI" ] || run git clone https://github.com/comfyanonymous/ComfyUI "$ROOT/ComfyUI"
[ -d "$ROOT/venv" ] || run python3 -m venv "$ROOT/venv"
IDX=""
case "$LANE" in
  cuda) IDX="--index-url https://download.pytorch.org/whl/cu126" ;;
  rocm) IDX="--index-url https://download.pytorch.org/whl/rocm6.3" ;;  # gfx1151 needs ROCm >= 6.4 userland: verify per node
  cpu)  IDX="--index-url https://download.pytorch.org/whl/cpu" ;;
esac
# shellcheck disable=SC2086
run "$ROOT/venv/bin/pip" install -r "$ROOT/ComfyUI/requirements.txt" $IDX
mkdir -p "$ROOT/ComfyUI/models" "$ROOT/logs"
printf 'lane=%s\ninstalled=%s\n' "$LANE" "$(date -Is)" > "$ROOT/lane.env"
echo
echo "installed. next steps (NOT automated - disk/RAM laws):"
echo "  1) models: fetch ../windows-forge/model_files.json rows (check free space FIRST;"
echo "     typed folders: vae, loras, diffusion_models, text_encoders, ...)"
echo "  2) launch: ../linux-forge/start_comfy.sh (enforces RAM floor + router-busy gate)"
echo "  3) verify: mirror verify_forge.ps1 discipline - installer and verifier"
echo "     must resolve every manifest row to the SAME path (bench #46 law)"
