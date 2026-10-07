"""The overlay's window: transparent and click-through, sized to fit the biggest panel, with the panel drawn
in the chosen corner.  A layer-shell surface anchored to that corner on Wayland; a frameless always-on-top
window placed by geometry on X11."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QGuiApplication, QPainter
from PySide6.QtWidgets import QWidget

from ..ui import palette
from ..ui_kit import Theme
from . import layershell, model
from .paint import PanelPainter


class OverlaySurface(QWidget):
    def __init__(self, corner: str, margin: int, scale: float, screen=None, use_layer_shell: bool = False,
                 clock=time.monotonic):
        super().__init__(None)
        self.corner, self.margin, self.scale = corner, int(margin), float(scale)
        self.screen_obj = screen
        self.layer = use_layer_shell
        self.clock = clock
        self.configured_as_layer = False
        self.lines: "list[model.Line]" = []
        flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput
                 | Qt.WindowDoesNotAcceptFocus)
        if not use_layer_shell and QGuiApplication.platformName() == "xcb":
            flags |= Qt.X11BypassWindowManagerHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setWindowTitle("portcullis overlay")
        self.painter = PanelPainter(scale, self.font())
        w, h = self.painter.max_size()
        self.resize(int(math.ceil(w)), int(math.ceil(h)))

    def present(self) -> None:
        self.winId()                                   # the QWindow must exist before layer-shell configure
        win = self.windowHandle()
        if self.screen_obj is not None and win is not None:
            win.setScreen(self.screen_obj)
        v, h = model.corner_edges(self.corner)
        if self.layer:
            m = self.margin
            margins = (m if v == "top" else 0, m if h == "right" else 0, m if v == "bottom" else 0, m if h == "left" else 0)
            self.configured_as_layer = layershell.configure(win, (v, h), margins)
        else:
            screen = self.screen_obj or QGuiApplication.primaryScreen()
            geo = QRectF(screen.availableGeometry() if screen is not None else QRectF(0, 0, 1920, 1080))
            x = geo.left() + self.margin if h == "left" else geo.right() - self.margin - self.width() + 1
            y = geo.top() + self.margin if v == "top" else geo.bottom() - self.margin - self.height() + 1
            self.move(int(x), int(y))
        self.show()

    def set_lines(self, lines: "list[model.Line]") -> None:
        self.lines = lines
        self.update()

    def panel_rect(self) -> QRectF:
        return PanelPainter.placed(self.corner, self.painter.size(self.lines, self.clock()), QRectF(self.rect()))

    def paintEvent(self, event) -> None:
        if not self.lines:
            return
        p = QPainter(self)
        t = Theme()
        self.painter.paint(p, self.panel_rect(), self.lines, self.clock(), palette.signals(), t.surface(), t.text())
        p.end()
