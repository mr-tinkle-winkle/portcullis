"""``portcullis gui`` entry point (the window lives in the ``ui`` package)."""
from __future__ import annotations


def run(hidden: bool = False) -> int:
    from .ui.app import run as _run
    return _run(hidden=hidden)
