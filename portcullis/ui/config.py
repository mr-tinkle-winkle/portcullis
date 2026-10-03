"""
The window's own settings (TOML in ``$XDG_CONFIG_HOME/portcullis/config.toml``): theme colours, where
"My Location" sits on the map, and a few switches.  Cached by mtime (the UI kit reads the theme very
often -- see UI_THEMING_GUIDE.md, pitfall 1).  The firewall settings live in the daemon, not here.
"""
from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ..ui_kit import ThemeSettings

# The kit's monochrome placeholder scheme until real colours are chosen (Settings -> Appearance).
APP_THEME_DEFAULTS = ThemeSettings()

DEFAULT_LAT, DEFAULT_LON = 25.0, -30.0          # mid-Atlantic: obviously "not me yet" -- drag the pin


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "portcullis" / "config.toml"


@dataclass
class GuiConfig:
    theme: ThemeSettings = field(default_factory=ThemeSettings)
    my_lat: float = DEFAULT_LAT
    my_lon: float = DEFAULT_LON
    advanced_ports: bool = False         # per-port rows and toggles in the connection list
    resolve_hostnames: bool = False      # reverse-DNS the remote addresses (asks your DNS resolver about each one)
    notifications: bool = True           # desktop notification for every question in ask mode
    pinned: list = field(default_factory=list)   # app identities pinned to the top of the list
    signal_input: str = "#f2994a"        # orange: input / incoming
    signal_output: str = "#4da3ff"       # blue: output / outgoing
    signal_special: str = "#b07cff"      # purple: both directions / special
    signal_on: str = "#3fd08b"           # green: on / allowed / enabled
    signal_off: str = "#f25c5c"          # red: off / blocked / disabled
    win_w: int = 1400
    win_h: int = 820

    def sanitize(self) -> "GuiConfig":
        self.my_lat = max(-85.0, min(85.0, float(self.my_lat)))
        self.my_lon = max(-180.0, min(180.0, float(self.my_lon)))
        self.pinned = list(dict.fromkeys(str(x) for x in self.pinned if isinstance(x, str) and x))
        import re
        for name in ("signal_input", "signal_output", "signal_special", "signal_on", "signal_off"):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(getattr(self, name))):
                setattr(self, name, type(self).__dataclass_fields__[name].default)
        self.win_w, self.win_h = max(900, int(self.win_w)), max(560, int(self.win_h))
        return self


_cache: "tuple[tuple, GuiConfig] | None" = None


def _stamp(path: Path) -> tuple:
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), 0, 0)


def _read(path: Path) -> GuiConfig:
    cfg = GuiConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return cfg.sanitize()
    for f in fields(GuiConfig):
        if f.name == "theme" or f.name not in data:
            continue
        cur = getattr(cfg, f.name)
        v = data[f.name]
        if isinstance(cur, bool):
            ok = isinstance(v, bool)
        elif isinstance(cur, float):
            ok = isinstance(v, (int, float)) and not isinstance(v, bool)
        else:
            ok = isinstance(v, type(cur)) and not isinstance(v, bool)
        if ok:
            setattr(cfg, f.name, v)
    for f in fields(ThemeSettings):
        tv = (data.get("theme") or {}).get(f.name)
        if tv is not None and type(tv) is type(getattr(cfg.theme, f.name)):
            setattr(cfg.theme, f.name, tv)
    return cfg.sanitize()


def load_readonly(path: "Path | None" = None) -> GuiConfig:
    """The shared cached object -- never mutate it."""
    global _cache
    path = path or config_path()
    stamp = _stamp(path)
    if _cache is None or _cache[0] != stamp:
        _cache = (stamp, _read(path))
    return _cache[1]


def load(path: "Path | None" = None) -> GuiConfig:
    return copy.deepcopy(load_readonly(path))


def save(cfg: GuiConfig, path: "Path | None" = None) -> None:
    import tomli_w

    global _cache
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {f.name: getattr(cfg, f.name) for f in fields(GuiConfig) if f.name != "theme"}
    data["theme"] = asdict(cfg.theme)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(tomli_w.dumps(data), encoding="utf-8")
    os.replace(tmp, path)
    _cache = None
