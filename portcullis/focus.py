"""Which app owns the focused window (``--override_to_focused``): kdotool asks KWin for the active window's
process id (KDE Plasma, Wayland or X11), and that process's cgroup names its app unit -- the same identity
portcullis matches profiles on."""
from __future__ import annotations

import shutil
import subprocess

from . import appid


class FocusError(RuntimeError):
    pass


def _kdotool(*args: str, timeout: float = 2.0) -> "str | None":
    try:
        out = subprocess.run(["kdotool", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def focused_pid(run=_kdotool) -> "tuple[int, str]":
    """(pid, window class) of the active window."""
    if run is _kdotool and shutil.which("kdotool") is None:
        raise FocusError("kdotool isn't installed (it tells portcullis which window is focused, on KDE Plasma)")
    wid = run("getactivewindow")
    if not wid:
        raise FocusError("couldn't ask the desktop which window is focused (is this a KDE Plasma session?)")
    pid = run("getwindowpid", wid)
    if not pid or not pid.strip().isdigit():
        raise FocusError("the focused window doesn't report a process id")
    return int(pid), run("getwindowclassname", wid) or ""


def identity_of_pid(pid: int, proc: str = "/proc") -> "tuple[str, str]":
    """(app identity, unit) for a process, from its cgroup path."""
    try:
        with open(f"{proc}/{pid}/cgroup") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        raise FocusError(f"can't read the cgroup of process {pid}: {e}") from None
    path = next((ln.split("::", 1)[1] for ln in lines if ln.startswith("0::")), "")
    for unit in reversed([c for c in path.split("/") if c]):     # innermost app unit wins
        ident = appid.identity(unit)
        if ident:
            return ident, unit
    raise FocusError(f"the focused window (process {pid}) isn't running in its own app unit ({path or 'no cgroup'}); "
                     "apps started from a terminal share the terminal's unit -- use `portcullis launch`")


def focused_identity(run=_kdotool, proc: str = "/proc") -> "tuple[str, str]":
    pid, _cls = focused_pid(run)
    return identity_of_pid(pid, proc)
