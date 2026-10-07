"""Profiles: which app(s), and what to do to their traffic.  Stored as JSON by the daemon."""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import decisions

MAX_DELAY_MS = 5000
MAX_PACKET = 1500
MAX_AUTO_S = 86400.0
MAX_NAME = 64


def state_dir() -> Path:
    return Path(os.environ.get("PORTCULLIS_STATE_DIR", "/var/lib/portcullis"))


@dataclass
class Profile:
    name: str
    qid: int = 0                                  # stable id; names the NFQUEUE numbers
    match: list = field(default_factory=list)     # identities, see appid.matches
    enabled: bool = False                         # the master switch ("active")
    block_in: bool = False
    block_out: bool = False
    delay_in_ms: int = 0
    delay_out_ms: int = 0
    auto_unblock_in_s: float = 0.0               # switch block_in off again after this long (0 = never)
    auto_unblock_out_s: float = 0.0              # switch block_out off again after this long (0 = never)
    block_out_above: int = 0                      # with block_out: let outgoing UDP packets up to this size through
                                                  # (0 = block everything).  Keeps the game's acks/keep-alives flowing.
    ask: str = "default"                          # "default" (follow the global setting) | "ask" | "allow"
    rules: list = field(default_factory=list)     # per-remote rules, see decisions.py
    ports: list = field(default_factory=list)     # named ports (enable / disable each), see decisions.clean_port

    def sanitize(self) -> "Profile":
        self.name = " ".join(str(self.name).split())[:MAX_NAME]
        self.match = [str(m).strip() for m in self.match if str(m).strip()]
        self.delay_in_ms = max(0, min(MAX_DELAY_MS, int(self.delay_in_ms)))
        self.delay_out_ms = max(0, min(MAX_DELAY_MS, int(self.delay_out_ms)))
        self.block_out_above = max(0, min(MAX_PACKET, int(self.block_out_above)))
        for k in ("auto_unblock_in_s", "auto_unblock_out_s"):
            setattr(self, k, round(max(0.0, min(MAX_AUTO_S, float(getattr(self, k)))), 2))
        self.enabled, self.block_in, self.block_out = bool(self.enabled), bool(self.block_in), bool(self.block_out)
        if self.ask not in ("default", "ask", "allow"):
            self.ask = "default"
        clean = []
        for r in self.rules if isinstance(self.rules, list) else []:
            try:
                c = decisions.clean_rule(r)
            except (KeyError, ValueError, TypeError):
                continue
            if c not in clean:
                clean.append(c)
        self.rules = clean
        ports, seen = [], set()
        for r in self.ports if isinstance(self.ports, list) else []:
            try:
                c = decisions.clean_port(r)
            except (KeyError, ValueError, TypeError):
                continue
            if c["name"].lower() not in seen:
                seen.add(c["name"].lower())
                ports.append(c)
        self.ports = ports
        return self

    def has_effect(self) -> bool:
        return self.block_in or self.block_out or self.delay_in_ms > 0 or self.delay_out_ms > 0

    def port_blocks(self) -> list:
        """The named ports that are switched off (their traffic is dropped)."""
        return [x for x in self.ports if not x["enabled"]]

    def relevant(self) -> bool:
        """Does this profile change anything at all (blocking, delay, per-remote rules, ask mode)?"""
        return self.has_effect() or bool(self.rules) or bool(self.ports) or self.ask != "default"


EDITABLE = {"name", "match", "enabled", "block_in", "block_out", "delay_in_ms", "delay_out_ms", "ask", "block_out_above",
            "auto_unblock_in_s", "auto_unblock_out_s"}
BOOLS = {"enabled", "block_in", "block_out"}


class StoreError(ValueError):
    pass


class ProfileStore:
    def __init__(self, path: "Path | None" = None, clock=time.monotonic):
        self.path = Path(path) if path else state_dir() / "profiles.json"
        self._clock = clock
        self._since: dict = {}                     # (qid, "in"|"out") -> when that block (last) went on
        self.lock = threading.RLock()
        self._profiles: "list[Profile]" = []
        self._next_qid = 0
        self.load()

    # -- persistence ------------------------------------------------------------------------
    def load(self) -> None:
        with self.lock:
            self._profiles, self._next_qid = [], 0
            try:
                data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                return
            names = {f.name for f in fields(Profile)}
            for raw in data.get("profiles", []) if isinstance(data, dict) else []:
                try:
                    p = Profile(**{k: v for k, v in raw.items() if k in names}).sanitize()
                except (TypeError, ValueError):
                    continue
                if p.name and all(p.qid != q.qid for q in self._profiles):
                    self._profiles.append(p)
            self._next_qid = max([data.get("next_qid", 0) if isinstance(data, dict) else 0]
                                 + [p.qid + 1 for p in self._profiles])

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"next_qid": self._next_qid, "profiles": [asdict(p) for p in self._profiles]}, indent=1))
        os.replace(tmp, self.path)

    # -- queries ------------------------------------------------------------------------------
    def all(self) -> "list[Profile]":
        with self.lock:
            return [Profile(**asdict(p)) for p in self._profiles]

    def find(self, name: str) -> "Profile | None":
        with self.lock:
            low = str(name).strip().lower()
            return next((p for p in self._profiles if p.name.lower() == low), None)

    def by_qid(self, qid: int) -> "Profile | None":
        with self.lock:
            return next((p for p in self._profiles if p.qid == qid), None)

    # -- edits ----------------------------------------------------------------------------------
    def add(self, **fields_) -> Profile:
        with self.lock:
            name = " ".join(str(fields_.get("name", "")).split())
            if not name:
                raise StoreError("a profile needs a name")
            if self.find(name):
                raise StoreError(f"a profile named {name!r} already exists")
            p = Profile(name=name, qid=self._next_qid)
            self._apply(p, {k: v for k, v in fields_.items() if k != "name"})
            self._track(p, fields_)
            self._next_qid += 1
            self._profiles.append(p)
            self._save()
            return p

    def update(self, name: str, changes: dict) -> Profile:
        with self.lock:
            p = self.find(name)
            if p is None:
                raise StoreError(f"no profile named {name!r}")
            new_name = changes.get("name")
            if new_name is not None:
                other = self.find(new_name)
                if other is not None and other.qid != p.qid:
                    raise StoreError(f"a profile named {new_name!r} already exists")
            self._apply(p, changes)
            self._track(p, changes)
            self._save()
            return p

    # -- auto-unblock ---------------------------------------------------------------------------
    def _track(self, p: Profile, changes: dict) -> None:
        """Remember when a block went on: (re)setting it, or switching the profile on, starts its clock again."""
        now = self._clock()
        for d in ("in", "out"):
            key = (p.qid, d)
            if p.enabled and getattr(p, f"block_{d}"):
                if f"block_{d}" in changes or "enabled" in changes or key not in self._since:
                    self._since[key] = now
            else:
                self._since.pop(key, None)

    def expire_blocks(self) -> "list[str]":
        """Switch off blocks whose auto-unblock time is up; returns 'profile:direction' for each."""
        done = []
        with self.lock:
            now = self._clock()
            for p in self._profiles:
                for d in ("in", "out"):
                    after = getattr(p, f"auto_unblock_{d}_s")
                    if not (after > 0 and p.enabled and getattr(p, f"block_{d}")):
                        continue
                    since = self._since.setdefault((p.qid, d), now)      # (blocked before a restart: count from now)
                    if now - since >= after:
                        setattr(p, f"block_{d}", False)
                        self._since.pop((p.qid, d), None)
                        done.append(f"{p.name}:{d}")
            if done:
                self._save()
        return done

    def unblock_left(self, p: Profile, d: str) -> "float | None":
        """Seconds until this block switches itself off, or None (not blocked / no timer)."""
        with self.lock:
            after = getattr(p, f"auto_unblock_{d}_s")
            if not (after > 0 and p.enabled and getattr(p, f"block_{d}")):
                return None
            return max(0.0, after - (self._clock() - self._since.get((p.qid, d), self._clock())))

    def unblock_due(self) -> "float | None":
        """Seconds until the next auto-unblock, or None when none is pending."""
        with self.lock:
            now, best = self._clock(), None
            for p in self._profiles:
                for d in ("in", "out"):
                    after = getattr(p, f"auto_unblock_{d}_s")
                    if after > 0 and p.enabled and getattr(p, f"block_{d}"):
                        left = after - (now - self._since.get((p.qid, d), now))
                        best = left if best is None else min(best, left)
            return None if best is None else max(0.0, best)

    def remove(self, name: str) -> None:
        with self.lock:
            p = self.find(name)
            if p is None:
                raise StoreError(f"no profile named {name!r}")
            self._profiles.remove(p)
            self._save()

    def ensure_for(self, identity: str) -> Profile:
        """The profile for an app identity, created (switched on, empty) when there is none yet --
        what the map UI does when you first block a connection of an app."""
        from . import appid
        with self.lock:
            for p in self._profiles:
                if p.enabled and identity in p.match:
                    return p
            for p in self._profiles:
                if identity in p.match:
                    p.enabled = True
                    self._track(p, {"enabled": True})
                    self._save()
                    return p
            base = appid.pretty_name(identity)
            name, n = base, 2
            while self.find(name):
                name, n = f"{base} {n}", n + 1
            return self.add(name=name, match=[identity], enabled=True)

    def set_rule(self, identity: str, rule: dict, clear: bool = False) -> Profile:
        """Add / replace (or with clear=True remove) the rule for rule's ip[:port/proto]."""
        with self.lock:
            p = self.ensure_for(identity)
            try:
                new = decisions.clean_rule({"verdict": "allow", **rule})
            except (KeyError, ValueError, TypeError) as e:
                raise StoreError(f"bad rule: {e}") from None
            same = lambda r: (r["ip"], r["port"], r["proto"]) == (new["ip"], new["port"], new["proto"])  # noqa: E731
            p.rules = [r for r in p.rules if not same(r)]
            if not clear:
                p.rules.append(new)
            self._save()
            return p

    def port_action(self, p: Profile, action: str, name: str = "", spec: "dict | None" = None) -> Profile:
        """add / remove / enable / disable / toggle a named port of ``p``.  ``name`` may also be the port number."""
        with self.lock:
            def find(key):
                key = str(key).strip().lower()
                hit = [x for x in p.ports if x["name"].lower() == key]
                return hit or [x for x in p.ports if key.isdigit() and x["port"] == int(key)]
            if action == "add":
                try:
                    new = decisions.clean_port(spec or {})
                except ValueError as e:
                    raise StoreError(f"bad port: {e}") from None
                p.ports = [x for x in p.ports if x["name"].lower() != new["name"].lower()] + [new]
            elif action in ("remove", "enable", "disable", "toggle"):
                hit = find(name)
                if not hit:
                    known = ", ".join(x["name"] for x in p.ports) or "none yet"
                    raise StoreError(f"{p.name!r} has no port {name!r} (it has: {known})")
                if action == "remove":
                    p.ports = [x for x in p.ports if x not in hit]
                else:
                    for x in hit:
                        x["enabled"] = (not x["enabled"]) if action == "toggle" else action == "enable"
            else:
                raise StoreError(f"unknown port action {action!r}")
            p.sanitize()
            self._save()
            return p

    @staticmethod
    def _apply(p: Profile, changes: dict) -> None:
        for key, val in changes.items():
            if key not in EDITABLE:
                raise StoreError(f"unknown setting {key!r}")
            if key in BOOLS and val == "toggle":
                val = not getattr(p, key)
            if key in BOOLS and not isinstance(val, bool):
                raise StoreError(f"{key} must be true, false or 'toggle'")
            if key in ("auto_unblock_in_s", "auto_unblock_out_s"):
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    raise StoreError(f"{key} must be a number of seconds")
            if key in ("delay_in_ms", "delay_out_ms", "block_out_above"):
                if isinstance(val, bool) or not isinstance(val, int):
                    raise StoreError(f"{key} must be a whole number of milliseconds")
            if key == "match" and not isinstance(val, list):
                raise StoreError("match must be a list")
            if key == "ask" and val not in ("default", "ask", "allow"):
                raise StoreError("ask must be 'default', 'ask' or 'allow'")
            setattr(p, key, val)
        p.sanitize()
        if not p.name:
            raise StoreError("a profile needs a name")
