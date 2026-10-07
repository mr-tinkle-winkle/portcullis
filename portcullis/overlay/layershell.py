"""
Layer-shell for the overlay window on Wayland (the same approach as afterglow's indicator).

A normal Qt window can't place itself on Wayland and won't sit above a fullscreen game, so the overlay is a
*layer-shell surface* on the ``overlay`` layer: no keyboard focus, exclusive zone 0, click-through.  KDE's
LayerShellQt has a C++ API only, so ``native/portcullis_layershell.cpp`` is a small shim (built by the
flake) loaded here with ctypes.

  * the shim and PySide6 must use the SAME Qt -- the flake builds both from one nixpkgs;
  * ``configure`` runs after the QWindow exists (``winId()``) but before the first ``show()``;
  * ``QT_WAYLAND_SHELL_INTEGRATION=layer-shell`` affects every window of a process, so it is set only in the
    overlay process (``enable_in_this_process``), never in the main window.

Found through ``PORTCULLIS_LAYERSHELL_LIB`` (set by the flake's wrapper) or ``native/build`` (developer
build).  Without it: X11 gets a plain always-on-top window; Wayland shows nothing (and says so in the log).
"""
from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path

logger = logging.getLogger("portcullis.overlay.layershell")

ANCHOR_TOP, ANCHOR_BOTTOM, ANCHOR_LEFT, ANCHOR_RIGHT = 1, 2, 4, 8
_BITS = {"top": ANCHOR_TOP, "bottom": ANCHOR_BOTTOM, "left": ANCHOR_LEFT, "right": ANCHOR_RIGHT}
LIB_NAME = "libportcullis_layershell.so"

_lib = None
_tried = False


def anchor_bits(edges) -> int:
    n = 0
    for e in edges:
        n |= _BITS[e]
    return n


def _candidates() -> "list[Path]":
    out = []
    env = os.environ.get("PORTCULLIS_LAYERSHELL_LIB")
    if env:
        out.append(Path(env))
    here = Path(__file__).resolve().parent
    out += [here / LIB_NAME, here.parent.parent / "native" / "build" / LIB_NAME]
    return out


def _load():
    global _lib, _tried
    if _tried:
        return _lib
    _tried = True
    for path in _candidates():
        if path.is_file():
            try:
                lib = ctypes.CDLL(str(path))
                fn = lib.portcullis_layershell_configure
                fn.argtypes = [ctypes.c_void_p] + [ctypes.c_int] * 5
                fn.restype = ctypes.c_int
                _lib = lib
                return _lib
            except (OSError, AttributeError) as e:
                logger.warning("layer-shell shim %s could not be loaded: %s", path, e)
    return None


def available() -> bool:
    return _load() is not None


def enable_in_this_process() -> bool:
    """Make Qt's Wayland plugin use the layer-shell integration -- overlay process only, before QApplication."""
    if not available():
        return False
    os.environ["QT_WAYLAND_SHELL_INTEGRATION"] = "layer-shell"
    return True


def configure(window, edges, margins=(0, 0, 0, 0)) -> bool:
    """Make the (created, not yet shown) QWindow an overlay-layer surface anchored to ``edges``.
    margins = (top, right, bottom, left)."""
    lib = _load()
    if lib is None or window is None:
        return False
    try:
        import shiboken6
        ptr = shiboken6.getCppPointer(window)[0]
    except Exception as e:  # noqa: BLE001
        logger.warning("layer-shell: no native pointer for the window: %s", e)
        return False
    t, r, b, l = margins
    rc = lib.portcullis_layershell_configure(ctypes.c_void_p(ptr), anchor_bits(edges), int(t), int(r), int(b), int(l))
    if rc != 0:
        logger.warning("layer-shell configure failed (rc=%s)", rc)
    return rc == 0
