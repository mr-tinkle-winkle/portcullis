"""
The world map: land from the bundled Natural Earth outlines, a pin for every place your apps are
talking to, an arc from "My Location" to each, and a "Local Network" tag for LAN traffic.

  * click a pin           -> selects the app (the connection panel follows)
  * drag "My Location"    -> moves your pin anywhere (so a stream never shows where you really are)
  * wheel / drag the map  -> zoom / pan (on the globe: spin it);  double-click -> reset the view
  * the corner button     -> flat map <-> globe

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
    """The flat (Mercator) map."""
    zoom: float = 1.0
    cx: float = 0.0          # longitude at the centre of the widget
    cy: float = 18.0         # latitude at the centre

    kind = "flat"

    def px_per_rad(self, w: float, h: float) -> float:
        fit = min(w, h * (2 * math.pi) / (2 * merc(MAX_LAT)))
        return fit * self.zoom / (2 * math.pi)

    def project(self, lon: float, lat: float, w: float, h: float) -> "tuple[float, float]":
        k = self.px_per_rad(w, h)
        return w / 2 + math.radians(lon - self.cx) * k, h / 2 - (merc(lat) - merc(self.cy)) * k

    def visible(self, lon: float, lat: float) -> bool:
        return True

    def unproject(self, x: float, y: float, w: float, h: float) -> "tuple[float, float]":
        k = self.px_per_rad(w, h)
        lon = self.cx + math.degrees((x - w / 2) / k)
        lat = unmerc(merc(self.cy) - (y - h / 2) / k)
        return max(-180.0, min(180.0, lon)), max(-MAX_LAT, min(MAX_LAT, lat))

    def clamp(self, w: float, h: float) -> None:
        self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom))
        self.cx = max(-180.0, min(180.0, self.cx))
        self.cy = max(-MAX_LAT, min(MAX_LAT, self.cy))

    def drag_state(self) -> tuple:
        return (self.cx, self.cy)

    def drag(self, state: tuple, dx: float, dy: float, w: float, h: float) -> None:
        cx0, cy0 = state
        k = self.px_per_rad(w, h)
        self.cx = cx0 - math.degrees(dx / k)
        self.cy = unmerc(merc(cy0) + dy / k)
        self.clamp(w, h)

    def zoom_at(self, x: float, y: float, steps: float, w: float, h: float) -> None:
        before = self.unproject(x, y, w, h)
        self.zoom *= 1.25 ** steps
        self.clamp(w, h)
        k = self.px_per_rad(w, h)                          # keep the point under the cursor fixed
        self.cx = before[0] - math.degrees((x - w / 2) / k)
        self.cy = unmerc(merc(before[1]) + (y - h / 2) / k)
        self.clamp(w, h)


def unit(lon: float, lat: float) -> "tuple[float, float, float]":
    la, lo = math.radians(lat), math.radians(lon)
    return (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la))


def great_circle(a: "tuple[float, float]", b: "tuple[float, float]", n: int = 48) -> "list[tuple[float, float, float]]":
    """n+1 unit vectors from lon/lat a to lon/lat b along the great circle (slerp)."""
    p, q = unit(*a), unit(*b)
    dot = max(-1.0, min(1.0, sum(i * j for i, j in zip(p, q))))
    om = math.acos(dot)
    if om < 1e-6:
        return [p] * (n + 1)
    so = math.sin(om)
    out = []
    for i in range(n + 1):
        t = i / n
        k1, k2 = math.sin((1 - t) * om) / so, math.sin(t * om) / so
        out.append(tuple(k1 * pi + k2 * qi for pi, qi in zip(p, q)))
    return out


@dataclass
class Globe:
    """An orthographic globe, rotated so (lon0, lat0) faces you."""
    zoom: float = 1.0
    lon0: float = -40.0
    lat0: float = 25.0

    kind = "globe"
    MAX_ZOOM = 8.0

    def radius(self, w: float, h: float) -> float:
        return min(w, h) / 2 * 0.92 * self.zoom

    def _rot(self, v: "tuple[float, float, float]") -> "tuple[float, float, float]":
        """World unit vector -> view coordinates (x right, y up, z towards you)."""
        x, y, z = v
        lo, la = math.radians(self.lon0), math.radians(self.lat0)
        # rotate about z by -lon0, then about y by lat0
        x1, y1 = x * math.cos(lo) + y * math.sin(lo), -x * math.sin(lo) + y * math.cos(lo)
        z1 = z
        xf = x1 * math.cos(la) + z1 * math.sin(la)            # towards the viewer
        zf = -x1 * math.sin(la) + z1 * math.cos(la)           # up
        return (y1, zf, xf)

    def view_xyz(self, lon: float, lat: float) -> "tuple[float, float, float]":
        return self._rot(unit(lon, lat))

    def to_screen(self, v: "tuple[float, float, float]", w: float, h: float) -> "tuple[float, float]":
        r = self.radius(w, h)
        return w / 2 + v[0] * r, h / 2 - v[1] * r

    def project(self, lon: float, lat: float, w: float, h: float) -> "tuple[float, float]":
        return self.to_screen(self.view_xyz(lon, lat), w, h)

    def visible(self, lon: float, lat: float) -> bool:
        return self.view_xyz(lon, lat)[2] >= 0

    def unproject(self, x: float, y: float, w: float, h: float) -> "tuple[float, float] | None":
        r = self.radius(w, h)
        vx, vy = (x - w / 2) / r, -(y - h / 2) / r
        rho2 = vx * vx + vy * vy
        if rho2 > 1:
            return None
        vz = math.sqrt(1 - rho2)
        # undo the rotation: view (vx, vy, vz) = (y1, zf, xf)
        lo, la = math.radians(self.lon0), math.radians(self.lat0)
        y1, zf, xf = vx, vy, vz
        x1 = xf * math.cos(la) - zf * math.sin(la)
        z1 = xf * math.sin(la) + zf * math.cos(la)
        xw = x1 * math.cos(lo) - y1 * math.sin(lo)
        yw = x1 * math.sin(lo) + y1 * math.cos(lo)
        lat = math.degrees(math.asin(max(-1.0, min(1.0, z1))))
        lon = math.degrees(math.atan2(yw, xw))
        return lon, lat

    def clamp(self, w: float = 0, h: float = 0) -> None:
        self.zoom = max(MIN_ZOOM, min(self.MAX_ZOOM, self.zoom))
        self.lat0 = max(-85.0, min(85.0, self.lat0))
        self.lon0 = (self.lon0 + 180.0) % 360.0 - 180.0

    def drag_state(self) -> tuple:
        return (self.lon0, self.lat0)

    def drag(self, state: tuple, dx: float, dy: float, w: float, h: float) -> None:
        lon0, lat0 = state
        r = self.radius(w, h)
        self.lon0 = lon0 - math.degrees(dx / r)
        self.lat0 = lat0 + math.degrees(dy / r)
        self.clamp()

    def zoom_at(self, x: float, y: float, steps: float, w: float, h: float) -> None:
        self.zoom *= 1.25 ** steps
        self.clamp()


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
    modeChanged = Signal(str)                    # "flat" | "globe" (the toggle in the corner)

    PIN_R = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(360, 260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._theme = Theme()
        self._views = {"flat": View(), "globe": Globe()}
        self.mode = "flat"
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

    @property
    def view(self):
        return self._views[self.mode]

    @view.setter
    def view(self, v) -> None:
        self._views[v.kind] = v

    def set_mode(self, mode: str) -> None:
        mode = "globe" if mode == "globe" else "flat"
        if mode != self.mode:
            self.mode = mode
            if mode == "globe":                                   # start the globe facing you
                g = self._views["globe"]
                g.lon0, g.lat0 = self.me[1], max(-60.0, min(60.0, self.me[0]))
            self._land = None
            self._hover = None
            self.update()

    def reset_view(self) -> None:
        self.view = View() if self.mode == "flat" else Globe(lon0=self.me[1], lat0=max(-60.0, min(60.0, self.me[0])))
        self.update()

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

    def _shown(self, lat: float, lon: float) -> bool:
        return self.view.visible(lon, lat)

    def _me_pt(self) -> QPointF:
        return self._pt(*self.me)

    def _pin_at(self, pos: QPointF):
        best, best_d = None, (self.PIN_R + 4) ** 2
        for p in self.pins:
            if not self._shown(p.lat, p.lon):
                continue
            c = self._pt(p.lat, p.lon)
            d = (c.x() - pos.x()) ** 2 + (c.y() - pos.y()) ** 2
            if d <= best_d:
                best, best_d = p, d
        return best

    def _hit_me(self, pos: QPointF) -> bool:
        if not self._shown(*self.me):
            return False
        c = self._me_pt()
        return (c.x() - pos.x()) ** 2 + (c.y() - pos.y()) ** 2 <= (self.PIN_R + 6) ** 2 or self._me_rect.contains(pos)

    # -- painting -----------------------------------------------------------------------------------------------------
    def _land_pixmap(self, dpr: float) -> QPixmap:
        if self.mode == "globe":
            return self._globe_pixmap(dpr)
        t = self._theme
        key = ("flat", self.width(), self.height(), round(self.view.zoom, 4), round(self.view.cx, 3), round(self.view.cy, 3),
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

    def _globe_pixmap(self, dpr: float) -> QPixmap:
        t, g = self._theme, self.view
        w, h = self.width(), self.height()
        key = ("globe", w, h, round(g.zoom, 4), round(g.lon0, 2), round(g.lat0, 2),
               t.page_background().rgba(), t.surface().rgba(), t.accent().rgba(), dpr)
        if self._land is not None and self._land[0] == key:
            return self._land[1]
        pm = QPixmap(int(w * dpr), int(h * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(t.page_background())
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        r = g.radius(w, h)
        c = QPointF(w / 2, h / 2)
        # faint halo, then the ocean disc
        halo = QColor(t.text())
        for i, a in ((10, 0.03), (6, 0.05), (3, 0.08)):
            halo.setAlphaF(a)
            p.setPen(QPen(halo, i))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, r + i / 2, r + i / 2)
        disc = QPainterPath()
        disc.addEllipse(c, r, r)
        p.fillPath(disc, t.page_background().darker(135))
        p.setClipPath(disc)
        grid = QPen(t.page_background().lighter(135))
        grid.setWidthF(0.6)
        p.setPen(grid)
        for lon in range(-180, 180, 30):
            self._globe_line(p, [(lon, lat) for lat in range(-90, 91, 3)])
        for lat in range(-60, 61, 30):
            self._globe_line(p, [(lon, lat) for lon in range(-180, 181, 3)])
        pen = QPen(t.accent().lighter(125) if t.accent().lightness() < 100 else t.accent().darker(125))
        pen.setWidthF(0.8)
        p.setPen(pen)
        p.setBrush(t.surface())
        for ring in world_rings():
            pts, any_front = [], False
            for lon, lat in ring:
                v = g.view_xyz(lon, lat)
                if v[2] >= 0:
                    any_front = True
                    pts.append(QPointF(*g.to_screen(v, w, h)))
                else:                                          # behind: pin to the rim so the outline closes
                    n = math.hypot(v[0], v[1]) or 1.0
                    pts.append(QPointF(*g.to_screen((v[0] / n, v[1] / n, 0.0), w, h)))
            if any_front:
                p.drawPolygon(QPolygonF(pts))
        p.setClipping(False)
        rim = QColor(t.text())
        rim.setAlphaF(0.35)
        p.setPen(QPen(rim, 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, r, r)
        p.end()
        self._land = (key, pm)
        return pm

    def _globe_line(self, p: QPainter, lonlats: list) -> None:
        g, w, h = self.view, self.width(), self.height()
        path, drawing = QPainterPath(), False
        for lon, lat in lonlats:
            v = g.view_xyz(lon, lat)
            if v[2] >= 0:
                pt = QPointF(*g.to_screen(v, w, h))
                path.lineTo(pt) if drawing else path.moveTo(pt)
                drawing = True
            else:
                drawing = False
        p.drawPath(path)

    def _arc_path(self, me: QPointF, pin) -> QPainterPath:
        """Flat: a bowed curve.  Globe: the great circle, lifted a little off the surface, far side left out."""
        c = self._pt(pin.lat, pin.lon)
        if self.mode == "flat":
            path = QPainterPath(me)
            path.quadTo(arc_control(me, c), c)
            return path
        g, w, h = self.view, self.width(), self.height()
        pts = great_circle((self.me[1], self.me[0]), (pin.lon, pin.lat))
        n = len(pts) - 1
        dot = max(-1.0, min(1.0, sum(i * j for i, j in zip(pts[0], pts[-1]))))
        lift = 0.06 * math.acos(dot) / math.pi
        path, drawing = QPainterPath(), False
        for i, u in enumerate(pts):
            k = 1 + lift * math.sin(math.pi * i / n)
            v = g._rot(tuple(x * k for x in u))
            if v[2] >= 0:                                       # the far side of the globe stays hidden
                pt = QPointF(*g.to_screen(v, w, h))
                path.lineTo(pt) if drawing else path.moveTo(pt)
                drawing = True
            else:
                drawing = False
        return path

    def _mode_font(self) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(max(8.0, f.pointSizeF() - 1))
        return f

    @property
    def _mode_rect(self) -> QRectF:
        """The flat/globe toggle in the top-right corner (computed, so it works before the first paint)."""
        fm = QFontMetrics(self._mode_font())
        text = "Flat map" if self.mode == "globe" else "Globe"
        w, h = fm.horizontalAdvance(text) + 22, fm.height() + 10
        return QRectF(self.width() - w - 10, 10, w, h)

    def _paint_mode_toggle(self, p: QPainter) -> None:
        t = self._theme
        text = "Flat map" if self.mode == "globe" else "Globe"
        p.setFont(self._mode_font())
        rect = self._mode_rect
        h = rect.height()
        bg = QColor(t.surface())
        bg.setAlpha(225)
        p.setBrush(bg)
        p.setPen(QPen(t.accent(), 1.2))
        p.drawPath(rounded_rect_path(rect, h / 2))
        p.setPen(contrast_text(bg))
        p.drawText(rect, Qt.AlignCenter, text)

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
            p.drawPath(self._arc_path(me, pin))
        me_shown = self._shown(*self.me)
        if self.local_items:
            self._paint_local(p, me, selected, me_shown)
        for pin in self.pins:
            if self._shown(pin.lat, pin.lon):
                self._paint_pin(p, pin, selected)
        if me_shown:
            self._paint_me(p, me)
        else:
            self._me_rect = QRectF()
        self._paint_mode_toggle(p)
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

    def _paint_local(self, p: QPainter, me: QPointF, selected, me_shown: bool = True) -> None:
        t = self._theme
        text = "Local Network"
        f = QFont(self.font())
        f.setPointSizeF(max(8.0, f.pointSizeF() - 1))
        p.setFont(f)
        fm = QFontMetrics(f)
        w, h = fm.horizontalAdvance(text) + 18, fm.height() + 10
        if me_shown:
            rect = QRectF(self.width() / 2 - w / 2, min(self.height() - h - 30, me.y() + 70), w, h)
            rect.moveLeft(max(4, min(self.width() - w - 4, me.x() - w / 2)))
        else:                                                    # you're on the far side of the globe
            rect = QRectF(self.width() / 2 - w / 2, self.height() - h - 30, w, h)
        self._local_rect = rect
        mine = selected is not None and any(a.identity == selected for a, _ in self.local_items)
        dirs = {r.direction for _, r in self.local_items}
        sig = palette.signals()
        col = QColor(sig.for_direction(dirs.pop() if len(dirs) == 1 else "both"))
        col.setAlphaF(0.95 if (mine or selected is None) else 0.3)
        pen = QPen(col)
        pen.setWidthF(3.0 if mine else 2.2)
        p.setPen(pen)
        if me_shown:
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
        if self._mode_rect.contains(pos):
            self.set_mode("flat" if self.mode == "globe" else "globe")
            self.modeChanged.emit(self.mode)
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
        self._drag = ("pan", pos.x(), pos.y(), self.view.drag_state())
        self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e) -> None:
        pos = e.position()
        if self._drag and self._drag[0] == "me":
            got = self.view.unproject(pos.x(), pos.y(), self.width(), self.height())
            if got is not None:                                  # (off the globe: stay where you were)
                self.me = (got[1], got[0])
                self.update()
            return
        if self._drag and self._drag[0] == "pan":
            _, x0, y0, state = self._drag
            self.view.drag(state, pos.x() - x0, pos.y() - y0, self.width(), self.height())
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
        self.setCursor(Qt.OpenHandCursor if self._hit_me(pos) else Qt.PointingHandCursor if (pin or self._mode_rect.contains(pos) or (
            self.local_items and self._local_rect.contains(pos))) else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e) -> None:
        if self._drag and self._drag[0] == "me":
            self.locationChanged.emit(*self.me)
        self._drag = None
        self.setCursor(Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, e) -> None:
        if self._mode_rect.contains(e.position()):
            return
        if not self._hit_me(e.position()) and self._pin_at(e.position()) is None:
            self.reset_view()

    def wheelEvent(self, e) -> None:
        steps = e.angleDelta().y() / 120.0
        if not steps:
            return
        pos = e.position()
        self.view.zoom_at(pos.x(), pos.y(), steps, self.width(), self.height())
        self.update()

    def leaveEvent(self, e) -> None:
        if self._hover is not None:
            self._hover = None
            self.update()
