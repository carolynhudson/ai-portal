#!/usr/bin/env bash
# LAN RA scan -- owner-run (sudo), read-only. Bench #27 found THREE IPv6
# RA advertisers fighting for DNS placement on the LAN: the Comcast
# gateway (own RDNSS), pihole2 (ours, DNS-only), and a Windows-ICS-shaped
# box (fe80::d6a3:4602:f25d:e171, RDNSS=itself, lifetime 0). RAs are
# ff02::1 multicast, so any NIC on the LAN sees them all.
#
#   sudo scripts/lan_ra_scan.sh [seconds] [iface]
#
# Prints a per-sender summary (RA count, RDNSS servers, RDNSS lifetime)
# and keeps the raw capture at /tmp/ra_scan_raw.txt for deeper analysis
# (paste to the bench; the forge's pktmon laws in memory 205 cover the
# Windows side). This script sniffs: it changes nothing.
set -eu
SEC="${1:-60}"
IF="${2:-any}"
RAW=/tmp/ra_scan_raw.txt
command -v tcpdump >/dev/null || { echo "tcpdump missing (apt install tcpdump)"; exit 1; }
echo "RA scan: ${SEC}s on -i ${IF} (raw -> ${RAW})"
# RA type byte sits at IPv6-payload offset 40 (IPv6 hdr 40B, ICMP type
# first). 'ip6[53]' is the IPv4 fragment-offset trick -- on IPv6 it reads
# junk, which is why an earlier sniff "saw no RAs" while they flooded.
timeout "$((SEC + 5))" tcpdump -i "$IF" -l -nn -vv 'ip6[40] == 134' 2>/dev/null | tee "$RAW" | python3 - <<'PY'
import re, sys, collections
senders = collections.Counter()
rdnss = collections.defaultdict(set)
lifetimes = collections.defaultdict(set)
txt = sys.stdin.read()
# tcpdump -vv RA stanzas: "IP6 <src> > ..." header, then body lines. The
# RDNSS option prints as a header line ("RDNSS ...") followed by server
# addresses and "lifetime: N" on following lines -- walk lines.
for block in re.split(r"\n(?=\S+\s+IP6\s+)", txt):
    m = re.search(r"IP6\s+([0-9a-fA-F:.]+)\s+>\s+", block)
    if not m or not re.search(r"type ra[ ,]", block):
        continue
    cur = m.group(1)
    senders[cur] += 1
    in_rdnss = False
    for line in block.splitlines():
        if "RDNSS" in line.upper():
            in_rdnss = True
        m2 = re.search(r"lifetime[:\s]+(\d+)", line, re.I)
        if in_rdnss and m2:
            lifetimes[cur].add(m2.group(1))
        if in_rdnss:
            m3 = re.search(r"[0-9a-fA-F:]{2,}:[0-9a-fA-F:.]*[0-9a-fA-F]", line)
            if m3:
                rdnss[cur].add(m3.group(0))
if not senders:
    print("no RAs seen (root needed for raw sockets? iface up? IPv6 enabled?)")
for s, n in senders.most_common():
    tag = "OURS (pi2 RDNSS)" if s == "fe80::a828:a424:5673:846" else \
          "OURS (pi RDNSS)" if s == "fe80::d6a3:4602:f25d:e171" else \
          "gateway" if "9618:65ff" in s else "UNKNOWN -- identify this box"
    print(f"{n:4d} RA  {s:42s} {tag}")
    if rdnss.get(s):
        print(f"        RDNSS: {', '.join(sorted(rdnss[s]))}")
    if lifetimes.get(s):
        lt = sorted(lifetimes[s])
        warn = "  <-- lifetime 0 = clients REMOVE this server (RFC 6106)" if "0" in lt else ""
        print(f"        RDNSS lifetime: {', '.join(lt)}{warn}")
PY
echo "raw kept: ${RAW}"
