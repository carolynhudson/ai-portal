#!/usr/bin/env bash
# Path A (docs/PLAN.md §4a): swap the self-built XRT 2.27 pairing for AMD's
# VALIDATED 1.8 stack — xrt-base 2.25.37 + xrt_plugin-amdxdna 2.25.260102
# (public opendownload zip, no EULA: RAI_1.8_Linux_NPU_XRT.zip).
#
#   sudo bash tools/npu_path_a_install.sh      # purge 2.27, install 2.25.37
#   sudo reboot                                # DKMS rebuild + MOK sign
#   tools/npu_validate.sh                      # after boot: layers
#   ... quicktest + probe300 (the actual test) — see PLAN §4a ladder
#
# The plugin deb pins xrt-base (>=2.25, <<2.26): the self-built 2.27
# (xrt-base/xrt-npu/xrt_plugin-amdxdna) MUST be purged first; its DKMS module
# (xrt-amdxdna/2.27.0) is removed by the purge, and the new module is built
# and MOK-signed at install with the key this box already enrolled.
set -euo pipefail

# $HOME under sudo is /root — resolve the invoking user's real home.
USERHOME="$(getent passwd "${SUDO_USER:-$(id -un)}" | cut -d: -f6)"
D="$USERHOME/ai/src/npu-path-a/RAI_1.8_Linux_NPU_XRT"
LOG="$USERHOME/ai/logs/npu-path-a-$(date +%Y%m%d-%H%M%S).log"

[ "$(id -u)" = 0 ] || { echo "run with sudo: sudo bash $0"; exit 1; }

need() { [ -f "$1" ] || { echo "missing deb: $1 (download+unzip RAI_1.8_Linux_NPU_XRT.zip first)"; exit 1; }; }
for f in xrt_202620.2.25.37_24.04-amd64-base.deb \
         xrt_202620.2.25.37_24.04-amd64-base-dev.deb \
         xrt_202620.2.25.37_24.04-amd64-npu.deb \
         xrt_plugin.2.25.260102.56.release_24.04-amd64-amdxdna.deb; do
  need "$D/$f"
done

echo "== state before ==" | tee "$LOG"
dpkg -l | grep -iE "xrt|xdna" | tee "$LOG" || true
dkms status 2>/dev/null | grep -i xdna | tee "$LOG" || true

echo "== purge self-built 2.27 (xrt-base, xrt-npu, xrt_plugin-amdxdna) ==" | tee "$LOG"
apt-get purge -y xrt-base xrt-npu xrt_plugin-amdxdna 2>&1 | tail -4 | tee "$LOG" || true
dkms status 2>/dev/null | grep -i xdna | tee "$LOG" || echo "no xdna dkms module (expected)" | tee "$LOG"

echo "== install AMD's validated 1.8 stack ==" | tee "$LOG"
for f in xrt_202620.2.25.37_24.04-amd64-base.deb \
         xrt_202620.2.25.37_24.04-amd64-base-dev.deb \
         xrt_202620.2.25.37_24.04-amd64-npu.deb \
         xrt_plugin.2.25.260102.56.release_24.04-amd64-amdxdna.deb; do
  apt-get install --fix-broken -y "$D/$f" 2>&1 | tail -3 | tee "$LOG"
done

echo "== state after ==" | tee -a "$LOG"
dpkg -l | grep -iE "xrt|xdna" | tee -a "$LOG"
dkms status 2>/dev/null | grep -i xdna | tee -a "$LOG" || true

cat <<EOF

Install log: $LOG
If dkms status shows the new xrt-amdxdna built for the current kernel:

    sudo reboot

After boot (as the normal user):

    tools/npu_validate.sh
    source ~/ai/venvs/ryzenai/bin/activate && source /opt/xilinx/xrt/setup.sh
    cd ~/ai/venvs/ryzenai/quicktest && python quicktest.py    # expect: Test Finished

Then the 300M probe and the parity ladder (docs/PLAN.md §4a).
EOF
