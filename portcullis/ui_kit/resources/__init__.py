"""
Bundled assets for ui_kit (currently just checkmark_icon.png, used by
CustomCheckBox and CustomRadioButton).

Uses importlib.resources so it works both from a source checkout and
from an installed wheel / Nix store path. If your package installs via
setuptools, add the PNGs as package data, e.g. in pyproject.toml:

    [tool.setuptools.package-data]
    "yourapp.gui.ui_kit.resources" = ["*.png"]

Pixmaps are cached per filename for the life of the process. Callers
must treat them as read-only (never paint into one you got from here).
"""
from __future__ import annotations

from importlib.resources import as_file, files

_pixmap_cache: dict = {}


def resource_path(name: str):
    return as_file(files(__package__).joinpath(name))


def resource_qpixmap(name: str):
    from PySide6.QtGui import QPixmap

    cached = _pixmap_cache.get(name)
    if cached is not None:
        return cached
    with resource_path(name) as p:
        pixmap = QPixmap(str(p))
    if not pixmap.isNull():  # don't cache a failed load
        _pixmap_cache[name] = pixmap
    return pixmap
