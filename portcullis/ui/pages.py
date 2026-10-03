"""Page chrome (kit rules: every page scrolls, paints its own background and outline) and the Settings page."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QColorDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .. import geo
from ..ui_kit import (CustomButton, CustomCheckBox, CustomDoubleSpinBox, CustomGroupBox, CustomSpinBox,
                      CustomLineEdit, SmoothScrollArea, Theme, ThemeEditorGroup, paint_page_outline, show_message)
from . import config as guicfg, palette
from .widgets import StateCheckBox


class Page(QWidget):
    """A top-level page: own background, a 3px outline 15% darker, scrollable (kit guide, sections 3 and 7)."""

    def __init__(self, scrollable: bool = True):
        super().__init__()
        self._theme = Theme()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(3, 3, 3, 3)
        pad = self._theme.padding
        if scrollable:
            scroll = SmoothScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.body = QWidget()
            self.body_layout = QVBoxLayout(self.body)
            self.body_layout.setContentsMargins(pad, pad, pad, pad)
            self.body_layout.setSpacing(pad)
            scroll.setWidget(self.body)
            self.body.setAutoFillBackground(False)              # setWidget() turned it on (pitfall 5)
            scroll.viewport().setAutoFillBackground(False)
            outer.addWidget(scroll)
        else:
            self.body = None
            self.body_layout = QVBoxLayout()
            self.body_layout.setContentsMargins(pad, pad, pad, pad)
            self.body_layout.setSpacing(pad)
            outer.addLayout(self.body_layout)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), self._theme.page_background())
        p.end()
        paint_page_outline(self, self._theme.page_background())


def _row(text: str, widget) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    lab = QLabel(text)
    lab.setWordWrap(True)
    h.addWidget(lab, stretch=1)
    h.addWidget(widget)
    return w


class SettingsPage(Page):
    daemonSettingChanged = Signal(dict)       # -> {"cmd": "settings", "changes": ...}
    guiSettingChanged = Signal()              # the window re-reads self.cfg
    locationApplied = Signal(float, float)
    updateGeoClicked = Signal()
    themeSaved = Signal()

    def __init__(self, cfg: "guicfg.GuiConfig"):
        super().__init__()
        self.cfg = cfg
        self._loading = True
        b = self.body_layout
        title = QLabel("Settings")
        title.setStyleSheet(f"QLabel {{ color: {self._theme.text().name()}; font-size: 18px; font-weight: bold; }}")
        b.addWidget(title)

        ask = CustomGroupBox("New connections")
        a = ask.make_layout(QVBoxLayout)
        self.ask_default = StateCheckBox("Never allow by default: ask about every new connection")
        self.ask_default.setToolTip("Every new connection of every app waits until you choose. Each app can override this.")
        self.ask_per_port = StateCheckBox("Ask per port (instead of once per address)")
        self.notifications = StateCheckBox("Show a desktop notification for each question")
        for w in (self.ask_default, self.ask_per_port, self.notifications):
            a.addWidget(w)
        self.temp_minutes = CustomSpinBox()
        self.temp_minutes.setRange(1, 1440)
        self.hold_seconds = CustomSpinBox()
        self.hold_seconds.setRange(3, 120)
        self.quiet_seconds = CustomSpinBox()
        self.quiet_seconds.setRange(0, 300)
        for text, spin in (("'Allow temporarily' lasts (minutes)", self.temp_minutes),
                           ("A question waits this long before the connection is dropped (seconds)", self.hold_seconds),
                           ("After 'Ignore', stay quiet about retries for (seconds)", self.quiet_seconds)):
            spin.setFixedWidth(110)
            a.addWidget(_row(text, spin))
        b.addWidget(ask)

        conns = CustomGroupBox("Connections")
        c = conns.make_layout(QVBoxLayout)
        self.track = StateCheckBox("Track connections (needed for the map and the lists)")
        self.advanced = StateCheckBox("Advanced: show per-port controls")
        self.resolve = StateCheckBox("Look up host names (asks your DNS resolver about each address)")
        for w in (self.track, self.advanced, self.resolve):
            c.addWidget(w)
        b.addWidget(conns)

        loc = CustomGroupBox("My location on the map")
        l = loc.make_layout(QVBoxLayout)
        hint = QLabel("Drag the \"My Location\" pin on the map, or type coordinates. It's only a drawing: "
                      "nothing is sent anywhere, so you can put it wherever you like when streaming.")
        hint.setWordWrap(True)
        l.addWidget(hint)
        self.lat = CustomDoubleSpinBox()
        self.lat.setRange(-85, 85)
        self.lat.setDecimals(3)
        self.lon = CustomDoubleSpinBox()
        self.lon.setRange(-180, 180)
        self.lon.setDecimals(3)
        l.addWidget(_row("Latitude", self.lat))
        l.addWidget(_row("Longitude", self.lon))
        apply_btn = CustomButton("Move pin")
        apply_btn.clicked.connect(lambda: self.locationApplied.emit(self.lat.value(), self.lon.value()))
        l.addWidget(apply_btn, alignment=Qt.AlignRight)
        b.addWidget(loc)

        db = CustomGroupBox("Location data")
        d = db.make_layout(QVBoxLayout)
        self.geo_status = QLabel()
        self.geo_status.setWordWrap(True)
        d.addWidget(self.geo_status)
        self.geo_button = CustomButton("Download / update location data")
        self.geo_button.clicked.connect(self.updateGeoClicked)
        d.addWidget(self.geo_button, alignment=Qt.AlignRight)
        credit = QLabel(geo.ATTRIBUTION + ". Looked up offline: no address ever leaves your machine.")
        credit.setWordWrap(True)
        d.addWidget(credit)
        b.addWidget(db)

        sigs = CustomGroupBox("Signal colors")
        sg = sigs.make_layout(QVBoxLayout)
        note = QLabel("Everything else stays black and white. These mark direction and state, as in Puppetry.")
        note.setWordWrap(True)
        sg.addWidget(note)
        self.signal_edits: dict = {}
        for key, text in palette.LABELS.items():
            row = QHBoxLayout()
            row.addWidget(QLabel(text), stretch=1)
            edit = CustomLineEdit()
            edit.setMaxLength(7)
            edit.setText(getattr(cfg, key))
            edit.setFixedWidth(110)
            pick = CustomButton("Pick...")
            pick.setFixedWidth(80)
            pick.clicked.connect(lambda _=False, e=edit: self._pick(e))
            row.addWidget(edit)
            row.addWidget(pick)
            self.signal_edits[key] = edit
            sg.addLayout(row)
        reset = CustomButton("Revert signal colors")
        reset.clicked.connect(lambda: [self.signal_edits[k].setText(v) for k, v in palette.DEFAULTS.items()])
        sg.addWidget(reset, alignment=Qt.AlignRight)
        b.addWidget(sigs)

        self.editor = ThemeEditorGroup(current=cfg.theme, defaults=guicfg.APP_THEME_DEFAULTS)
        b.addWidget(self.editor)
        save = CustomButton("Save appearance")
        save.clicked.connect(self._save_theme)
        b.addWidget(save, alignment=Qt.AlignRight)
        b.addStretch(1)

        self.ask_default.toggled.connect(lambda on: self._daemon({"ask_default": on}))
        self.ask_per_port.toggled.connect(lambda on: self._daemon({"ask_per_port": on}))
        self.track.toggled.connect(lambda on: self._daemon({"track_flows": on}))
        for key, spin in (("temp_allow_minutes", self.temp_minutes), ("hold_seconds", self.hold_seconds),
                          ("quiet_seconds", self.quiet_seconds)):
            spin.editingFinished.connect(lambda k=key, s=spin: self._daemon({k: s.value()}))
        self.notifications.toggled.connect(lambda on: self._gui("notifications", on))
        self.advanced.toggled.connect(lambda on: self._gui("advanced_ports", on))
        self.resolve.toggled.connect(lambda on: self._gui("resolve_hostnames", on))
        self.notifications.setChecked(cfg.notifications)
        self.advanced.setChecked(cfg.advanced_ports)
        self.resolve.setChecked(cfg.resolve_hostnames)
        self.set_location(cfg.my_lat, cfg.my_lon)
        self._loading = False

    # -- helpers ----------------------------------------------------------------------------------------------
    def _daemon(self, changes: dict) -> None:
        if not self._loading:
            self.daemonSettingChanged.emit(changes)

    def _gui(self, key: str, value) -> None:
        if not self._loading:
            setattr(self.cfg, key, value)
            self.guiSettingChanged.emit()

    def load_daemon_settings(self, s: dict) -> None:
        """Show the daemon's current settings (without echoing them back as changes)."""
        self._loading = True
        try:
            for box, key in ((self.ask_default, "ask_default"), (self.ask_per_port, "ask_per_port"), (self.track, "track_flows")):
                if box.isChecked() != s.get(key, box.isChecked()):
                    box.setChecked(bool(s[key]))
            for spin, key in ((self.temp_minutes, "temp_allow_minutes"), (self.hold_seconds, "hold_seconds"),
                              (self.quiet_seconds, "quiet_seconds")):
                if not spin.hasFocus() and key in s and spin.value() != s[key]:
                    spin.setValue(int(s[key]))
        finally:
            self._loading = False

    def set_location(self, lat: float, lon: float) -> None:
        was, self._loading = self._loading, True
        self.lat.setValue(lat)
        self.lon.setValue(lon)
        self._loading = was

    def set_geo_status(self, available: bool, age_days: "int | None", busy: str = "") -> None:
        if busy:
            self.geo_status.setText(busy)
        elif available:
            self.geo_status.setText("Location data installed" + (f" ({age_days} days old)." if age_days is not None else "."))
        else:
            self.geo_status.setText("No location data yet: the map can't place any connection. Download it once.")
        self.geo_button.setEnabled(not busy)

    def _pick(self, edit) -> None:
        c = QColorDialog.getColor(QColor(edit.text()) if palette.valid(edit.text()) else QColor("#808080"), self)
        if c.isValid():
            edit.setText(c.name())

    def _save_theme(self) -> None:
        bad = self.editor.apply_to(self.cfg.theme)
        for key, edit in self.signal_edits.items():
            if palette.valid(edit.text().strip()):
                setattr(self.cfg, key, edit.text().strip().lower())
            else:
                bad.append(key)
        self.themeSaved.emit()
        msg = "Saved. Reopen Portcullis to apply the new look everywhere."
        if bad:
            msg += "\n\nIgnored invalid colours: " + ", ".join(bad)
        show_message(self, "Appearance", msg)
