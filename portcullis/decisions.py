"""
Who may talk to whom: the rules and the temporary state around them (pure logic, no netfilter).

A *rule* belongs to a profile: ``{"ip": "1.2.3.4", "port": 0, "proto": "", "verdict": "allow"|"block"}``.
``port`` 0 means "any port".  The most specific rule wins (port beats ip); at equal specificity a
block beats an allow.  Blocks are enforced in the kernel (see rules.py); allows matter in ask mode.

Ask mode asks about a connection only when no rule and no temporary allow covers it.
"""
from __future__ import annotations

import ipaddress
import time

PROTOS = ("tcp", "udp")


def normalize_ip(text: str) -> str:
    return str(ipaddress.ip_address(str(text).strip()))


def clean_rule(raw: dict) -> dict:
    """Validate one rule; raises ValueError."""
    ip = normalize_ip(raw["ip"])
    port = int(raw.get("port", 0) or 0)
    proto = str(raw.get("proto", "") or "").lower()
    verdict = raw.get("verdict")
    if verdict not in ("allow", "block"):
        raise ValueError("verdict must be allow or block")
    if not 0 <= port <= 65535:
        raise ValueError("port must be 0-65535")
    if proto not in ("",) + PROTOS:
        raise ValueError("proto must be tcp, udp or empty")
    if port and not proto:
        raise ValueError("a port rule needs a protocol (tcp or udp)")
    if not port:
        proto = ""
    return {"ip": ip, "port": port, "proto": proto, "verdict": verdict}


PORT_DIRECTIONS = ("both", "in", "out")
PORT_PROTOS = ("both", "tcp", "udp")
MAX_PORT_NAME = 32


def clean_port(raw: dict) -> dict:
    """Validate one *named port* of a profile; raises ValueError.

    ``{"name": "voice", "port": 3478, "proto": "udp"|"tcp"|"both", "direction": "both"|"in"|"out",
    "enabled": True}``.  A named port covers that port number on either end of the connection (so it
    works for ports the app connects to and for ports it listens on), for any remote address.
    Enabled = traffic allowed (the default); disabled = dropped in the kernel."""
    name = " ".join(str(raw.get("name", "")).split())[:MAX_PORT_NAME]
    if not name or not name.isprintable():
        raise ValueError("a port needs a name")
    try:
        port = int(raw["port"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("port must be a number") from None
    if isinstance(raw["port"], bool) or not 1 <= port <= 65535:
        raise ValueError("port must be 1-65535")
    proto = str(raw.get("proto", "both") or "both").lower()
    if proto == "any":
        proto = "both"
    if proto not in PORT_PROTOS:
        raise ValueError("proto must be tcp, udp or both")
    direction = str(raw.get("direction", "both") or "both").lower()
    if direction not in PORT_DIRECTIONS:
        raise ValueError("direction must be both, in or out")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    return {"name": name, "port": port, "proto": proto, "direction": direction, "enabled": enabled}


def _specificity(rule: dict) -> int:
    return 2 if rule["port"] else 1


def rule_verdict(rules: "list[dict]", ip: str, port: int, proto: str) -> "str | None":
    """'allow' / 'block' / None for one remote endpoint."""
    best, best_v = 0, None
    for r in rules:
        if r["ip"] != ip:
            continue
        if r["port"] and (r["port"] != port or r["proto"] != proto):
            continue
        s = _specificity(r)
        if s > best or (s == best and r["verdict"] == "block"):
            best, best_v = s, r["verdict"]
    return best_v


def is_unaskable(ip: str) -> bool:
    """Traffic that is never worth a popup: loopback, multicast, broadcast, unspecified."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return a.is_loopback or a.is_multicast or a.is_unspecified or ip == "255.255.255.255"


def is_local(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_loopback or a.is_link_local


def effective_ask(profile, settings) -> bool:
    mode = getattr(profile, "ask", "default") if profile is not None else "default"
    return mode == "ask" or (mode == "default" and settings.ask_default)


class Temporary:
    """In-memory, expiring decisions: temporary allows and the post-'ignore' quiet window."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._allow: dict = {}
        self._quiet: dict = {}

    @staticmethod
    def key(identity: str, ip: str, port: int, proto: str, per_port: bool) -> tuple:
        return (identity, ip, port, proto) if per_port else (identity, ip)

    def allow(self, key: tuple, seconds: float) -> None:
        self._allow[key] = self._clock() + seconds

    def quiet(self, key: tuple, seconds: float) -> None:
        if seconds > 0:
            self._quiet[key] = self._clock() + seconds

    def allowed(self, key: tuple) -> bool:
        return self._live(self._allow, key)

    def is_quiet(self, key: tuple) -> bool:
        return self._live(self._quiet, key)

    def _live(self, table: dict, key: tuple) -> bool:
        until = table.get(key)
        if until is None:
            return False
        if until <= self._clock():
            del table[key]
            return False
        return True

    def allowed_list(self) -> "list[tuple]":
        now = self._clock()
        return [(k, round(v - now)) for k, v in self._allow.items() if v > now]

    def forget(self, identity: str) -> None:
        for table in (self._allow, self._quiet):
            for k in [k for k in table if k[0] == identity]:
                del table[k]
