"""From the daemon's ``blocks`` reply to the overlay's lines (no Qt here)."""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import appid

CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")
MAX_LINES = 8


@dataclass
class Chip:
    text: str
    kind: str                    # "out" | "in" | "special" | "note"
    deadline: "float | None" = None      # monotonic time the block ends (a countdown is drawn), or None


@dataclass
class Line:
    app: str
    chips: list = field(default_factory=list)


def app_label(entry: dict) -> str:
    """The app's short name (Sober, firefox), else the profile's name."""
    m = entry.get("match") or []
    if len(m) == 1 and ":" in m[0]:
        return appid.pretty_name(m[0])
    return entry.get("name", "?")


def build_lines(blocks: list, now: float, *, ports: bool = True, addresses: bool = False,
                only_running: bool = True) -> "list[Line]":
    """One line per app with something blocked.  ``now`` turns the daemon's seconds-left into deadlines."""
    out = []
    for b in blocks:
        if only_running and not b.get("running", True):
            continue
        chips = []
        if b.get("out"):
            left = b.get("out_left")
            chips.append(Chip("OUT", "out", None if left is None else now + float(left)))
            if b.get("keep_alive"):
                chips.append(Chip(f"keep-alive {int(b['keep_alive'])} B", "note"))
        if b.get("in"):
            left = b.get("in_left")
            chips.append(Chip("IN", "in", None if left is None else now + float(left)))
        if ports:
            chips += [Chip(f"port {name}", "special") for name in b.get("ports_off", [])]
        if addresses and b.get("addresses"):
            n = int(b["addresses"])
            chips.append(Chip(f"{n} address{'es' if n != 1 else ''}", "special"))
        if [c for c in chips if c.kind != "note"]:
            out.append(Line(app_label(b), chips))
    out.sort(key=lambda ln: ln.app.lower())
    if len(out) > MAX_LINES:
        more = len(out) - (MAX_LINES - 1)
        out = out[:MAX_LINES - 1] + [Line(f"+{more} more", [])]
    return out


def countdown(deadline: "float | None", now: float) -> str:
    if deadline is None:
        return ""
    left = max(0.0, deadline - now)
    return f"{left:.1f}s" if left < 10 else f"{left:.0f}s"


def corner_edges(corner: str) -> "tuple[str, str]":
    v, h = (corner if corner in CORNERS else "top-right").split("-")
    return v, h
