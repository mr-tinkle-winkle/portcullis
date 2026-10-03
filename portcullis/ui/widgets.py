"""
The window's building blocks, all on the UI kit: the app list (left), the connection panel (right),
the status banner and the "waiting for you" panel.  They only emit signals; the window decides what
to ask the daemon.
"""
from __future__ import annotations

import time

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QAbstractButton, QComboBox, QGraphicsOpacityEffect, QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout,
                               QWidget)

from ..ui_kit import (CollapseToggleButton, CustomButton, CustomCheckBox, CustomGroupBox,
                      CustomLineEdit, CustomSpinBox, SmoothScrollArea, Theme, combo_box_stylesheet,
                      contrast_text, get_settings, rounded_rect_path)
from . import model, palette

MAX_ROWS = 300


def muted(color: QColor, alpha: float = 0.65) -> QColor:
    c = QColor(color)
    c.setAlphaF(alpha)
    return c


def _label(text: str = "", size_delta: float = 0, bold: bool = False, muted_: bool = False, wrap: bool = False,
           color: "QColor | None" = None) -> QLabel:
    lab = QLabel(text)
    f = QFont(lab.font())
    f.setPointSizeF(max(7.0, f.pointSizeF() + size_delta))
    f.setBold(bold)
    lab.setFont(f)
    lab.setWordWrap(wrap)
    if color is not None:
        lab.setStyleSheet(f"QLabel {{ color: {QColor(color).name()}; }}")
    elif muted_:
        t = Theme().text()
        lab.setStyleSheet(f"QLabel {{ color: rgba({t.red()}, {t.green()}, {t.blue()}, 160); }}")
    return lab


# ---------------------------------------------------------------------------- signal colours (see palette.py)
class _TintTheme(Theme):
    """The kit's theme with one change: accent() (outlines) comes from a callable, so a stock kit widget
    can be drawn in a signal colour without copying its paintEvent."""

    def __init__(self, tint):
        super().__init__()
        self._tint = tint

    def accent(self) -> QColor:
        c = self._tint()
        return c if c is not None else super().accent()


_tinted_cache: dict = {}


def tinted(pm: QPixmap, color: QColor) -> QPixmap:
    key = (pm.cacheKey(), color.rgb())
    out = _tinted_cache.get(key)
    if out is None:
        out = QPixmap(pm.size())
        out.setDevicePixelRatio(pm.devicePixelRatio())
        out.fill(Qt.transparent)
        q = QPainter(out)
        q.drawPixmap(0, 0, pm)
        q.setCompositionMode(QPainter.CompositionMode_SourceIn)
        q.fillRect(out.rect(), color)
        q.end()
        _tinted_cache[key] = out
    return out


class StateCheckBox(CustomCheckBox):
    """An on/off toggle in signal colours: green when on, red when off (``invert`` for 'block' boxes,
    where checked means the traffic is off).  ``on``/``off`` may also be None (plain) for one side."""

    def __init__(self, text: str = "", invert: bool = False, parent=None):
        super().__init__(text, parent)
        self.invert = invert
        self._theme = _TintTheme(self._tint)

    def _tint(self) -> "QColor | None":
        sig = palette.signals()
        good = self.isChecked() != self.invert
        return sig.on if good else sig.off

    def _icon_for_state(self):
        if not self.isChecked():
            return None
        return tinted(self._checkmark, self._tint())


class ClickLabel(QLabel):
    """A label that toggles its checkbox when clicked (so a coloured caption behaves like the box's own text)."""

    def __init__(self, text: str, box, parent=None):
        super().__init__(text, parent)
        self._box = box
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, e) -> None:
        self._box.toggle()


def coloured_toggle(text: str, color: QColor, invert: bool = False, bold: bool = False) -> "tuple[QHBoxLayout, StateCheckBox]":
    """[box] caption  -- the caption drawn in a signal colour."""
    row = QHBoxLayout()
    row.setSpacing(8)
    box = StateCheckBox("", invert=invert)
    row.addWidget(box)
    lab = ClickLabel(text, box)
    f = QFont(lab.font())
    f.setBold(bold)
    lab.setFont(f)
    lab.setStyleSheet(f"QLabel {{ color: {QColor(color).name()}; }}")
    row.addWidget(lab, stretch=1)
    return row, box


def tinted_group(title: str, color: "QColor | None") -> CustomGroupBox:
    gb = CustomGroupBox(title)
    if color is not None:
        gb._theme = _TintTheme(lambda c=QColor(color): c)
    return gb


DIRECTION_WORDS = {"both": "incoming and outgoing", "in": "incoming", "out": "outgoing"}


def avatar_letter(name: str) -> str:
    return (name[:1] or "?").upper()


class Avatar(QWidget):
    def __init__(self, letter: str, size: int = 34, parent=None):
        super().__init__(parent)
        self._letter, self._theme = letter, Theme()
        self.setFixedSize(size, size)

    def set_letter(self, letter: str) -> None:
        self._letter = letter
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fill = self._theme.highlight()
        p.setBrush(fill)
        p.setPen(Qt.NoPen)
        p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        f = QFont(self.font())
        f.setBold(True)
        f.setPixelSize(int(self.height() * 0.46))
        p.setFont(f)
        p.setPen(contrast_text(fill))
        p.drawText(self.rect(), Qt.AlignCenter, self._letter)


class Surface(QWidget):
    """A rounded panel in the surface colour (optionally highlighted)."""

    def __init__(self, parent=None, radius: float = 14):
        super().__init__(parent)
        self._theme = Theme()
        self._radius = radius
        self.highlighted = False
        self.hovered = False

    def lock_height(self) -> None:
        """Rows inside a scrolling panel must never be squeezed below their content (the panel scrolls instead)."""
        self.setMinimumHeight(self.sizeHint().height())

    def set_highlighted(self, on: bool) -> None:
        if on != self.highlighted:
            self.highlighted = on
            self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t = self._theme
        fill = QColor(t.surface())
        if self.highlighted:
            fill = t.highlight()
        elif self.hovered:
            fill = fill.lighter(112)
        p.fillPath(rounded_rect_path(QRectF(self.rect()), min(t.corner_radius(8), self._radius)), fill)


# ============================================================================== app list (left)
class StarButton(QAbstractButton):
    """A pin toggle drawn as a star (no icon font needed): solid when pinned, an outline otherwise."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(24, 24)
        self._theme = Theme()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(self._theme.text())
        c.setAlphaF(1.0 if self.isChecked() else (0.75 if self.underMouse() else 0.4))
        cx, cy, ro, ri = self.width() / 2, self.height() / 2 + 0.5, 9.0, 3.8
        pts = [QPointF(cx + (ro if i % 2 == 0 else ri) * math.sin(i * math.pi / 5),
                       cy - (ro if i % 2 == 0 else ri) * math.cos(i * math.pi / 5)) for i in range(10)]
        p.setPen(QPen(c, 1.4))
        p.setBrush(c if self.isChecked() else Qt.NoBrush)
        p.drawPolygon(QPolygonF(pts))

    def enterEvent(self, e) -> None:
        self.update()

    def leaveEvent(self, e) -> None:
        self.update()


class EyeButton(QAbstractButton):
    """The visibility toggle: an open eye = shown on the map; a crossed-out eye = hidden from it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)                     # checked = visible
        self.setChecked(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(24, 24)
        self._theme = Theme()
        self.toggled.connect(lambda on: self.setToolTip(
            "Shown on the map: click to hide it (for streaming)" if on else "Hidden from the map: click to show it"))
        self.setToolTip("Shown on the map: click to hide it (for streaming)")

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(self._theme.text())
        c.setAlphaF((0.9 if self.isChecked() else 0.55) if not self.underMouse() else 1.0)
        cx, cy = self.width() / 2, self.height() / 2
        w, h = 9.0, 5.5
        path = QPainterPath(QPointF(cx - w, cy))
        path.quadTo(QPointF(cx, cy - 2 * h), QPointF(cx + w, cy))
        path.quadTo(QPointF(cx, cy + 2 * h), QPointF(cx - w, cy))
        p.setPen(QPen(c, 1.4))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.setBrush(c)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, cy), 2.6, 2.6)
        if not self.isChecked():
            p.setPen(QPen(c, 1.8))
            p.drawLine(QPointF(cx - 8, cy + 7), QPointF(cx + 8, cy - 7))

    def enterEvent(self, e) -> None:
        self.update()

    def leaveEvent(self, e) -> None:
        self.update()


def dim(widget: QWidget, on: bool, level: float = 0.45) -> None:
    """Fade a whole row (hidden things stay listed, just quieter)."""
    if on:
        eff = QGraphicsOpacityEffect(widget)
        eff.setOpacity(level)
        widget.setGraphicsEffect(eff)
    else:
        widget.setGraphicsEffect(None)


class AppRow(Surface):
    clicked = Signal(str)
    allowToggled = Signal(str, bool)
    pinToggled = Signal(str, bool)
    visibilityToggled = Signal(str, bool)

    def __init__(self, app: "model.AppView", selected: bool, pinned: bool = False, hidden: bool = False):
        super().__init__(radius=16)
        self.identity = app.identity
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 12, 8)
        lay.setSpacing(10)
        lay.addWidget(Avatar(avatar_letter(app.name)))
        col = QVBoxLayout()
        col.setSpacing(0)
        self.name = _label(app.name, bold=True)
        bits = []
        if not app.running:
            bits.append("not running")
        else:
            bits.append(f"{app.active_count} open" if app.active_count else "idle")
        if app.blocked_count:
            bits.append(f"{app.blocked_count} blocked")
        if app.ask:
            bits.append("asks")
        self.sub = _label(" · ".join(bits), size_delta=-1.5, muted_=True)
        col.addWidget(self.name)
        col.addWidget(self.sub)
        lay.addLayout(col, stretch=1)
        self.eye = EyeButton()
        self.eye.setChecked(not hidden)
        self.eye.toggled.connect(lambda on: self.visibilityToggled.emit(self.identity, on))
        lay.addWidget(self.eye)
        self.hidden = hidden
        self.star = StarButton()
        self.star.setChecked(pinned)
        self.star.setToolTip("Unpin from the top of the list" if pinned else "Pin to the top of the list")
        self.star.toggled.connect(lambda on: self.pinToggled.emit(self.identity, on))
        lay.addWidget(self.star)
        self.allow = StateCheckBox("")
        self.allow.setChecked(app.allowed)
        self.allow.setToolTip("Allow this app's network traffic (off blocks both directions)")
        self.allow.toggled.connect(lambda on: self.allowToggled.emit(self.identity, on))
        lay.addWidget(self.allow)
        self.set_highlighted(selected)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self._apply_colors()
        if hidden:
            self.name.setText(app.name + "  (hidden)")
            dim(self, True, 0.55)

    def _apply_colors(self) -> None:
        # on the highlight fill, text must contrast with it (kit rule), not the normal text colour
        if self.highlighted:
            c = contrast_text(self._theme.highlight())
            self.name.setStyleSheet(f"QLabel {{ color: {c.name()}; }}")
            self.sub.setStyleSheet(f"QLabel {{ color: rgba({c.red()}, {c.green()}, {c.blue()}, 190); }}")
        else:
            t = self._theme.text()
            self.name.setStyleSheet(f"QLabel {{ color: {t.name()}; }}")
            self.sub.setStyleSheet(f"QLabel {{ color: rgba({t.red()}, {t.green()}, {t.blue()}, 160); }}")

    def set_highlighted(self, on: bool) -> None:
        super().set_highlighted(on)
        if hasattr(self, "name"):
            self._apply_colors()

    def enterEvent(self, e) -> None:
        self.hovered = True
        self.update()

    def leaveEvent(self, e) -> None:
        self.hovered = False
        self.update()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.identity)


class AppListPanel(QWidget):
    selected = Signal(str)
    allowToggled = Signal(str, bool)
    pinToggled = Signal(str, bool)
    visibilityToggled = Signal(str, bool)
    showHiddenToggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme = Theme()
        self._pinned: list = []
        self._hidden: list = []
        self._show_hidden = False
        self._apps: list = []
        self._selected: "str | None" = None
        self._rows: "dict[str, AppRow]" = {}
        self._sig = None
        outer = QVBoxLayout(self)
        pad = self._theme.padding
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(pad // 2)
        self.search = CustomLineEdit()
        self.search.setPlaceholderText("Search apps")
        self.search.textChanged.connect(lambda _: self._rebuild(force=True))
        outer.addWidget(self.search)
        self.hidden_btn = CustomButton("")
        self.hidden_btn.clicked.connect(lambda: self.showHiddenToggled.emit(not self._show_hidden))
        self.hidden_btn.hide()
        outer.addWidget(self.hidden_btn)
        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 0, 4, 0)
        self.body_lay.setSpacing(6)
        self.scroll.setWidget(self.body)
        self.body.setAutoFillBackground(False)               # kit pitfall 5
        self.scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(self.scroll, stretch=1)

    def set_pinned(self, pinned: list) -> None:
        self._pinned = list(pinned)

    def set_visibility(self, hidden: list, show_hidden: bool) -> None:
        self._hidden, self._show_hidden = list(hidden), bool(show_hidden)

    def set_apps(self, apps: list, selected: "str | None") -> None:
        self._apps, self._selected = apps, selected
        sig = (tuple((a.identity, a.running, a.allowed, a.ask, a.active_count, a.blocked_count) for a in apps),
               self.search.text(), tuple(self._pinned), tuple(self._hidden), self._show_hidden)
        if sig == self._sig:                                 # nothing visible changed: don't rebuild (kit pitfall 14)
            self.set_selected(selected)
            return
        self._sig = sig
        self._rebuild()

    def set_selected(self, identity: "str | None") -> None:
        self._selected = identity
        for ident, row in self._rows.items():
            row.set_highlighted(ident == identity)

    def _rebuild(self, force: bool = False) -> None:
        q = self.search.text().strip().lower()
        while self.body_lay.count():
            item = self.body_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._rows = {}
        matching = [a for a in self._apps if not q or q in a.name.lower() or q in a.identity.lower()]
        n_hidden = sum(1 for a in self._apps if a.identity in self._hidden)
        shown = [a for a in matching if self._show_hidden or a.identity not in self._hidden]
        self.hidden_btn.setVisible(n_hidden > 0)
        self.hidden_btn.setText(f"Hide the {n_hidden} hidden app{'s' if n_hidden != 1 else ''}" if self._show_hidden
                                else f"Show {n_hidden} hidden app{'s' if n_hidden != 1 else ''}")
        for a in shown:
            row = AppRow(a, a.identity == self._selected, a.identity in self._pinned, a.identity in self._hidden)
            row.visibilityToggled.connect(self.visibilityToggled)
            row.clicked.connect(self.selected)
            row.allowToggled.connect(self.allowToggled)
            row.pinToggled.connect(self.pinToggled)
            self._rows[a.identity] = row
            self.body_lay.addWidget(row)
        if not shown:
            msg = "No apps match." if q else "No apps yet. Start an app and it will appear here."
            self.body_lay.addWidget(_label(msg, muted_=True, wrap=True))
        self.body_lay.addStretch(1)
        self._sig = (self._sig[0], self.search.text(), tuple(self._pinned), tuple(self._hidden), self._show_hidden) if self._sig else None


# ============================================================================== connection rows (right)
class ConnectionRow(Surface):
    allowToggled = Signal(str, int, str, bool)           # ip, port (0 = whole address), proto, allowed
    visibilityToggled = Signal(str, bool)                # ip, visible on the map

    def __init__(self, r: "model.RemoteView", advanced: bool, focused: bool, hidden: bool = False):
        super().__init__(radius=12)
        self.ip = r.ip
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 6, 10, 6)
        v.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.dot = QWidget()
        self.dot.setFixedSize(10, 10)
        self._active = r.active
        self._dir = r.direction
        self.dot.paintEvent = self._paint_dot                                   # tiny custom indicator
        top.addWidget(self.dot)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(_label(r.title, bold=True))
        ports = ", ".join(f"{p['proto']}/{p['port']}" for p in r.ports[:4]) + (" …" if len(r.ports) > 4 else "")
        sub = r.where + (f" · {ports}" if ports else "")
        if r.temp_allowed:
            sub += " · allowed for now"
        col.addWidget(_label(sub, size_delta=-1.5, muted_=True))
        top.addLayout(col, stretch=1)
        self.toggle_btn = None
        if advanced and r.ports:
            self.toggle_btn = CollapseToggleButton(expanded=False)
            self.toggle_btn.setFixedSize(26, 26)
            self.toggle_btn.setToolTip("Per-port controls")
            top.addWidget(self.toggle_btn)
        self.eye = EyeButton()
        self.eye.setChecked(not hidden)
        self.eye.toggled.connect(lambda on: self.visibilityToggled.emit(r.ip, on))
        top.addWidget(self.eye)
        self.allow = StateCheckBox("")
        self.allow.setChecked(not r.blocked)
        self.allow.setToolTip("Allow this connection (off blocks that address)")
        self.allow.toggled.connect(lambda on: self.allowToggled.emit(r.ip, 0, "", on))
        top.addWidget(self.allow)
        v.addLayout(top)
        self.ports_box = None
        if self.toggle_btn is not None:
            self.ports_box = QWidget()
            pl = QVBoxLayout(self.ports_box)
            pl.setContentsMargins(20, 0, 0, 0)
            pl.setSpacing(2)
            for p in r.ports[:40]:
                row = QHBoxLayout()
                row.addWidget(_label(f"{p['proto']} {p['port']}", size_delta=-1))
                row.addWidget(_label(f"{p['count']}×" + ("  open" if p.get("active") else ""), size_delta=-2, muted_=True), stretch=1)
                cb = StateCheckBox("")
                cb.setChecked(p.get("rule") != "block")
                cb.toggled.connect(lambda on, pr=p: self.allowToggled.emit(r.ip, pr["port"], pr["proto"], on))
                row.addWidget(cb)
                pl.addLayout(row)
            self.ports_box.setVisible(False)
            v.addWidget(self.ports_box)
            self.toggle_btn.toggled.connect(self.ports_box.setVisible)
        self.set_highlighted(focused)
        self.lock_height()
        if hidden:
            dim(self, True, 0.55)

    def _paint_dot(self, e) -> None:
        p = QPainter(self.dot)
        p.setRenderHint(QPainter.Antialiasing)
        t = self._theme
        c = QColor(palette.signals().for_direction(self._dir))
        if not self._active:
            c.setAlphaF(0.35)
        p.setBrush(c)
        p.setPen(Qt.NoPen)
        p.drawEllipse(self.dot.rect().adjusted(1, 1, -1, -1))


class DetailPanel(QWidget):
    """Everything about the selected app: master switch, traffic rules, and its connections."""
    changeRequested = Signal(str, dict)                       # identity, {block_in: ..., ask: ...}
    remoteToggled = Signal(str, str, int, str, bool)          # identity, ip, port, proto, allowed
    portRequested = Signal(str, str, str, dict)               # identity, action (add|remove|enable|disable), port name, spec
    remoteVisibilityToggled = Signal(str, str, bool)          # identity, ip, visible on the map

    ASK_MODES = (("default", "Follow the global setting"), ("ask", "Always ask"), ("allow", "Always allow"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme = Theme()
        self.app: "model.AppView | None" = None
        self.advanced = False
        self.focus_ip: "str | None" = None
        self._sig = None
        self._draft = {"name": "", "port": 0, "proto": 0, "direction": 0}      # the 'add a port' form survives rebuilds
        self.hidden_remotes: set = set()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.lay = QVBoxLayout(self.body)
        self.lay.setContentsMargins(0, 0, 4, 0)
        self.lay.setSpacing(self._theme.padding)
        self.scroll.setWidget(self.body)
        self.body.setAutoFillBackground(False)
        self.scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(self.scroll)
        self._empty()

    def _clear(self) -> None:
        while self.lay.count():
            item = self.lay.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()

    def _empty(self) -> None:
        self._clear()
        self.lay.addWidget(_label("Select an app on the left, or click a pin on the map.", muted_=True, wrap=True))
        self.lay.addStretch(1)

    def show_app(self, app: "model.AppView | None", advanced: bool, focus_ip: "str | None") -> None:
        sig = None if app is None else (
            app.identity, app.running, app.allowed, app.ask, app.profile, advanced, focus_ip,
            tuple(sorted((app.settings or {}).items())),
            tuple((x["name"], x["port"], x["proto"], x["direction"], x["enabled"]) for x in app.ports),
            tuple(sorted(k for k in self.hidden_remotes if k.startswith(app.identity + "|"))),
            tuple((r.ip, r.direction, r.active, r.blocked, r.rule, r.temp_allowed, r.hostname,
                   tuple((p["proto"], p["port"], p.get("rule"), p.get("active")) for p in r.ports)) for r in app.remotes))
        if sig == self._sig:
            return
        self._sig = sig
        self.app, self.advanced, self.focus_ip = app, advanced, focus_ip
        if app is None:
            self._empty()
            return
        keep_scroll = self.scroll.verticalScrollBar().value()
        self._clear()
        self._build(app)
        self.scroll.verticalScrollBar().setValue(keep_scroll)

    def _build(self, app: "model.AppView") -> None:
        t = self._theme
        head = Surface(radius=16)
        h = QHBoxLayout(head)
        h.setContentsMargins(12, 10, 12, 10)
        h.addWidget(Avatar(avatar_letter(app.name), 44))
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(_label(app.name, size_delta=3, bold=True))
        col.addWidget(_label(app.identity + ("" if app.running else " · not running"), size_delta=-1.5, muted_=True))
        h.addLayout(col, stretch=1)
        allow = StateCheckBox("Allow")
        allow.setChecked(app.allowed)
        allow.setToolTip("Master switch: off blocks all traffic of this app, both directions")
        allow.toggled.connect(lambda on, i=app.identity: self.changeRequested.emit(
            i, {"block_in": not on, "block_out": not on}))
        h.addWidget(allow)
        self.lay.addWidget(head)

        s = app.settings or {}
        sig = palette.signals()
        rules = tinted_group("Traffic rules", None)
        rl = rules.make_layout(QVBoxLayout)
        for key, text, colour in (("block_out", "Block outgoing", sig.output), ("block_in", "Block incoming", sig.input)):
            row, cb = coloured_toggle(text, colour, invert=True)
            cb.setChecked(bool(s.get(key)))
            cb.toggled.connect(lambda on, k=key, i=app.identity: self.changeRequested.emit(i, {k: on}))
            rl.addLayout(row)
        for key, text, colour in (("delay_out_ms", "Fake latency, outgoing (ms)", sig.output),
                                  ("delay_in_ms", "Fake latency, incoming (ms)", sig.input)):
            row = QHBoxLayout()
            row.addWidget(_label(text, color=colour), stretch=1)
            spin = CustomSpinBox()
            spin.setRange(0, 5000)
            spin.setSingleStep(10)
            spin.setValue(int(s.get(key, 0)))
            spin.setFixedWidth(110)
            spin.editingFinished.connect(lambda sp=spin, k=key, i=app.identity: self.changeRequested.emit(i, {k: sp.value()}))
            row.addWidget(spin)
            rl.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(_label("New connections", color=sig.special), stretch=1)
        combo = QComboBox()
        combo.setStyleSheet(combo_box_stylesheet(get_settings()))
        for value, text in self.ASK_MODES:
            combo.addItem(text, value)
        combo.setCurrentIndex(max(0, [v for v, _ in self.ASK_MODES].index(s.get("ask", "default"))))
        combo.activated.connect(lambda idx, c=combo, i=app.identity: self.changeRequested.emit(i, {"ask": c.itemData(idx)}))
        row.addWidget(combo)
        rl.addLayout(row)
        self.lay.addWidget(rules)
        self.lay.addWidget(self._ports_box(app))

        for direction, title in (("out", "Outgoing connections"), ("in", "Incoming connections")):
            items = [r for r in app.remotes if r.direction == direction]
            box = tinted_group(f"{title} ({len(items)})", sig.for_direction(direction))
            bl = box.make_layout(QVBoxLayout)
            bl.setSpacing(6)
            if not items:
                bl.addWidget(_label("None seen yet." if direction == "out" else "None seen.", muted_=True))
            for r in items[:MAX_ROWS]:
                hidden = model.remote_key(app.identity, r.ip) in self.hidden_remotes
                row_w = ConnectionRow(r, self.advanced, r.ip == self.focus_ip, hidden)
                row_w.visibilityToggled.connect(lambda ip, on, i=app.identity: self.remoteVisibilityToggled.emit(i, ip, on))
                row_w.allowToggled.connect(lambda ip, port, proto, on, i=app.identity: self.remoteToggled.emit(i, ip, port, proto, on))
                bl.addWidget(row_w)
            if len(items) > MAX_ROWS:
                bl.addWidget(_label(f"… and {len(items) - MAX_ROWS} more", muted_=True))
            self.lay.addWidget(box)
        self.lay.addStretch(1)

    PROTOS = (("both", "TCP + UDP"), ("tcp", "TCP"), ("udp", "UDP"))
    DIRECTIONS = (("both", "In and out"), ("in", "Incoming"), ("out", "Outgoing"))

    def _ports_box(self, app: "model.AppView") -> CustomGroupBox:
        """Named ports: give a port number a name, then switch it on / off (off = blocked) for this app."""
        sig = palette.signals()
        box = tinted_group(f"Named ports ({len(app.ports)})", sig.special)
        bl = box.make_layout(QVBoxLayout)
        bl.setSpacing(6)
        if not app.ports:
            bl.addWidget(_label("No ports named yet. A port matches that number on either end of a connection.",
                                muted_=True, wrap=True))
        for x in app.ports:
            row_w = Surface(radius=12)
            h = QHBoxLayout(row_w)
            h.setContentsMargins(10, 6, 8, 6)
            h.setSpacing(8)
            col = QVBoxLayout()
            col.setSpacing(0)
            col.addWidget(_label(x["name"], bold=True))
            proto = {"both": "tcp+udp"}.get(x["proto"], x["proto"])
            col.addWidget(_label(f"{x['port']}/{proto} · {DIRECTION_WORDS[x['direction']]}", size_delta=-1.5,
                                 color=sig.for_direction(x["direction"])))
            h.addLayout(col, stretch=1)
            on = StateCheckBox("")
            on.setChecked(x["enabled"])
            on.setToolTip("Enabled: traffic on this port is allowed. Off: it is blocked.")
            on.toggled.connect(lambda v, n=x["name"], i=app.identity: self.portRequested.emit(i, "enable" if v else "disable", n, {}))
            h.addWidget(on)
            rm = CustomButton("✕")
            rm.setFixedWidth(34)
            rm.setToolTip("Forget this port")
            rm.clicked.connect(lambda _=False, n=x["name"], i=app.identity: self.portRequested.emit(i, "remove", n, {}))
            h.addWidget(rm)
            row_w.lock_height()
            row_w.setMinimumHeight(max(row_w.minimumHeight(), 48))
            bl.addWidget(row_w)

        bl.addWidget(_label("Add a port", muted_=True, size_delta=-1))
        top = QHBoxLayout()
        name = CustomLineEdit()
        name.setPlaceholderText("Name, e.g. voice")
        name.setMaxLength(32)
        name.setText(self._draft["name"])
        port = CustomSpinBox()
        port.setRange(0, 65535)
        port.setSpecialValueText("port")
        port.setValue(self._draft["port"])
        port.setFixedWidth(110)
        top.addWidget(name, stretch=1)
        top.addWidget(port)
        bl.addLayout(top)
        bottom = QHBoxLayout()
        proto, direction = QComboBox(), QComboBox()
        for combo, options, key in ((proto, self.PROTOS, "proto"), (direction, self.DIRECTIONS, "direction")):
            combo.setStyleSheet(combo_box_stylesheet(get_settings()))
            for value, text in options:
                combo.addItem(text, value)
            combo.setCurrentIndex(self._draft[key])
            bottom.addWidget(combo, stretch=1)
        add = CustomButton("Add")
        bottom.addWidget(add)
        bl.addLayout(bottom)

        def remember(*_):
            self._draft.update(name=name.text(), port=port.value(), proto=proto.currentIndex(), direction=direction.currentIndex())

        def submit():
            if not name.text().strip() or not port.value():
                return
            spec = {"name": name.text().strip(), "port": port.value(), "proto": proto.currentData(),
                    "direction": direction.currentData()}
            self._draft.update(name="", port=0)
            self.portRequested.emit(app.identity, "add", spec["name"], spec)

        name.textChanged.connect(remember)
        port.valueChanged.connect(remember)
        proto.currentIndexChanged.connect(remember)
        direction.currentIndexChanged.connect(remember)
        add.clicked.connect(submit)
        name.returnPressed.connect(submit)
        return box


# ============================================================================== banner + pending
class Banner(QWidget):
    """The strip above the map: service down / questions waiting."""
    reviewClicked = Signal()
    restartClicked = Signal()
    updateClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme = Theme()
        self._text = ""
        self._button_visible = False
        self._fill = palette.signals().special
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 6, 8, 6)
        self.label = _label("", bold=True)
        lay.addWidget(self.label, stretch=1)
        self.button = CustomButton("Review")
        self.button.clicked.connect(self.reviewClicked)
        lay.addWidget(self.button)
        self.restart = CustomButton("Restart service")
        self.restart.setToolTip("systemctl restart portcullis.service")
        self.restart.clicked.connect(self.restartClicked)
        self.restart.hide()
        lay.addWidget(self.restart)
        self.update_btn = CustomButton("Restart window")
        self.update_btn.setToolTip("Reopen Portcullis on the newly installed version")
        self.update_btn.clicked.connect(self.updateClicked)
        self.update_btn.hide()
        lay.addWidget(self.update_btn)
        self.setFixedHeight(46)
        self.hide()

    def show_state(self, text: str, button: bool = False, kind: str = "special", restart: bool = False,
                   update: bool = False) -> None:
        """kind: 'special' (purple: needs you) or 'off' (red: something is broken)."""
        sig = palette.signals()
        self._fill = sig.off if kind == "off" else sig.special
        self.label.setText(text)
        self.button.setVisible(button)
        self.restart.setVisible(restart)
        self.update_btn.setVisible(update)
        self.setVisible(bool(text))
        c = contrast_text(self._fill)
        self.label.setStyleSheet(f"QLabel {{ color: {c.name()}; }}")
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t = self._theme
        p.fillPath(rounded_rect_path(QRectF(self.rect()), min(t.corner_radius(8), self.height() / 2)), self._fill)


class PendingPanel(QWidget):
    """One card per question in ask mode, with the four choices."""
    decided = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme = Theme()
        self._sig = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.body = QWidget()
        self.lay = QVBoxLayout(self.body)
        self.lay.setSpacing(self._theme.padding)
        self.scroll.setWidget(self.body)
        self.body.setAutoFillBackground(False)
        self.scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(self.scroll)

    def set_pending(self, pending: list, label_for=lambda ip: "") -> None:
        sig = tuple((a["id"], a["seconds_left"] // 5) for a in pending)
        if sig == self._sig:
            return
        self._sig = sig
        while self.lay.count():
            item = self.lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        from .notifier import ACTIONS, describe
        sig = palette.signals()
        if not pending:
            self.lay.addWidget(_label("Nothing is waiting for you.", muted_=True))
        for a in pending:
            card = Surface(radius=16)
            v = QVBoxLayout(card)
            v.setContentsMargins(14, 10, 14, 12)
            title, body = describe(a, label_for(a["ip"]))
            v.addWidget(_label(title, bold=True))
            v.addWidget(_label(f"{body}  ·  {int(a['seconds_left'])}s left", size_delta=-1.5, muted_=True, wrap=True))
            row = QHBoxLayout()
            for key, text in ACTIONS:
                b = CustomButton(text)
                tone = {"allow_always": sig.on, "allow_temp": sig.special, "block_always": sig.off}.get(key)
                if tone is not None:
                    b.set_fill_color(tone)
                b.clicked.connect(lambda _=False, i=a["id"], k=key: self.decided.emit(i, k))
                row.addWidget(b)
            v.addLayout(row)
            self.lay.addWidget(card)
        self.lay.addStretch(1)
