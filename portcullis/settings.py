"""Daemon-wide settings (JSON next to the profiles)."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .profiles import StoreError, state_dir


@dataclass
class Settings:
    ask_default: bool = False        # "never allow by default": ask about every new connection of every app
    ask_per_port: bool = False       # ask mode decides per remote IP + port + protocol (else per remote IP)
    track_flows: bool = True         # watch new connections (needed for the map and the connection lists)
    temp_allow_minutes: int = 10     # how long "allow temporarily" lasts
    hold_seconds: int = 20           # how long a new connection waits for your answer before being dropped
    quiet_seconds: int = 5           # after "ignore": retries inside this window are dropped without a new popup

    def sanitize(self) -> "Settings":
        for name in ("ask_default", "ask_per_port", "track_flows"):
            setattr(self, name, bool(getattr(self, name)))
        self.temp_allow_minutes = max(1, min(24 * 60, int(self.temp_allow_minutes)))
        self.hold_seconds = max(3, min(120, int(self.hold_seconds)))
        self.quiet_seconds = max(0, min(300, int(self.quiet_seconds)))
        return self


class SettingsStore:
    def __init__(self, path: "Path | None" = None):
        self.path = Path(path) if path else state_dir() / "settings.json"
        self.lock = threading.RLock()
        self._s = Settings()
        self.load()

    def load(self) -> None:
        with self.lock:
            s = Settings()
            try:
                data = json.loads(self.path.read_text())
                for f in fields(Settings):
                    if f.name in data and type(data[f.name]) is type(getattr(s, f.name)):
                        setattr(s, f.name, data[f.name])
            except (OSError, ValueError, AttributeError):
                pass
            self._s = s.sanitize()

    def get(self) -> Settings:
        with self.lock:
            return Settings(**asdict(self._s))

    def update(self, changes: dict) -> Settings:
        with self.lock:
            names = {f.name for f in fields(Settings)}
            new = Settings(**asdict(self._s))
            for k, v in changes.items():
                if k not in names:
                    raise StoreError(f"unknown setting {k!r}")
                want = type(getattr(new, k))
                if want is bool and not isinstance(v, bool):
                    raise StoreError(f"{k} must be true or false")
                if want is int and (isinstance(v, bool) or not isinstance(v, int)):
                    raise StoreError(f"{k} must be a whole number")
                setattr(new, k, v)
            self._s = new.sanitize()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self._s), indent=1))
            os.replace(tmp, self.path)
            return self.get()
