"""
Replaces the native KDE-styled QLineEdit box with the same rounded,
accent-outlined, card-background-filled look the Library's search bar
already has -- per a direct request to apply that same box style
to every text field, not just the search bar. Text editing itself
(cursor, selection, IME, etc.) is still QLineEdit's own native
behavior; only the background/border painting is replaced, and a
stylesheet strips QLineEdit's own opaque background so this widget's
own paintEvent (drawn first) shows through underneath the native text
rendering, same layering trick CustomButton and _InfoBox already use.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QLineEdit

from .theme_config import get_settings
from .rounded_rect import rounded_rect_path
from .theme import Theme, contrast_text


class CustomLineEdit(QLineEdit):
    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        appearance = get_settings()
        self._appearance = appearance
        self._theme = Theme(appearance)
        text_color = contrast_text(self._theme.surface()).name()
        accent = self._theme.accent().name()
        # Scoped to QLineEdit specifically (not a bare "background-color:
        # ..." with no type selector) -- deliberately avoiding the exact
        # unscoped-stylesheet-cascade mistake documented in HANDOFF.md.
        self.setStyleSheet(
            f"QLineEdit {{ background: transparent; border: none; color: {text_color}; "
            f"padding: 2px 8px; selection-background-color: {accent}; }}"
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        radius = self._appearance.rounded_corner_radius if self._appearance.rounded_corners_enabled else 8
        radius = min(radius, rect.height() / 2) if radius else 0
        if radius:
            path = rounded_rect_path(rect, radius)
            painter.fillPath(path, self._theme.surface())
            pen = painter.pen()
            pen.setColor(self._theme.accent())
            pen.setWidthF(2)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
        else:
            painter.fillRect(self.rect(), self._theme.surface())
            pen = painter.pen()
            pen.setColor(self._theme.accent())
            pen.setWidthF(2)
            painter.setPen(pen)
            painter.drawRect(self.rect().adjusted(1, 1, -1, -1))
        painter.end()
        super().paintEvent(event)
