#!/usr/bin/env bash
# Build the XRT userspace SHIM for the NPU from AMD's public source — the path that
# needs no AMD account and no .deb, and that mlir-aie documents for boxes with an
# in-tree amdxdna (this box: kernel 7.0, driver 0.7.0, node /dev/accel/accel0 live).
#
#   tools/npu_xrt_build.sh deps     # print the apt line for the missing build deps (root, owner)
#   tools/npu_xrt_build.sh build    # clone + build XRT(-npu -opt) + plugin(-release -nokmod)
#   tools/npu_xrt_build.sh install  # print the sudo install lines once the build has landed
#
# -nokmod is LAW here: the box's in-tree amdxdna works and owns the firmware pairing
# (npu_7.sbin protocol 7 on kernel 7.0). A DKMS module over it is the documented
# driver/firmware mismatch that produces post-load aborts. We build userspace ONLY.
#
# This box's policy (owner): AMD components arrive via AMD's own channels. Source
# from amd/xdna-driver is AMD's repository; plain Ubuntu build deps are not AMD
# components. Nothing installed here touches /opt/rocm — XRT lives in /opt/xilinx/xrt.
set -euo pipefail
SRC="${NPU_SRC_DIR:-$HOME/ai/src/xdna-driver}"
JOBS="${NPU_BUILD_JOBS:-6}"        # a polite -j: the box is a running agent host
RAM_FLOOR_KB=$((20 * 1024 * 1024))

deps_list() {
  local missing=() p
  # Ubuntu 26.04 (Boost 1.90): libboost-system-dev is a virtual redirect into
  # libboost-dev and libboost-property-tree-dev no longer exists (both are
  # header-only now — apt refused the old list, 2026-10-16). The four packages
  # below are the real ones on this release.
  for p in libboost-dev libboost-filesystem-dev libboost-program-options-dev \
           libboost-iostreams-dev \
           ocl-icd-opencl-dev opencl-headers \
           systemtap-sdt-dev libelf-dev libudev-dev libsystemd-dev; do
    dpkg -l "$p" 2>/dev/null | grep -q '^ii' || missing+=("$p")
  done
  printf '%s' "${missing[*]+"${missing[*]}"}"
}

cmd="${1:-build}"
case "$cmd" in
  deps)
    m=$(deps_list)
    if [ -z "$m" ]; then echo "all build deps present"; exit 0; fi
    echo "# plain Ubuntu packages (not AMD-managed) — the owner runs this:"
    echo "sudo apt install -y $m"
    ;;
  build)
    avail_kb=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
    [ "$avail_kb" -ge "$RAM_FLOOR_KB" ] || { echo "refused: < 20 GiB free (the box is busy; the router is not a build artifact)"; exit 3; }
    if [ ! -d "$SRC/.git" ]; then
      mkdir -p "$(dirname "$SRC")"
      git clone --recurse-submodules --shallow-submodules --depth 1 \
        https://github.com/amd/xdna-driver.git "$SRC"
    fi
    # CMake caches NEGATIVE include/library checks: install the missing dep and
    # re-run, and the stale miss kills the configure in ~0.1s looking like the
    # install did nothing (Fedora-44 Strix Halo guide's hard-won tip). Wipe the
    # cache between phases, and phase order is LAW: base before plugin (pinning).
    rm -rf "$SRC/xrt/build/Release" "$SRC/build/Release"
    # XRT base first — the plugin pins xrt-base 2.26.x (measured on Fedora 44:
    # order matters; the plugin hard-fails against the wrong base).
    ( cd "$SRC/xrt/build" && nice -n 19 ./build.sh -npu -opt -j "$JOBS" )
    ( cd "$SRC/build" && nice -n 19 ./build.sh -release -nokmod -j "$JOBS" )
    echo
    echo "artifacts:"
    ls -1 "$SRC/xrt/build/Release/"xrt_*.deb "$SRC/build/Release/"xrt_plugin*.tar.gz 2>/dev/null
    "$0" install
    ;;
  install)
    base=$(ls -1 "$SRC/xrt/build/Release/"xrt_*-base.deb 2>/dev/null | head -1)
    plugin=$(ls -1 "$SRC/build/Release/"xrt_plugin*amdxdna*.tar.gz 2>/dev/null | head -1)
    [ -n "$base" ] && [ -n "$plugin" ] || { echo "build artifacts not found — run '$0 build' first"; exit 2; }
    echo "# the owner runs these (root):"
    echo "sudo apt install -y '$base'   # xrt base (+ base-dev, npu debs in the same dir as you need them)"
    echo "sudo tar xzf '$plugin' -C /  # SHIM into /opt/xilinx/xrt (the tarball carries the opt/ prefix)"
    echo "printf '@render - memlock unlimited\\n' | sudo tee /etc/security/limits.d/99-xrt.conf"
    echo "# then RE-L (new login session for the memlock), and prove it:"
    echo "source /opt/xilinx/xrt/setup.sh && xrt-smi examine   # -> [0000:c4:00.1] NPU row"
    ;;
  *) echo "usage: $0 deps|build|install"; exit 1 ;;
esac
