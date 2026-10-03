"""
Ask mode: new connections wait here for a decision.

``Broker.on_flow`` is called by the flow listeners for every NEW connection (the first packet).
It answers immediately when it can:

  * a rule or temporary allow covers it    -> accept (a *block* rule never gets here: the kernel drops it earlier)
  * the app isn't in ask mode              -> accept
  * it was just ignored (quiet window)     -> drop, silently
  * otherwise                              -> hold the packet and ask (``pending()`` is what the user's
                                              agent shows as a notification); ``answer`` settles it

Held packets are given their verdict through ``sink(pkt, 'accept'|'drop')`` -- the listener thread
applies it.  An unanswered question expires after ``hold_seconds``: the packet is dropped and the
next attempt asks again.
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field

from . import decisions
from .decisions import Temporary

MAX_HELD_PER_ASK = 64
DECISIONS = ("allow_always", "allow_temp", "ignore", "block_always")


@dataclass
class Ask:
    id: int
    identity: str
    direction: str
    ip: str
    port: int
    proto: str
    per_port: bool
    created: float
    expires: float
    packets: list = field(default_factory=list)

    def public(self, now: float) -> dict:
        return {"id": self.id, "identity": self.identity, "direction": self.direction, "ip": self.ip,
                "port": self.port, "proto": self.proto, "per_port": self.per_port,
                "seconds_left": max(0, round(self.expires - now, 1))}


class Broker:
    def __init__(self, *, owner_for, settings, add_rule, sink, clock=time.monotonic, on_change=lambda: None):
        """owner_for(identity) -> the Profile governing the app, or None;  settings() -> Settings;
        add_rule(identity, rule_dict) stores a permanent rule;  sink(pkt, verdict) delivers a verdict."""
        self._owner_for, self._settings, self._add_rule, self._sink = owner_for, settings, add_rule, sink
        self._clock, self._on_change = clock, on_change
        self.temp = Temporary(clock)
        self.lock = threading.RLock()
        self._pending: "dict[tuple, Ask]" = {}
        self._ids = itertools.count(1)
        self.counts = {"accepted": 0, "dropped": 0, "asked": 0}

    # -- called per new flow ----------------------------------------------------------------------------------
    def on_flow(self, identity: str, direction: str, proto: str, ip: str, port: int, pkt) -> "str | None":
        """'accept' / 'drop' now, or None when the packet was taken (held for a decision)."""
        s = self._settings()
        profile = self._owner_for(identity)
        if not decisions.effective_ask(profile, s) or decisions.is_unaskable(ip):
            return "accept"
        verdict = decisions.rule_verdict(profile.rules if profile else [], ip, port, proto)
        if verdict == "allow":
            return "accept"
        if verdict == "block":
            return "drop"
        key = Temporary.key(identity, ip, port, proto, s.ask_per_port)
        with self.lock:
            if self.temp.allowed(key):
                return "accept"
            if self.temp.is_quiet(key):
                return "drop"
            ask = self._pending.get(key)
            if ask is None:
                now = self._clock()
                ask = Ask(next(self._ids), identity, direction, ip, port if s.ask_per_port else 0,
                          proto if s.ask_per_port else "", s.ask_per_port, now, now + s.hold_seconds)
                self._pending[key] = ask
                self.counts["asked"] += 1
                self._on_change()
            if len(ask.packets) >= MAX_HELD_PER_ASK:
                return "drop"
            ask.packets.append(pkt)
            return None

    # -- answers / timeouts ------------------------------------------------------------------------------------
    def pending(self) -> "list[dict]":
        now = self._clock()
        with self.lock:
            return [a.public(now) for a in self._pending.values()]

    def answer(self, ask_id: int, decision: str) -> bool:
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
        s = self._settings()
        with self.lock:
            hit = next(((k, a) for k, a in self._pending.items() if a.id == ask_id), None)
            if hit is None:
                return False                                  # already answered / expired
            key, ask = hit
            del self._pending[key]
        rule = {"ip": ask.ip, "port": ask.port, "proto": ask.proto}
        if decision == "allow_always":
            self._add_rule(ask.identity, {**rule, "verdict": "allow"})
        elif decision == "block_always":
            self._add_rule(ask.identity, {**rule, "verdict": "block"})
        elif decision == "allow_temp":
            self.temp.allow(key, s.temp_allow_minutes * 60)
        else:
            self.temp.quiet(key, s.quiet_seconds)
        verdict = "accept" if decision in ("allow_always", "allow_temp") else "drop"
        self._deliver(ask.packets, verdict)
        self.counts["accepted" if verdict == "accept" else "dropped"] += len(ask.packets)
        self._on_change()
        return True

    def expire(self) -> None:
        now = self._clock()
        with self.lock:
            dead = [k for k, a in self._pending.items() if a.expires <= now]
            asks = [self._pending.pop(k) for k in dead]
        for a in asks:
            self._deliver(a.packets, "drop")
            self.counts["dropped"] += len(a.packets)
        if asks:
            self._on_change()

    def release_all(self) -> None:
        """Shutting down: never leave a packet stranded -- let held ones through."""
        with self.lock:
            asks, self._pending = list(self._pending.values()), {}
        for a in asks:
            self._deliver(a.packets, "accept")

    def _deliver(self, packets, verdict: str) -> None:
        for p in packets:
            self._sink(p, verdict)
