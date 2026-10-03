"""Profiles: which app(s), and what to do to their traffic.  Stored as JSON by the daemon."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

MAX_DELAY_MS = 5000
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

    def sanitize(self) -> "Profile":
        self.name = " ".join(str(self.name).split())[:MAX_NAME]
        self.match = [str(m).strip() for m in self.match if str(m).strip()]
        self.delay_in_ms = max(0, min(MAX_DELAY_MS, int(self.delay_in_ms)))
        self.delay_out_ms = max(0, min(MAX_DELAY_MS, int(self.delay_out_ms)))
        self.enabled, self.block_in, self.block_out = bool(self.enabled), bool(self.block_in), bool(self.block_out)
        return self

    def has_effect(self) -> bool:
        return self.block_in or self.block_out or self.delay_in_ms > 0 or self.delay_out_ms > 0


EDITABLE = {"name", "match", "enabled", "block_in", "block_out", "delay_in_ms", "delay_out_ms"}
BOOLS = {"enabled", "block_in", "block_out"}


class StoreError(ValueError):
    pass


class ProfileStore:
    def __init__(self, path: "Path | None" = None):
        self.path = Path(path) if path else state_dir() / "profiles.json"
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
            self._save()
            return p

    def remove(self, name: str) -> None:
        with self.lock:
            p = self.find(name)
            if p is None:
                raise StoreError(f"no profile named {name!r}")
            self._profiles.remove(p)
            self._save()

    @staticmethod
    def _apply(p: Profile, changes: dict) -> None:
        for key, val in changes.items():
            if key not in EDITABLE:
                raise StoreError(f"unknown setting {key!r}")
            if key in BOOLS and val == "toggle":
                val = not getattr(p, key)
            if key in BOOLS and not isinstance(val, bool):
                raise StoreError(f"{key} must be true, false or 'toggle'")
            if key in ("delay_in_ms", "delay_out_ms"):
                if isinstance(val, bool) or not isinstance(val, int):
                    raise StoreError(f"{key} must be a whole number of milliseconds")
            if key == "match" and not isinstance(val, list):
                raise StoreError("match must be a list")
            setattr(p, key, val)
        p.sanitize()
        if not p.name:
            raise StoreError("a profile needs a name")
