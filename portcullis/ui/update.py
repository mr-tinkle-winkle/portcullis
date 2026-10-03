"""Noticing that a newer build of portcullis was installed (``nixos-rebuild switch``) while the window keeps
running, so the window can restart itself into it instead of needing to be killed.

On NixOS every build lives in its own ``/nix/store/<hash>-portcullis-<version>`` directory, and
``/run/current-system/sw/bin/portcullis`` points at the build of the current system.  When that differs from
the build this process was started from, an update is waiting."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

CURRENT_SYSTEM_BIN = "/run/current-system/sw/bin/portcullis"


def build_root(path: "str | os.PathLike | None" = None) -> "str | None":
    """``/nix/store/<hash>-name`` for a path inside the store, else None (not a Nix install: no updates to track)."""
    if path is None:
        import portcullis
        path = portcullis.__file__
    try:
        p = Path(path).resolve()
    except OSError:
        return None
    parts = p.parts
    if len(parts) >= 4 and parts[1:3] == ("nix", "store"):
        return str(Path(*parts[:4]))
    return None


def build_id() -> str:
    """What identifies this build (sent to an already-running window, see app.py)."""
    return build_root() or "dev"


def installed_exe(candidates=None) -> "str | None":
    """The ``portcullis`` executable of the installed system, resolved to its store path."""
    for c in candidates or (CURRENT_SYSTEM_BIN, shutil.which("portcullis")):
        if c and os.path.exists(c):
            return os.path.realpath(c)
    return None


def newer_exe(current: "str | None" = None, installed: "str | None" = None) -> "str | None":
    """The executable to restart into when the installed build differs from the running one, else None."""
    current = current if current is not None else build_root()
    installed = installed if installed is not None else installed_exe()
    if not current or not installed:
        return None
    target = build_root(installed)
    return installed if target and target != current else None
