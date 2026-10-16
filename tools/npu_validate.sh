#!/usr/bin/env bash
# NPU validation trial: prove the Strix Halo NPU is reachable, end-to-end, in layers.
# READ-ONLY: this script inspects and prints. Every command that CHANGES the system
# is printed for the owner to run, never executed — the AMD userspace stack wants
# root and targets Ubuntu 24.04; this box is 26.04 (packaging friction is expected,
# and system changes are the owner's call, in the closed-session discipline).
#
#   tools/npu_validate.sh            # report layers + next commands
#   tools/npu_validate.sh --parity   # also run the parity arms if an NPU endpoint answers
#
# The measured state this script exists to check, layer by layer (2026-10-16):
#   kernel: amdxdna 0.7.0 in-tree, bound to 0000:c4:00.1, firmware npu_7.sbin  [OK]
#   node:   accel driver initialized "on minor 0" — no /dev node auto-created  [MISSING]
#   user:   no XRT, no AMD runtime                                              [MISSING]
#   model:  no NPU-compiled model; llama.cpp XDNA runs aiegen models only      [MISSING]
#
# Decision gate (the owner's condition): NPU adoption is benchmark-gated by
# tools/npu_parity.py — embedding arms (docs/s, p50/p95) and, closed-session,
# the agent's prefill/decode tok/s idle vs under each arm's embedding stream.
# CPU baseline measured 2026-10-16: 111 docs/s. Numbers first, commitment second.
set -uo pipefail
PCI="0000:c4:00.1"
PORT_NPU_EMBED="${NPU_EMBED_URL:-http://127.0.0.1:8391/v1}"

say() { printf '%s\n' "$*"; }
ok()   { say "  [ok]      $*"; }
bad()  { say "  [missing] $*"; }
note() { say "  [note]    $*"; }

say "=== layer 1: kernel driver + firmware ==="
# grep -q + pipefail: grep exits early, the writer SIGPIPEs, the pipeline "fails"
# a healthy state (measured). Read /proc/modules directly: no pipe, no lie.
if grep -qc '^amdxdna ' /proc/modules >/dev/null 2>&1; then
  ok "amdxdna loaded: refcount $(awk '/^amdxdna /{print $3}' /proc/modules)"
else
  bad "amdxdna not loaded (/proc/modules)"
fi
if [ -e "/sys/bus/pci/devices/${PCI}/driver" ]; then
  ok "driver bound: $(readlink -f "/sys/bus/pci/devices/${PCI}/driver" | xargs basename)"
else
  bad "no driver bound to ${PCI}"
fi
fw=$(journalctl -k -b 0 --no-pager 2>/dev/null | grep -i 'amdnpu.*firmware\|npu_7.sbin' | tail -1)
[ -n "$fw" ] && ok "firmware line: ${fw:0:110}" || note "firmware journal line not found (journalctl -k may need root)"

say "=== layer 2: device node (the userspace door) ==="
node=""
for c in /dev/xdna0 /dev/dri/renderD129 /dev/dri/renderD130; do
  [ -e "$c" ] && node="$c" && break
done
if [ -n "$node" ]; then
  ok "node present: $node ($(stat -c '%A %U:%G' "$node"))"
else
  bad "no NPU node: nothing under /dev speaks to it (udev has no xdna rule on this box)"
  say "            -> the xrt deb ships the udev rule; after installing it:"
  say "               sudo udevadm control --reload && sudo udevadm trigger"
  say "               verify: ls -l /dev/xdna0 (or /dev/dri/renderD129)"
fi

say "=== layer 3: AMD userspace (XRT + AIE runtime) ==="
found=0
# /opt/amdgpu deliberately NOT counted: that is the GPU (ROCm) stack, and a
# first run proved the point — it made the NPU verdict read "layers present"
# while the NPU userspace was entirely absent.
for d in /opt/xilinx/xrt /opt/ryzenai; do
  [ -d "$d" ] && { ok "present: $d"; found=1; }
done
command -v xrt-smi >/dev/null && { ok "xrt-smi: $(command -v xrt-smi)"; found=1; }
[ "$found" = 0 ] && bad "no XRT / Ryzen AI runtime anywhere standard"
snap list 2>/dev/null | grep -qiE '^amd-xdna' && ok "snap amd-xdna installed" || note "snap amd-xdna not installed (Ubuntu's packaging of XRT; 24.04-targeted)"

say "=== layer 4: python ONNX path (the intended first workload: embeddings) ==="
if python3 -c "import onnxruntime" 2>/dev/null; then
  ok "onnxruntime $(python3 -c 'import onnxruntime as o; print(o.__version__)')"
  python3 -c "import onnxruntime as o; import sys; sys.exit(0 if 'CPUExecutionProvider' in o.get_available_providers() else 1)" 2>/dev/null
  np=$(python3 -c "import onnxruntime as o; print([p for p in o.get_available_providers() if 'Ryzen' in p or 'Vitis' in p])" 2>/dev/null)
  if [ "$np" = "[]" ] || [ -z "$np" ]; then bad "no Ryzen AI execution provider in onnxruntime"; else ok "NPU EP: $np"; fi
else
  bad "python3 onnxruntime absent (the venv may have it; run with the venv python)"
fi

say "=== verdict + the owner's next commands (NOT run by this script) ==="
if [ -z "$node" ] && [ "$found" = 0 ]; then
  say "  Kernel is ready; userspace is absent. Bring-up, in the order that isolates failures:"
  say "    1. AMD Ryzen AI Software for Ubuntu 24.04 (ryzenai.docs.amd.com): the xrt deb"
  say "       + aie-rt from the GA tarball; on 26.04 expect dependency friction — install"
  say "       into a 24.04 chroot/podman if apt refuses, and copy the udev rule out."
  say "    2. reload udev, confirm the node (layer 2 above must go [ok])."
  say "    3. xrt-smi examine must list the NPU — that is the 'driver works' proof."
  say "    4. embeddings ONNX int8 (AMD publishes gte/bge-family; EmbeddingGemma ONNX is"
  say "       convertible) behind /v1/embeddings on port 8391 — a thin python server, the"
  say "       same interface kb_mcp already speaks."
  say "    5. RERUN THIS SCRIPT with --parity, closed-session, to run the arms:"
  say "         tools/embed_sidecar.sh start                     # CPU arm endpoint"
  say "         <the NPU /v1/embeddings server on 8391>          # NPU arm endpoint"
  say "         .venv/bin/python tools/npu_parity.py --skip-agent \\"
  say "             --embed-endpoint cpu=http://127.0.0.1:8390/v1 \\"
  say "             --embed-endpoint npu=${PORT_NPU_EMBED}"
  say "       then the closed-session full arms (agent idle vs under load) per the header."
else
  say "  Layers present. Run the parity harness (header); numbers decide."
fi

if [ "${1:-}" = "--parity" ]; then
  say "=== --parity: probing ${PORT_NPU_EMBED} ==="
  if curl -sf -m 3 "${PORT_NPU_EMBED%/v1}/health" >/dev/null 2>&1; then
    exec "$(dirname "$0")/../.venv/bin/python" "$(dirname "$0")/npu_parity.py" \
      --skip-agent --embed-endpoint "cpu=http://127.0.0.1:8390/v1" \
      --embed-endpoint "npu=${PORT_NPU_EMBED}"
  fi
  bad "no NPU embedding endpoint answering at ${PORT_NPU_EMBED}; arms not run"
  exit 3
fi
