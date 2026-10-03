"""
The world map: land from the bundled Natural Earth outlines, a pin for every place your apps are
talking to, an arc from "My Location" to each, and a "Local Network" tag for LAN traffic.

  * click a pin           -> selects the app (the connection panel follows)
  * drag "My Location"    -> moves your pin anywhere (so a stream never shows where you really are)
  * wheel / drag the map  -> zoom / pan;  double-click the map -> reset the view

Drawn with QPainter in the UI kit's colours; the (expensive) land is cached in a pixmap and only
redrawn when the size, view or colours change.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from importlib import resources

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ..ui_kit import Theme, contrast_text, rounded_rect_path
from . import palette

MAX_LAT = 78.0
MIN_ZOOM, MAX_ZOOM = 1.0, 14.0


def merc(lat: float) -> float:
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def unmerc(y: float) -> float:
    return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)


@dataclass
class View:
    zoom: float = 1.0
    cx: float = 0.0          # longitude at the centre of the widget
    cy: float = 18.0         # latitude at the centre

    def px_per_rad(self, w: float, h: float) -> float:
        fit = min(w, h * (2 * math.pi) / (2 * merc(MAX_LAT)))
        return fit * self.zoom / (2 * math.pi)

    def project(self, lon: float, lat: float, w: float, h: float) -> "tuple[float, float]":
        k = self.px_per_rad(w, h)
        return w / 2 + math.radians(lon - self.cx) * k, h / 2 - (merc(lat) - merc(self.cy)) * k

    def unproject(self, x: float, y: float, w: float, h: float) -> "tuple[float, float]":
        k = self.px_per_rad(w, h)
        lon = self.cx + math.degrees((x - w / 2) / k)
        lat = unmerc(merc(self.cy) - (y - h / 2) / k)
        return max(-180.0, min(180.0, lon)), max(-MAX_LAT, min(MAX_LAT, lat))

    def clamp(self, w: float, h: float) -> None:
        self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom))
        self.cx = max(-180.0, min(180.0, self.cx))
        self.cy = max(-MAX_LAT, min(MAX_LAT, self.cy))


_world = None


def world_rings() -> list:
    global _world
    if _world is None:
        data = json.loads(resources.files("portcullis").joinpath("data/world.json").read_text())
        _world = [r for r in data["rings"] if max(y for _, y in r) > -60]          # skip Antarctica
    return _world


def arc_control(a: QPointF, b: QPointF, lift: float = 0.22) -> QPointF:
    """Control point of the quadratic curve from a to b, bowed upward (a flight-path look)."""
    mx, my = (a.x() + b.x()) / 2, (a.y() + b.y()) / 2
    dx, dy = b.x() - a.x(), b.y() - a.y()
    dist = math.hypot(dx, dy)
    return QPointF(mx, my - dist * lift)


class MapWidget(QWidget):
    appSelected = Signal(str, str)               # identity, remote ip
    locationChanged = Signal(float, float)

    PIN_R = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(360, 260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._theme = Theme()
        self.view = View()
        self.me = (25.0, -30.0)
        self.pins: list = []
        self.local_items: list = []
        self.selected: "str | None" = None
        self._land: "tuple[tuple, QPixmap] | None" = None
        self._drag = None                     # ("me",) or ("pan", x, y, cx, cy)
        self._hover = None
        self._me_rect = QRectF()
        self._local_rect = QRectF()
        self._hint = ""

    # -- data ---------------------------------------------------------------------------------------------------
    def set_me(self, lat: float, lon: float) -> None:
        self.me = (lat, lon)
        self.update()

    def set_data(self, pins: list, local_items: list, selected: "str | None") -> None:
        self.pins, self.local_items, self.selected = pins, local_items, selected
        self.update()

    def set_hint(self, text: str) -> None:
        if text != self._hint:
            self._hint = text
            self.update()

    # -- geometry -------------------------------------------------------------------------------------------------
    def _pt(self, lat: float, lon: float) -> QPointF:
        x, y = self.view.project(lon, lat, self.width(), self.height())
        return QPointF(x, y)

    def _me_pt(self) -> QPointF:
        return self._pt(*self.me)

    def _pin_at(self, pos: QPointF):
        best, best_d = None, (self.PIN_R + 4) ** 2
        for p in self.pins:
            c = self._pt(p.lat, p.lon)
            d = (c.x() - pos.x()) ** 2 + (c.y() - pos.y()) ** 2
            if d <= best_d:
                best, best_d = p, d
        return best

    def _hit_me(self, pos: QPointF) -> bool:
        c = self._me_pt()
        return (c.x() - pos.x()) ** 2 + (c.y() - pos.y()) ** 2 <= (self.PIN_R + 6) ** 2 or self._me_rect.contains(pos)

    # -- painting -----------------------------------------------------------------------------------------------------
    def _land_pixmap(self, dpr: float) -> QPixmap:
        t = self._theme
        key = (self.width(), self.height(), round(self.view.zoom, 4), round(self.view.cx, 3), round(self.view.cy, 3),
               t.page_background().rgba(), t.surface().rgba(), t.accent().rgba(), dpr)
        if self._land is not None and self._land[0] == key:
            return self._land[1]
        pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(t.page_background().darker(112))
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(t.surface())
        pen = QPen(t.accent().lighter(125) if t.accent().lightness() < 100 else t.accent().darker(125))
        pen.setWidthF(0.8)
        p.setPen(pen)
        w, h = self.width(), self.height()
        # faint graticule every 30 degrees
        grid = QPen(t.page_background().lighter(135))
        grid.setWidthF(0.6)
        p.setPen(grid)
        for lon in range(-180, 181, 30):
            a, b = self._pt(-MAX_LAT, lon), self._pt(MAX_LAT, lon)
            p.drawLine(a, b)
        for lat in range(-60, 81, 30):
            a, b = self._pt(lat, -180), self._pt(lat, 180)
            p.drawLine(a, b)
        p.setPen(pen)
        for ring in world_rings():
            poly = QPolygonF([self._pt(lat, lon) for lon, lat in ring])
            br = poly.boundingRect()
            if br.right() < 0 or br.left() > w or br.bottom() < 0 or br.top() > h:
                continue
            p.drawPolygon(poly)
        p.end()
        self._land = (key, pm)
        return pm

    def paintEvent(self, event) -> None:
        t = self._theme
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        clip = rounded_rect_path(QRectF(self.rect()), t.corner_radius(8))
        p.setClipPath(clip)
        p.drawPixmap(0, 0, self._land_pixmap(self.devicePixelRatioF()))
        me = self._me_pt()
        selected = self.selected

        # arcs first, pins on top
        for pin in self.pins:
            c = self._pt(pin.lat, pin.lon)
            mine = selected is not None and any(a.identity == selected for a in pin.apps())
            dim = selected is not None and not mine
            sig = palette.signals()
            col = QColor(sig.off if pin.blocked else sig.for_direction(pin.kind))
            col.setAlphaF(0.22 if dim else (0.95 if pin.active else 0.55) if mine or selected is None else 0.3)
            pen = QPen(col)
            pen.setWidthF(2.2 if mine else 1.4)
            if pin.blocked:
                pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            path = QPainterPath(me)
            path.quadTo(arc_control(me, c), c)
            p.drawPath(path)
        if self.local_items:
            self._paint_local(p, me, selected)
        for pin in self.pins:
            self._paint_pin(p, pin, selected)
        self._paint_me(p, me)
        if self._hint:
            f = QFont(self.font())
            f.setPointSizeF(max(8.0, f.pointSizeF() - 1))
            p.setFont(f)
            p.setPen(QColor(t.text().red(), t.text().green(), t.text().blue(), 150))
            p.drawText(QRectF(12, self.height() - 28, self.width() - 24, 20), Qt.AlignLeft | Qt.AlignVCenter, self._hint)
        p.end()

    def _paint_pin(self, p: QPainter, pin, selected) -> None:
        t = self._theme
        c = self._pt(pin.lat, pin.lon)
        mine = selected is not None and any(a.identity == selected for a in pin.apps())
        dim = selected is not None and not mine
        sig = palette.signals()
        base = sig.off if pin.blocked else sig.for_direction(pin.kind)
        fill = QColor(base)
        fill.setAlphaF(0.35 if dim else 1.0)
        ring = QColor(t.text())
        ring.setAlphaF(0.35 if dim else (0.95 if mine else 0.7))
        r = self.PIN_R + (2 if mine else 0) + (1 if pin is self._hover else 0)
        p.setBrush(fill)
        pen = QPen(ring)
        pen.setWidthF(2.0 if mine else 1.4)
        p.setPen(pen)
        p.drawEllipse(c, r, r)
        if not pin.active:
            p.setBrush(Qt.NoBrush)
        n = len(pin.items)
        if n > 1:
            f = QFont(self.font())
            f.setPointSizeF(max(7.0, f.pointSizeF() - 2))
            f.setBold(True)
            p.setFont(f)
            p.setPen(contrast_text(base) if not dim else ring)
            p.drawText(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), Qt.AlignCenter, str(min(n, 99)))
        if pin.blocked:
            p.setPen(QPen(ring, 1.6))
            d = r * 0.55
            p.drawLine(QPointF(c.x() - d, c.y() - d), QPointF(c.x() + d, c.y() + d))
            p.drawLine(QPointF(c.x() - d, c.y() + d), QPointF(c.x() + d, c.y() - d))
        if mine or pin is self._hover:
            self._label(p, c + QPointF(0, -r - 8), pin.label)

    def _label(self, p: QPainter, anchor: QPointF, text: str) -> None:
        t = self._theme
        f = QFont(self.font())
        f.setPointSizeF(max(8.0, f.pointSizeF() - 1))
        p.setFont(f)
        fm = QFontMetrics(f)
        w, h = fm.horizontalAdvance(text) + 16, fm.height() + 8
        rect = QRectF(anchor.x() - w / 2, anchor.y() - h, w, h)
        rect.moveLeft(max(4, min(self.width() - w - 4, rect.left())))
        rect.moveTop(max(4, rect.top()))
        bg = QColor(t.surface())
        bg.setAlpha(235)
        p.setBrush(bg)
        p.setPen(QPen(t.accent(), 1))
        p.drawPath(rounded_rect_path(rect, min(10, h / 2)))
        p.setPen(contrast_text(bg))
        p.drawText(rect, Qt.AlignCenter, text)

    def _paint_local(self, p: QPainter, me: QPointF, selected) -> None:
        t = self._theme
        text = "Local Network"
        f = QFont(self.font())
        f.setPointSizeF(max(8.0, f.pointSizeF() - 1))
        p.setFont(f)
        fm = QFontMetrics(f)
        w, h = fm.horizontalAdvance(text) + 18, fm.height() + 10
        rect = QRectF(self.width() / 2 - w / 2, min(self.height() - h - 30, me.y() + 70), w, h)
        rect.moveLeft(max(4, min(self.width() - w - 4, me.x() - w / 2)))
        self._local_rect = rect
        mine = selected is not None and any(a.identity == selected for a, _ in self.local_items)
        dirs = {r.direction for _, r in self.local_items}
        sig = palette.signals()
        col = QColor(sig.for_direction(dirs.pop() if len(dirs) == 1 else "both"))
        col.setAlphaF(0.95 if (mine or selected is None) else 0.3)
        pen = QPen(col)
        pen.setWidthF(3.0 if mine else 2.2)
        p.setPen(pen)
        p.drawLine(me, QPointF(rect.center().x(), rect.top()))
        bg = QColor(t.surface())
        p.setBrush(bg)
        p.setPen(QPen(col, 1.5))
        p.drawPath(rounded_rect_path(rect, h / 2))
        p.setPen(contrast_text(bg))
        p.drawText(rect, Qt.AlignCenter, text)

    def _paint_me(self, p: QPainter, me: QPointF) -> None:
        t = self._theme
        r = self.PIN_R + 2
        p.setBrush(t.accent())
        p.setPen(QPen(t.text(), 2.0))
        p.drawEllipse(me, r, r)
        p.setBrush(t.text())
        p.setPen(Qt.NoPen)
        p.drawEllipse(me, 2.5, 2.5)
        text = "My Location"
        f = QFont(self.font())
        f.setPointSizeF(max(8.0, f.pointSizeF() - 1))
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        w, h = fm.horizontalAdvance(text) + 18, fm.height() + 8
        rect = QRectF(me.x() - w / 2, me.y() - r - h - 6, w, h)
        rect.moveLeft(max(4, min(self.width() - w - 4, rect.left())))
        if rect.top() < 4:
            rect.moveTop(me.y() + r + 6)
        self._me_rect = rect
        bg = QColor(t.surface())
        p.setBrush(bg)
        p.setPen(QPen(t.accent(), 1.5))
        p.drawPath(rounded_rect_path(rect, h / 2))
        p.setPen(contrast_text(bg))
        p.drawText(rect, Qt.AlignCenter, text)

    # -- mouse -----------------------------------------------------------------------------------------------------------
    def mousePressEvent(self, e) -> None:
        pos = e.position()
        if e.button() != Qt.LeftButton:
            return
        if self._hit_me(pos):
            self._drag = ("me",)
            self.setCursor(Qt.ClosedHandCursor)
            return
        pin = self._pin_at(pos)
        if pin is not None:
            apps = pin.apps()
            app = next((a for a in apps if a.identity == self.selected), apps[0])
            remote = next(r for a, r in pin.items if a.identity == app.identity)
            self.appSelected.emit(app.identity, remote.ip)
            return
        if self.local_items and self._local_rect.contains(pos):
            app, remote = next(((a, r) for a, r in self.local_items if a.identity == self.selected), self.local_items[0])
            self.appSelected.emit(app.identity, remote.ip)
            return
        self._drag = ("pan", pos.x(), pos.y(), self.view.cx, self.view.cy)
        self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e) -> None:
        pos = e.position()
        if self._drag and self._drag[0] == "me":
            lon, lat = self.view.unproject(pos.x(), pos.y(), self.width(), self.height())
            self.me = (lat, lon)
            self.update()
            return
        if self._drag and self._drag[0] == "pan":
            _, x0, y0, cx0, cy0 = self._drag
            k = self.view.px_per_rad(self.width(), self.height())
            self.view.cx = cx0 - math.degrees((pos.x() - x0) / k)
            self.view.cy = unmerc(merc(cy0) + (pos.y() - y0) / k)
            self.view.clamp(self.width(), self.height())
            self.update()
            return
        pin = self._pin_at(pos)
        if pin is not self._hover:
            self._hover = pin
            self.update()
            if pin is not None:
                names = ", ".join(sorted({a.name for a in pin.apps()}))
                ips = ", ".join(r.title for _, r in pin.items[:6]) + (" ..." if len(pin.items) > 6 else "")
                QToolTip.showText(e.globalPosition().toPoint(), f"{pin.label}\n{names}\n{ips}", self)
            else:
                QToolTip.hideText()
        self.setCursor(Qt.OpenHandCursor if self._hit_me(pos) else Qt.PointingHandCursor if (pin or (
            self.local_items and self._local_rect.contains(pos))) else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e) -> None:
        if self._drag and self._drag[0] == "me":
            self.locationChanged.emit(*self.me)
        self._drag = None
        self.setCursor(Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, e) -> None:
        if not self._hit_me(e.position()) and self._pin_at(e.position()) is None:
            self.view = View()
            self.update()

    def wheelEvent(self, e) -> None:
        steps = e.angleDelta().y() / 120.0
        if not steps:
            return
        pos = e.position()
        before = self.view.unproject(pos.x(), pos.y(), self.width(), self.height())
        self.view.zoom *= 1.25 ** steps
        self.view.clamp(self.width(), self.height())
        # keep the point under the cursor fixed
        k = self.view.px_per_rad(self.width(), self.height())
        self.view.cx = before[0] - math.degrees((pos.x() - self.width() / 2) / k)
        self.view.cy = unmerc(merc(before[1]) + (pos.y() - self.height() / 2) / k)
        self.view.clamp(self.width(), self.height())
        self.update()

    def leaveEvent(self, e) -> None:
        if self._hover is not None:
            self._hover = None
            self.update()
