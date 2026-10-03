"""
Recognising "the same app" across launches.

Every app a desktop session starts lives in its own systemd unit (= cgroup) under the user's
``app.slice`` -- ``app-flatpak-org.vinegarhq.Sober-1788577750.scope`` for a Flatpak,
``app-firefox-4242.scope`` / ``app-org.kde.konsole@3f2a.service`` for KDE-launched apps.  The
trailing number / hash changes on every launch, so an app's *identity* is the unit name with that
instance part removed (``flatpak:org.vinegarhq.Sober``, ``app:firefox``).  A profile stores
identities; whenever a new unit with a matching identity appears, the profile applies to it.
That is the "auto detect".

Limit: this separates apps that have their own unit.  A program started from a terminal lives in
the *terminal's* unit -- ``portcullis launch`` starts a command in its own unit for that case.
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path


def cgroup2_root() -> str:
    """Mount point of the cgroup2 hierarchy ($PORTCULLIS_CGROUP_ROOT overrides, for tests)."""
    env = os.environ.get("PORTCULLIS_CGROUP_ROOT")
    if env:
        return env
    try:
        for line in Path("/proc/self/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] == "cgroup2":
                return parts[1]
    except OSError:
        pass
    return "/sys/fs/cgroup"


def _unescape(name: str) -> str:
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m[1], 16)), name)


def identity(unit: str) -> "str | None":
    """``app-flatpak-org.x.Y-123.scope`` -> ``flatpak:org.x.Y``; None for non-app units."""
    m = re.fullmatch(r"app-(.+)\.(?:scope|service)", unit)
    if not m:
        return None
    rest = _unescape(m[1])
    if rest.startswith("flatpak-"):
        return "flatpak:" + re.sub(r"-\d+$", "", rest[len("flatpak-"):])
    return "app:" + re.sub(r"(@[0-9a-fA-F]+|-\d+)$", "", rest)


def pretty_name(identity: str) -> str:
    """A short human name for an identity: flatpak:org.vinegarhq.Sober -> Sober, app:firefox -> firefox."""
    kind, _, rest = identity.partition(":")
    name = rest.rsplit(".", 1)[-1] if kind == "flatpak" and "." in rest else rest
    return name or identity


@dataclass(frozen=True)
class AppCgroup:
    unit: str
    relpath: str            # cgroup path relative to the cgroup2 root
    identity: str
    uid: "int | None" = None


def scan(root: "str | None" = None) -> "list[AppCgroup]":
    """Every running app unit of every logged-in user."""
    root = root or cgroup2_root()
    out = []
    users = Path(root, "user.slice")
    try:
        slices = sorted(users.glob("user-*.slice"))
    except OSError:
        return out
    for uslice in slices:
        m = re.fullmatch(r"user-(\d+)\.slice", uslice.name)
        uid = int(m[1]) if m else None
        app_slice = uslice / f"user@{uid}.service" / "app.slice"
        try:
            entries = sorted(p for p in app_slice.iterdir() if p.is_dir())
        except OSError:
            continue
        for p in entries:
            ident = identity(p.name)
            if ident:
                out.append(AppCgroup(p.name, str(p.relative_to(root)), ident, uid))
    return out


def matches(spec: str, app: AppCgroup) -> bool:
    """A profile's match entry against a running app.

    ``flatpak:ID`` / ``app:NAME`` -- the identity (case-insensitive);
    ``unit:GLOB``  -- shell-style glob on the raw unit name;
    a bare word   -- the part after the colon of either kind of identity.
    """
    spec = spec.strip()
    if not spec:
        return False
    low = spec.lower()
    if low.startswith("unit:"):
        return fnmatch.fnmatchcase(app.unit, spec[5:])
    if ":" in spec:
        return app.identity.lower() == low
    return app.identity.split(":", 1)[1].lower() == low
