"""
From the daemon's ``overview`` reply to what the window draws (no Qt in here).

  * ``build_apps``  -- one AppView per app, its remotes located on the map (or marked local / unknown)
  * ``build_pins``  -- remotes that share a place become one pin on the map
  * ``connection_state`` -- the three states of a connection row: allowed / blocked / waiting
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from .. import appid, geo


@dataclass
class RemoteView:
    ip: str
    direction: str                    # "out": the app connected there; "in": it connected to the app
    place: "geo.Place | None"
    local: bool                       # LAN / loopback: drawn as "Local Network", not on the map
    active: bool
    blocked: bool
    rule: "str | None"                # the explicit rule on the address: "allow" | "block" | None
    temp_allowed: bool
    count: int
    last: float
    ports: list = field(default_factory=list)       # [{"proto", "port", "count", "active", "rule"}]
    hostname: str = ""

    @property
    def title(self) -> str:
        return self.hostname or self.ip

    @property
    def where(self) -> str:
        if self.local:
            return "Local network"
        return self.place.label if self.place else "Location unknown"


@dataclass
class AppView:
    identity: str
    name: str
    running: bool
    profile: "str | None"
    ask: bool
    settings: "dict | None"
    remotes: "list[RemoteView]"
    counters: "dict | None" = None
    ports: list = field(default_factory=list)       # named ports: [{"name","port","proto","direction","enabled"}]

    @property
    def allowed(self) -> bool:
        """The app-level 'Allow' switch: on unless the profile blocks both directions."""
        s = self.settings
        return not (s and s.get("enabled") and s.get("block_in") and s.get("block_out"))

    @property
    def active_count(self) -> int:
        return sum(1 for r in self.remotes if r.active)

    @property
    def blocked_count(self) -> int:
        return sum(1 for r in self.remotes if r.blocked)


def is_local_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_loopback or a.is_link_local or a.is_multicast


def build_apps(overview: dict, locate, hostnames: "dict | None" = None) -> "list[AppView]":
    """``locate(ip) -> Place | None``; ``hostnames`` maps ip -> name (already resolved)."""
    hostnames = hostnames or {}
    out = []
    for a in overview.get("apps", []):
        remotes = []
        for r in a.get("remotes", []):
            local = is_local_ip(r["ip"])
            remotes.append(RemoteView(
                ip=r["ip"], direction=r.get("direction", "out"), place=None if local else locate(r["ip"]), local=local,
                active=bool(r.get("active")), blocked=bool(r.get("blocked")), rule=r.get("rule"),
                temp_allowed=bool(r.get("temp_allowed")), count=int(r.get("count", 0)), last=float(r.get("last", 0)),
                ports=list(r.get("ports", [])), hostname=hostnames.get(r["ip"], "")))
        out.append(AppView(a["identity"], appid.pretty_name(a["identity"]), bool(a.get("running")), a.get("profile"),
                           bool(a.get("ask")), a.get("settings"), remotes, a.get("counters"), list(a.get("ports", []))))
    out.sort(key=lambda x: (not x.running, -x.active_count, x.name.lower()))
    return out


@dataclass
class Pin:
    lat: float
    lon: float
    label: str
    items: list                 # [(AppView, RemoteView)]

    @property
    def active(self) -> bool:
        return any(r.active for _, r in self.items)

    @property
    def blocked(self) -> bool:
        return all(r.blocked for _, r in self.items)

    @property
    def kind(self) -> str:
        """'out' / 'in' when every connection here goes one way, else 'both' (drawn blue / orange / purple)."""
        dirs = {r.direction for _, r in self.items}
        return dirs.pop() if len(dirs) == 1 else "both"

    def apps(self) -> "list[AppView]":
        seen, out = set(), []
        for app, _ in self.items:
            if app.identity not in seen:
                seen.add(app.identity)
                out.append(app)
        return out


def build_pins(apps: "list[AppView]", precision: int = 1) -> "list[Pin]":
    """Remotes in the same place (rounded to ~10 km by default) share a pin."""
    pins: dict = {}
    for app in apps:
        for r in app.remotes:
            if r.place is None:
                continue
            key = (round(r.place.lat, precision), round(r.place.lon, precision))
            p = pins.get(key)
            if p is None:
                p = pins[key] = Pin(r.place.lat, r.place.lon, r.place.label, [])
            p.items.append((app, r))
    return sorted(pins.values(), key=lambda p: (-p.active, p.label))


def local_items(apps: "list[AppView]") -> "list[tuple]":
    return [(a, r) for a in apps for r in a.remotes if r.local]


def connection_state(r: RemoteView) -> str:
    return "blocked" if r.blocked else "allowed"


def signature(apps: "list[AppView]", pending: list) -> tuple:
    """Cheap fingerprint of what's on screen: if it hasn't changed, nothing needs rebuilding."""
    return (tuple((a.identity, a.running, a.allowed, a.ask, a.profile,
                   tuple(sorted((a.settings or {}).items())),
                   tuple((x["name"], x["port"], x["proto"], x["direction"], x["enabled"]) for x in a.ports),
                   tuple((r.ip, r.direction, r.active, r.blocked, r.rule, r.temp_allowed, r.hostname,
                          tuple((p["proto"], p["port"], p.get("rule"), p.get("active")) for p in r.ports))
                         for r in a.remotes)) for a in apps),
            tuple((p["id"], p["identity"], p["ip"], p["port"]) for p in pending))
