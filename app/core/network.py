"""Host networking: where the services can be reached, and what blocks them.

Three jobs, all read-only:

1. **Addresses** — :func:`interfaces` parses ``ip -4 -o addr`` + the default
   route so the UI can show the *real* LAN address (``hostname -I`` dumps
   everything, docker bridges included, and lies after a reboot).
2. **Bind semantics** — a service's ``host`` value maps to a *mode*:
   ``loopback`` (this machine only), ``lan`` (one interface address) or
   ``all`` (every interface). :func:`resolve_bind` turns a mode (+ optional
   address) back into a host string. ``::`` / ``0.0.0.0`` are equivalent for
   our purposes and always reported as ``all``.
3. **Firewall** — :func:`firewall_status` reports ufw state *without root*
   (``ufw status`` is root-only, so the enabled flag comes from
   ``/etc/ufw/ufw.conf``, world-readable), and :func:`allow_command` returns
   the exact command that opens one port to the local network.

:func:`check_reachable` is a TCP probe used by the Settings page after an
apply: connecting to the LAN address proves the daemon is not loopback-bound
(a loopback-only bind refuses the same-host connection to 192.168.x.x).

No third-party deps; every probe degrades to ``unknown``/``[]`` rather than
raising, because the Settings page must still render on a machine where
``ip``/``ufw`` are missing or refused.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import subprocess

LOOPBACK = "127.0.0.1"
ALL = "0.0.0.0"
LOOPBACK_V6 = "::1"
ALL_V6 = "::"

MODES = ("loopback", "lan", "all")

# Interfaces that are never a sane LAN bind target even when they are UP.
VIRTUAL_PREFIXES = ("lo", "docker", "br-", "virbr", "veth", "vet", "tap",
                     "kube", "cni", "flannel", "wg", "tailscale", "zt", "dummy")

_ip_cache: tuple[float, list[dict]] = (0.0, [])


def _run(args: list[str], timeout: float = 5.0) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except Exception:  # noqa: BLE001
        return ""


def _link_states() -> dict[str, dict]:
    """name -> {"flags_up": bool, "operstate": str} from ``ip -o link show``.

    ``ip addr`` alone does not report carrier state, and a bridge with no
    carrier (docker0) still has the IFF_UP flag — both are needed to tell a
    live NIC from a parked one.
    """
    out = _run(["ip", "-o", "link", "show"])
    states: dict[str, dict] = {}
    for line in out.splitlines():
        # "2: enp191s0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu ... state UP ..."
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        try:
            int(parts[0])
        except ValueError:
            continue
        name = parts[1].split("@", 1)[0].strip()
        rest = parts[2]
        flags = ""
        m = re.search(r"<([^>]*)>", rest)
        if m:
            flags = m.group(1)
        oper = None
        ms = re.search(r"\bstate\s+(\S+)", rest)
        if ms:
            oper = ms.group(1)
        states[name] = {
            "flags_up": "UP" in flags.split(","),
            "operstate": oper or "UNKNOWN",
        }
    return states


def interfaces() -> list[dict]:
    """IPv4 interfaces: ``[{name, address, prefix, network, scope, up, default,
    virtual, lan}]``, default-route interface first.

    ``lan`` means "a sane bind target for other machines on the network":
    scope global, not a virtual/bridge device.
    """
    global _ip_cache
    import time as _t
    now = _t.time()
    if now - _ip_cache[0] < 10.0 and _ip_cache[1]:
        return _ip_cache[1]

    out = _run(["ip", "-4", "-o", "addr", "show"])
    links = _link_states()
    items: list[dict] = []
    for line in out.splitlines():
        parts = line.split()
        if "inet" not in parts:
            continue
        name = parts[1].split("@", 1)[0]
        i = parts.index("inet")
        cidr = parts[i + 1] if i + 1 < len(parts) else ""
        scope = "global"
        if "scope" in parts:
            j = parts.index("scope")
            if j + 1 < len(parts):
                scope = parts[j + 1]
        link = links.get(name, {})
        oper = link.get("operstate", "UNKNOWN")
        # UNKNOWN is how a working link is reported when the driver does not
        # implement carrier detection; only DOWN/DORMANT means "no traffic".
        up = bool(link.get("flags_up")) and oper not in ("DOWN", "DORMANT", "NO-CARRIER")
        try:
            net = ipaddress.ip_network(cidr, strict=False)
            addr = str(ipaddress.ip_interface(cidr).ip)
            network = str(net)
            prefix = net.prefixlen
        except ValueError:
            addr, network, prefix = cidr.split("/")[0], None, None
        virtual = name.startswith(VIRTUAL_PREFIXES)
        items.append({
            "name": name, "address": addr, "prefix": prefix, "network": network,
            "scope": scope, "up": up, "operstate": oper,
            # "dynamic" = the address came from DHCP: it can change under a
            # service that bound the old one explicitly (bind then fails at
            # start), which is why "all interfaces" is the resilient default.
            "dynamic": "dynamic" in parts,
            "virtual": virtual,
            "lan": bool(up and scope == "global" and not virtual),
            "default": False,
        })

    # The default-route interface is the one other machines actually reach.
    for line in _run(["ip", "route", "show", "default"]).splitlines():
        m = re.search(r"\bdev\s+(\S+)", line)
        if m:
            for it in items:
                if it["name"] == m.group(1):
                    it["default"] = True
    items.sort(key=lambda it: (not it["default"], not it["lan"], it["name"]))
    _ip_cache = (now, items)
    return items


def lan_addresses() -> list[str]:
    return [it["address"] for it in interfaces() if it["lan"]]


def primary_lan() -> dict | None:
    """The interface to advertise: default-route LAN nic, else any LAN nic."""
    for it in interfaces():
        if it["lan"] and it["default"]:
            return it
    for it in interfaces():
        if it["lan"]:
            return it
    return None


def lan_network() -> str | None:
    """e.g. "192.168.1.0/24" — the source range used in firewall hints."""
    it = primary_lan()
    return it.get("network") if it else None


def is_local_address(addr: str) -> bool:
    """True if ``addr`` is this host's (loopback or one of our interface IPs)."""
    if addr in (LOOPBACK, "localhost", ALL, ALL_V6, LOOPBACK_V6):
        return True
    return any(it["address"] == addr for it in interfaces())


# --------------------------------------------------------------- bind modes

def bind_mode(host: str | None) -> str:
    """Classify a bind address: ``loopback`` | ``lan`` | ``all``."""
    h = (host or "").strip()
    if h in (ALL, ALL_V6, ""):
        return "all"
    if h in (LOOPBACK, LOOPBACK_V6, "localhost", "::1"):
        return "loopback"
    return "lan"


def resolve_bind(mode: str, address: str | None = None) -> str:
    """Host string for a bind mode. ``lan`` without an address uses the
    default-route interface.

    ``lan`` with no interface available is an error rather than a quiet fall
    back to ``0.0.0.0``: the caller asked to expose one network, and opening
    every interface is strictly more than that.
    """
    mode = (mode or "").strip().lower()
    if mode == "loopback":
        return LOOPBACK
    if mode == "all":
        return ALL
    if mode == "lan":
        host = (address or "").strip() or (primary_lan() or {}).get("address")
        if not host:
            raise ValueError(
                "no usable LAN interface found (need an up, scope-global, "
                "non-virtual interface with an IPv4 address) — use 'all' "
                "explicitly if that is what you want")
        return host
    raise ValueError(f"unknown bind mode {mode!r} (expected one of {', '.join(MODES)})")


def describe_bind(host: str | None) -> str:
    mode = bind_mode(host)
    if mode == "loopback":
        return "loopback only — this machine"
    if mode == "all":
        return "all interfaces — reachable from the local network"
    return f"bound to interface address {host}"


def endpoint_urls(host: str | None, port: int, scheme: str = "http") -> list[dict]:
    """Every URL a human would use for this bind, with a note on which ones
    can actually work (a loopback bind is unreachable from the LAN)."""
    mode = bind_mode(host)
    urls: list[dict] = []
    if mode == "loopback":
        urls.append({"url": f"{scheme}://{LOOPBACK}:{port}", "label": "this machine",
                     "reachable_from": "local"})
        return urls
    urls.append({"url": f"{scheme}://{LOOPBACK}:{port}", "label": "this machine",
                 "reachable_from": "local"})
    it = primary_lan()
    if it:
        urls.append({"url": f"{scheme}://{it['address']}:{port}",
                     "label": f"LAN · {it['name']}", "reachable_from": "lan"})
    for a in lan_addresses():
        if it and a == it["address"]:
            continue
        urls.append({"url": f"{scheme}://{a}:{port}", "label": "LAN",
                     "reachable_from": "lan"})
    return urls


# ------------------------------------------------------------- reachability

def check_reachable(host: str, port: int, timeout: float = 1.5) -> dict:
    """TCP connect probe. Used against the *LAN address* to prove a daemon is
    not loopback-bound: the kernel routes that connection through the real
    interface, so a 127.0.0.1-only bind refuses it."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return {"host": host, "port": int(port), "ok": True}
    except Exception as e:  # noqa: BLE001
        return {"host": host, "port": int(port), "ok": False, "error": str(e)[:200]}


WILDCARDS = ("*", "0.0.0.0", "::", "[::]")


def bound_addresses(port: int) -> set[str] | None:
    """Addresses ``port`` is bound on (as ``ss`` prints them). None when ``ss``
    is unavailable, so callers can tell "not bound" from "unknown"."""
    try:
        p = subprocess.run(["ss", "-tlnH"], capture_output=True, text=True,
                           timeout=5)
        if p.returncode != 0:
            return None
    except Exception:  # noqa: BLE001
        return None
    found: set[str] = set()
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        addr_part, _, p_str = parts[3].rpartition(":")
        if p_str == str(port):
            found.add(addr_part)
    return found


def listening_on(host: str, port: int) -> bool | None:
    """Would a connection to ``host:port`` find a listener?

    A wildcard bind (``0.0.0.0`` / ``*`` / ``::``) satisfies any target — that
    is the point of binding to all interfaces, and the case the Settings page
    most needs to report correctly after opening a service to the LAN.
    """
    bound = bound_addresses(port)
    if bound is None:
        return None
    if bound & set(WILDCARDS):
        return True
    h = (host or "").strip().strip("[]")
    if h in ("localhost", LOOPBACK):
        return bool(bound & {LOOPBACK, LOOPBACK_V6, "[::1]"})
    return h in bound or f"[{h}]" in bound


# ----------------------------------------------------------------- firewall

def firewall_status() -> dict:
    """Best-effort without root. ``enabled`` is None when undetectable.

    ``ufw status`` requires root, but ``/etc/ufw/ufw.conf`` is world-readable
    and is the source of truth for whether ufw runs at boot. When ufw is off
    (this box today) nothing filters incoming ports; when it is on, the port
    must be allowed explicitly — :func:`allow_command` gives the command.
    """
    res: dict = {"enabled": None, "backend": None, "detail": "unknown",
                 "checked": ["ufw"]}
    try:
        with open("/etc/ufw/ufw.conf", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"^\s*ENABLED=(\S+)", line)
                if m:
                    res["enabled"] = m.group(1).strip().lower() == "yes"
                    res["backend"] = "ufw"
                    res["detail"] = ("ufw enabled at boot" if res["enabled"]
                                     else "ufw disabled — inbound not filtered")
    except OSError:
        res["detail"] = "no ufw config found"

    live = _run(["ufw", "status"])
    if "Status: active" in live:
        res["enabled"] = True
        res["detail"] = "ufw active"
    elif "Status: inactive" in live:
        res["enabled"] = False
        res["detail"] = "ufw inactive"
    elif "root" in live.lower() or "permission" in live.lower():
        res["root_only"] = True
    return res


def allow_command(port: int, network: str | None = None,
                  comment: str | None = None) -> str:
    """The command that opens ``port`` to the local network (or to one
    source subnet). Printed for the operator — the portal never runs it
    (no root, and firewall changes are not the portal's to make)."""
    net = network or lan_network() or "<local-subnet>"
    tail = f" comment '{comment}'" if comment else ""
    return (f"sudo ufw allow from {net} to any port {port} proto tcp"
            + tail)


def trusted_proxies() -> list[str]:
    """Reverse-proxy / localhost sources whose X-Forwarded-For we may trust.
    Kept minimal: this deployment expects direct connections."""
    return [LOOPBACK]
