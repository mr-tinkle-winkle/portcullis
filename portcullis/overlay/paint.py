"""Drawing the overlay panel (shared by the overlay window and the Settings preview)."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen

from ..ui_kit import contrast_text
from . import model

TITLE = "BLOCKED"


class PanelPainter:
    """Lays out and paints the list of blocked things.  Everything scales with ``scale``."""

    def __init__(self, scale: float = 1.0, font: "QFont | None" = None):
        self.scale = max(0.5, min(3.0, float(scale)))
        base = QFont(font) if font is not None else QFont()
        self.f_title = QFont(base)
        self.f_title.setPointSizeF(8.5 * self.scale)
        self.f_title.setBold(True)
        self.f_title.setLetterSpacing(QFont.AbsoluteSpacing, 1.2 * self.scale)
        self.f_app = QFont(base)
        self.f_app.setPointSizeF(10.5 * self.scale)
        self.f_app.setBold(True)
        self.f_chip = QFont(base)
        self.f_chip.setPointSizeF(8.5 * self.scale)
        self.f_chip.setBold(True)
        self.pad = 12 * self.scale
        self.gap = 6 * self.scale
        self.row_h = QFontMetricsF(self.f_app).height() + 8 * self.scale
        self.chip_h = QFontMetricsF(self.f_chip).height() + 4 * self.scale
        self.stripe = 4 * self.scale

    # -- geometry -----------------------------------------------------------------------------------------------------
    def _chip_w(self, text: str) -> float:
        return QFontMetricsF(self.f_chip).horizontalAdvance(text) + 12 * self.scale

    def _chip_texts(self, line: "model.Line", now: float) -> "list[str]":
        out = []
        for c in line.chips:
            cd = model.countdown(c.deadline, now)
            out.append(f"{c.text} {cd}" if cd else c.text)
        return out

    def _app_col(self, lines) -> float:
        fm = QFontMetricsF(self.f_app)
        return max([fm.horizontalAdvance(ln.app) for ln in lines] + [40 * self.scale])

    def size(self, lines: "list[model.Line]", now: float) -> "tuple[float, float]":
        if not lines:
            return (0.0, 0.0)
        title_h = QFontMetricsF(self.f_title).height()
        chips_w = 0.0
        for ln in lines:
            ws = [self._chip_w(t) for t in self._chip_texts(ln, now)]
            chips_w = max(chips_w, sum(ws) + self.gap * max(0, len(ws) - 1))
        w = self.stripe + self.pad * 2 + self._app_col(lines) + (self.gap * 2 + chips_w if chips_w else 0)
        h = self.pad * 2 + title_h + self.gap + self.row_h * len(lines)
        return (w, h)

    def max_size(self) -> "tuple[float, float]":
        """A size every reasonable panel fits in (the overlay surface is this big; the panel sits in a corner of it)."""
        sample = [model.Line("Some long app name", [model.Chip("OUT 10.0s", "out"), model.Chip("keep-alive 1500 B", "note"),
                                                    model.Chip("IN 10.0s", "in"), model.Chip("port longportname", "special")])]
        w, _ = self.size(sample, 0.0)
        _, h = self.size(sample * model.MAX_LINES, 0.0)
        return (w + 120 * self.scale, h + 4)

    @staticmethod
    def placed(corner: str, panel: "tuple[float, float]", area: QRectF) -> QRectF:
        """The panel's rect inside ``area``, pushed into ``corner``."""
        v, h = model.corner_edges(corner)
        w, hh = panel
        x = area.left() if h == "left" else area.right() - w
        y = area.top() if v == "top" else area.bottom() - hh
        return QRectF(x, y, w, hh)

    # -- painting -----------------------------------------------------------------------------------------------------
    def paint(self, p: QPainter, rect: QRectF, lines: "list[model.Line]", now: float, signals, surface: QColor,
              text: QColor) -> None:
        if not lines:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        radius = 10 * self.scale
        body = QPainterPath()
        body.addRoundedRect(rect, radius, radius)
        bg = QColor(surface)
        bg.setAlpha(225)
        p.fillPath(body, bg)
        p.setClipPath(body)
        p.fillRect(QRectF(rect.left(), rect.top(), self.stripe, rect.height()), signals.off)      # red: blocked
        p.setClipping(False)
        edge = QColor(signals.off)
        edge.setAlphaF(0.55)
        p.setPen(QPen(edge, 1.2 * self.scale))
        p.setBrush(Qt.NoBrush)
        p.drawPath(body)

        x0 = rect.left() + self.stripe + self.pad
        y = rect.top() + self.pad
        p.setFont(self.f_title)
        p.setPen(signals.off)
        title_h = QFontMetricsF(self.f_title).height()
        p.drawText(QRectF(x0, y, rect.width(), title_h), Qt.AlignLeft | Qt.AlignVCenter, TITLE)
        y += title_h + self.gap
        app_w = self._app_col(lines)
        for ln in lines:
            row = QRectF(x0, y, rect.right() - x0 - self.pad, self.row_h)
            p.setFont(self.f_app)
            p.setPen(text)
            p.drawText(QRectF(row.left(), row.top(), app_w, row.height()), Qt.AlignLeft | Qt.AlignVCenter, ln.app)
            cx = row.left() + app_w + self.gap * 2
            p.setFont(self.f_chip)
            for chip, label in zip(ln.chips, self._chip_texts(ln, now)):
                w = self._chip_w(label)
                r = QRectF(cx, row.center().y() - self.chip_h / 2, w, self.chip_h)
                if chip.kind == "note":
                    muted = QColor(text)
                    muted.setAlphaF(0.65)
                    p.setPen(muted)
                    p.drawText(r, Qt.AlignCenter, label)
                else:
                    fill = {"out": signals.output, "in": signals.input}.get(chip.kind, signals.special)
                    path = QPainterPath()
                    path.addRoundedRect(r, self.chip_h / 2, self.chip_h / 2)
                    p.fillPath(path, fill)
                    p.setPen(contrast_text(fill))
                    p.drawText(r, Qt.AlignCenter, label)
                cx += w + self.gap
            y += self.row_h
        p.restore()
