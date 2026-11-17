#!/usr/bin/env bash
# llama_version.sh EXE - record the ACTIVE llama.cpp build at
# $MESHD_LLAMA_VERSION_FILE (default ~/forge/llama/version.json).
# Mirrors the forge's llama_version.ps1 law: NEVER read the version off
# the BUILD repo - after a staged build the repo sits at the incoming
# ref, not at what runs (that probe lies). Version = release-directory
# match (llama.cpp-<build>); arch = rocminfo, else lspci table, else
# nvidia-smi; the 'source' field tells the reader how this was known.
set -eu
EXE="${1:?usage: llama_version.sh /path/to/llama-server}"
OUT="${MESHD_LLAMA_VERSION_FILE:-$HOME/forge/llama/version.json}"
EXE="$(readlink -f "$EXE")"
dir=$(basename "$(dirname "$EXE")")
case "$dir" in
  llama.cpp-*) version="${dir#llama.cpp-}" ;;
  *) version="" ;;
esac
sha=""; source="exe-dir"
[ -n "$version" ] && source="release-dir-match"
if command -v rocminfo >/dev/null 2>&1; then
  arch=$(rocminfo 2>/dev/null | grep -m1 -o "gfx[0-9a-f]*" || true)
  [ -n "$arch" ] && source="$source+rocminfo"
fi
if [ -z "${arch:-}" ] && lspci 2>/dev/null | grep -qi "Strix Halo"; then
  arch="gfx1151"; source="$source+lspci-strix"
fi
if [ -z "${arch:-}" ] && command -v nvidia-smi >/dev/null 2>&1; then
  arch="sm_$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d '.')"
  source="$source+nvidia-smi"
fi
[ -z "${arch:-}" ] && arch="unknown"
mt=$(date -u -Iseconds -d "@$(stat -c %Y "$EXE")")
mkdir -p "$(dirname "$OUT")"
python3 - "$OUT" "$version" "$sha" "$arch" "$source" "$mt" <<'PY'
import json, sys, datetime, socket
out, ver, sha, arch, source, mt = sys.argv[1:7]
json.dump({"node": socket.gethostname().upper(), "version": ver or None,
           "printed_version": ver or None, "sha": sha or None,
           "cuda_arch": arch, "source": source,
           "exe_mtime": mt, "built_utc": mt,
           "measured_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")},
          open(out, "w"), indent=1)
PY
echo "recorded: $OUT (version=${version:-null} arch=$arch source=$source)"
