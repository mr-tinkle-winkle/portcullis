"""
Connections the daemon has seen, per app.

New flows are reported by the NFQUEUE listeners (flowqueue.py); this module parses the packet
headers, keeps a table of what each app has talked to, and uses the kernel's conntrack list to
tell which of those are still open.  Pure Python, no netfilter calls.
"""
from __future__ import annotations

import ipaddress
import re
import struct
import threading
import time
from dataclasses import dataclass

PROTO_NAMES = {1: "icmp", 6: "tcp", 17: "udp", 58: "icmp", 132: "sctp", 136: "udplite"}
MAX_TUPLES = 6000
FORGET_AFTER = 3 * 3600     # drop closed flows nobody has seen for this long


@dataclass(frozen=True)
class Packet4:
    proto: str
    src: str
    dst: str
    sport: int
    dport: int


def parse_packet(data: bytes) -> "Packet4 | None":
    """IPv4 / IPv6 header (+ TCP/UDP ports) -> Packet4, or None if it isn't parseable."""
    if not data:
        return None
    ver = data[0] >> 4
    try:
        if ver == 4 and len(data) >= 20:
            ihl = (data[0] & 15) * 4
            nxt = data[9]
            src, dst = str(ipaddress.IPv4Address(data[12:16])), str(ipaddress.IPv4Address(data[16:20]))
            off = ihl
        elif ver == 6 and len(data) >= 40:
            nxt = data[6]
            src, dst = str(ipaddress.IPv6Address(data[8:24])), str(ipaddress.IPv6Address(data[24:40]))
            off = 40
            while nxt in (0, 43, 60) and len(data) >= off + 8:        # hop-by-hop, routing, destination options
                nxt, size = data[off], (data[off + 1] + 1) * 8
                off += size
        else:
            return None
    except (ValueError, IndexError):
        return None
    proto = PROTO_NAMES.get(nxt, str(nxt))
    sport = dport = 0
    if proto in ("tcp", "udp", "sctp", "udplite") and len(data) >= off + 4:
        sport, dport = struct.unpack("!HH", data[off:off + 4])
    return Packet4(proto, src, dst, sport, dport)


def remote_of(pkt: Packet4, direction: str) -> "tuple[str, int, int]":
    """(remote ip, remote port, local port) of a packet seen in the output ('out') or input ('in') hook."""
    if direction == "out":
        return pkt.dst, pkt.dport, pkt.sport
    return pkt.src, pkt.sport, pkt.dport


_CT = re.compile(r"(src|dst|sport|dport)=(\S+)")


def parse_conntrack(text: str) -> "set[tuple[str, str, int, int]]":
    """Open flows from /proc/net/nf_conntrack as {(proto, remote ip, remote port, local port)} in BOTH
    orientations (we don't know which end is local, the table lookup tries both)."""
    out = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        proto = parts[2]
        if proto == "tcp" and any(f" {st} " in line for st in ("TIME_WAIT", "CLOSE", "CLOSE_WAIT", "LAST_ACK")):
            continue
        got = _CT.findall(line)
        if len(got) < 4:
            continue
        d = {}
        for k, v in got[:4]:
            d.setdefault(k, v)
        try:
            s, dd, sp, dp = d["src"], d["dst"], int(d.get("sport", 0)), int(d.get("dport", 0))
        except (KeyError, ValueError):
            continue
        out.add((proto, dd, dp, sp))
        out.add((proto, s, sp, dp))
    return out


class FlowTable:
    """Per-app record of remote endpoints, fed by ``record`` and refreshed by ``refresh_open``."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self.lock = threading.Lock()
        # identity -> {(direction, proto, rip, rport, lport): [first, last, count, unit]}
        self._apps: dict = {}
        self._open: "set | None" = None

    def record(self, identity: str, unit: str, direction: str, proto: str, rip: str, rport: int, lport: int) -> None:
        now = self._clock()
        with self.lock:
            app = self._apps.setdefault(identity, {})
            key = (direction, proto, rip, rport, lport)
            e = app.get(key)
            if e is None:
                if sum(len(a) for a in self._apps.values()) >= MAX_TUPLES:
                    self._prune(now, force=True)
                app[key] = [now, now, 1, unit]
            else:
                e[1], e[2], e[3] = now, e[2] + 1, unit

    def refresh_open(self, open_set: "set | None") -> None:
        """``open_set`` from parse_conntrack, or None when conntrack can't be read."""
        with self.lock:
            self._open = open_set
            if open_set is not None:
                now = self._clock()
                for app in self._apps.values():
                    for (d, proto, rip, rport, lport), e in app.items():
                        if (proto, rip, rport, lport) in open_set:
                            e[1] = now
            self._prune(self._clock())

    def _prune(self, now: float, force: bool = False) -> None:
        cutoff = now - (60 if force else FORGET_AFTER)
        for ident in list(self._apps):
            app = self._apps[ident]
            for k in [k for k, e in app.items() if e[1] < cutoff and not self._is_open(k)]:
                del app[k]
            if not app:
                del self._apps[ident]

    def _is_open(self, key) -> bool:
        if self._open is None:
            return False
        d, proto, rip, rport, lport = key
        return (proto, rip, rport, lport) in self._open

    def remotes(self, identity: str, recent: float = 30.0) -> "list[dict]":
        """The app's remotes, one per (ip, direction), newest first."""
        now = self._clock()
        with self.lock:
            app = dict(self._apps.get(identity, {}))
            opened = self._open
        grouped: dict = {}
        for (d, proto, rip, rport, lport), (first, last, count, unit) in app.items():
            g = grouped.setdefault((rip, d), {"ip": rip, "direction": d, "first": first, "last": last, "count": 0,
                                              "active": False, "ports": {}, "units": set()})
            is_open = ((proto, rip, rport, lport) in opened) if opened is not None else (now - last < recent)
            g["first"], g["last"] = min(g["first"], first), max(g["last"], last)
            g["count"] += count
            g["active"] = g["active"] or is_open
            g["units"].add(unit)
            p = g["ports"].setdefault((proto, rport), {"proto": proto, "port": rport, "count": 0, "active": False, "last": 0})
            p["count"] += count
            p["active"] = p["active"] or is_open
            p["last"] = max(p["last"], last)
        out = []
        for g in grouped.values():
            g["ports"] = sorted(g["ports"].values(), key=lambda p: (-p["last"], p["port"]))
            g["units"] = sorted(g["units"])
            out.append(g)
        out.sort(key=lambda g: (not g["active"], -g["last"]))
        return out

    def identities(self) -> "list[str]":
        with self.lock:
            return list(self._apps)
