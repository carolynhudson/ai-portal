#!/usr/bin/env python3
"""Tiny RA sniffer -- reads Router Advertisements off the wire, root,
no dependencies (the production Pis have no tcpdump and apt there is
owner-method). Pure observation: sends nothing, changes nothing.

  sudo python3 ra_sniff.py eth0 120      # via ssh: cat | sudo python3 -

Prints one line per RA: source, router lifetime, and every RDNSS option
(server + lifetime). RFC 6106: an RDNSS lifetime of 0 tells clients to
REMOVE that DNS server -- what radvd actually TRANSMITS is only visible
here, in the bytes, never in the config file (the forge's pktmon saw
lifetime 0; the config says 600; only the wire settles it).
"""
import socket
import sys
import time

ETH_P_IPV6 = 0x86DD
ICMPV6_ROUTER_ADVERTISEMENT = 134
ND_OPT_RDNSS = 25


def main() -> int:
    iface = sys.argv[1] if len(sys.argv) > 1 else "eth0"
    secs = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    only_rd = len(sys.argv) > 3 and sys.argv[3].lower() == "rdns"
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                      socket.htons(ETH_P_IPV6))
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    s.bind((iface, ETH_P_IPV6))
    s.settimeout(5.0)
    end = time.time() + secs
    seen = 0
    while time.time() < end:
        try:
            pkt, _ = s.recvfrom(65535)
        except socket.timeout:
            continue
        if len(pkt) < 55 or pkt[12:14] != b"\x86\xdd":
            continue
        if pkt[20] != 58:  # not ICMPv6
            continue
        icmp = pkt[54:]
        if not icmp or icmp[0] != ICMPV6_ROUTER_ADVERTISEMENT:
            continue
        src = socket.inet_ntop(socket.AF_INET6, pkt[22:38])  # IPv6 src at eth14+8
        rlife = int.from_bytes(icmp[6:8], "big")             # RA lifetime at 6:8
        opts, i = [], 16   # RA options begin at 16 (after retrans-timer)
        while i + 8 <= len(icmp):
            otype, olen = icmp[i], icmp[i + 1]
            if olen < 1:
                break
            if otype == ND_OPT_RDNSS and olen >= 3:
                lt = int.from_bytes(icmp[i + 4:i + 8], "big")
                for k in range(8, olen * 8, 16):            # addrs start at +8
                    if k + 16 <= olen * 8:
                        ip = socket.inet_ntop(socket.AF_INET6,
                                              icmp[i + k:i + k + 16])
                        opts.append((ip, lt))
            i += olen * 8
        line = f"RA from {src}: router-lifetime {rlife}"
        if opts:
            parts = [f"{ip} ttl {lt}"
                     + ("  <-- lifetime 0: clients REMOVE this server (RFC 6106)"
                        if lt == 0 else "") for ip, lt in opts]
            line += " | RDNSS " + "; ".join(parts)
        if only_rd and not opts:
            continue
        print(line, flush=True)
        seen += 1
    if not seen:
        print("no RAs captured (iface up? root? IPv6 on?)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
