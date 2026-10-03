"""
The signal colours (the same language as Puppetry): everything else in the window stays black and white.

    orange  input / incoming          blue    output / outgoing        purple  both directions, or special
    green   on / allowed / enabled    red     off / blocked / disabled

Read through ``signals()`` (cheap: the config is cached by mtime).  Like the kit's theme, widgets pick
these up when they are built.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from PySide6.QtGui import QColor

from . import config as guicfg

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULTS = {
    "signal_input": "#f2994a",
    "signal_output": "#4da3ff",
    "signal_special": "#b07cff",
    "signal_on": "#3fd08b",
    "signal_off": "#f25c5c",
}

LABELS = {
    "signal_input": "Input / incoming (orange):",
    "signal_output": "Output / outgoing (blue):",
    "signal_special": "Both directions / special (purple):",
    "signal_on": "On / allowed (green):",
    "signal_off": "Off / blocked (red):",
}


@dataclass(frozen=True)
class Signals:
    input: QColor
    output: QColor
    special: QColor
    on: QColor
    off: QColor

    def for_direction(self, direction: str) -> QColor:
        """'in' -> orange, 'out' -> blue, anything else (both) -> purple."""
        return self.input if direction == "in" else self.output if direction == "out" else self.special


def valid(value) -> bool:
    return isinstance(value, str) and bool(HEX.match(value))


def signals() -> Signals:
    cfg = guicfg.load_readonly()
    pick = lambda k: QColor(getattr(cfg, k) if valid(getattr(cfg, k)) else DEFAULTS[k])   # noqa: E731
    return Signals(pick("signal_input"), pick("signal_output"), pick("signal_special"), pick("signal_on"), pick("signal_off"))
