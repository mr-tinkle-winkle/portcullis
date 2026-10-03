"""
Custom-styled replacement for QMessageBox.information/warning/critical
-- same rounded, theme-colored, frameless treatment as the video
previewer and the Add Filter dialog, rather than the native/KDE
message box chrome. Covers the OBS "Test Connection" result and every
other QMessageBox call site in Settings.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel

from .theme_config import get_settings
from .theme import Theme
from .rounded_rect import rounded_rect_path
from .custom_button import CustomButton


class CustomMessageDialog(QDialog):
    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        appearance = get_settings()
        self._appearance = appearance
        self._theme = Theme(appearance)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)

        title_label = QLabel(title)
        title_label.setStyleSheet(f"color: {appearance.color_text}; font-size: 15px; font-weight: bold;")
        layout.addWidget(title_label)

        text_label = QLabel(text)
        text_label.setWordWrap(True)
        text_label.setStyleSheet(f"color: {appearance.color_text};")
        layout.addWidget(text_label)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        ok_btn = CustomButton("OK")
        ok_btn.clicked.connect(self.accept)
        button_row.addWidget(ok_btn)
        layout.addLayout(button_row)

        self.setMinimumWidth(320)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        radius = self._appearance.rounded_corner_radius if self._appearance.rounded_corners_enabled else 16
        if radius:
            painter.fillPath(rounded_rect_path(rect, radius), self._theme.page_background())
        else:
            painter.fillRect(rect, self._theme.page_background())
        painter.end()


def show_message(parent, title: str, text: str) -> None:
    """Drop-in replacement for QMessageBox.information/warning/critical
    -- there's no severity-based icon distinction here (this app has no
    error/warning/info icon set of its own), just a consistently
    custom-styled dialog for all three."""
    dialog = CustomMessageDialog(title, text, parent=parent)
    dialog.exec()
