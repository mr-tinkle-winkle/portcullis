"""The main window: sidebar (Map / Settings) -> pages.  The Map page is the app list, the world map
and the connection panel of the selected app, with a banner for the service state and for questions
waiting in ask mode."""
from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (QApplication, QButtonGroup, QDialog, QHBoxLayout, QMainWindow, QSizePolicy,
                               QStackedWidget, QVBoxLayout, QWidget)

from .. import geo as geomod
from ..ui_kit import SegmentButton, Theme, compute_scale, crossfade_to_index, show_message
from . import config as guicfg
from . import model
from .bridge import Bridge
from .mapview import MapWidget
from .notifier import Notifier
from .pages import Page, SettingsPage
from .resolver import Resolver
from .widgets import AppListPanel, Banner, DetailPanel, PendingPanel


class PendingDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Waiting for your decision")
        self.resize(560, 460)
        t = Theme()
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), t.page_background())
        for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
            pal.setColor(role, t.text())
        self.setPalette(pal)
        lay = QVBoxLayout(self)
        self.panel = PendingPanel()
        lay.addWidget(self.panel)


class MainWindow(QMainWindow):
    def __init__(self, bridge: "Bridge | None" = None, geo: "geomod.Geo | None" = None, cfg=None,
                 notifier: "Notifier | None" = None, resolver: "Resolver | None" = None, poll_ms: int = 1000,
                 downloader=None):
        super().__init__()
        self.setWindowTitle("Portcullis")
        self.cfg = cfg or guicfg.load()
        self.geo = geo or geomod.Geo()
        self.bridge = bridge or Bridge()
        self.resolver = resolver or Resolver()
        self.notifier = notifier
        self._download = downloader or geomod.update
        self.apps: "list[model.AppView]" = []
        self.pending: list = []
        self.selected: "str | None" = None
        self.focus_ip: "str | None" = None
        self._sig = None
        self._down_reason = ""
        self.keep_running = False                 # True in tray/agent mode: closing the window only hides it
        theme = Theme()
        self.resize(self.cfg.win_w, self.cfg.win_h)

        central = QWidget()
        self.setCentralWidget(central)
        central.setAutoFillBackground(True)                      # palette, never an unscoped stylesheet (pitfall 2)
        pal = central.palette()
        pal.setColor(central.backgroundRole(), theme.app_background())
        for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
            pal.setColor(role, theme.text())                     # plain QLabels need this (pitfall 3)
        central.setPalette(pal)
        row = QHBoxLayout(central)
        pad = theme.padding
        row.setContentsMargins(pad, pad, pad, pad)
        row.setSpacing(pad)

        self.sidebar = QWidget()
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(pad)
        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        self.stack = QStackedWidget()

        # ---- map page
        self.map_page = Page(scrollable=False)
        self.banner = Banner()
        self.map_page.body_layout.addWidget(self.banner)
        cols = QHBoxLayout()
        cols.setSpacing(pad)
        self.applist = AppListPanel()
        self.map = MapWidget()
        self.detail = DetailPanel()
        self.applist.setMinimumWidth(200)
        self.detail.setMinimumWidth(280)
        cols.addWidget(self.applist, 22)
        cols.addWidget(self.map, 50)
        cols.addWidget(self.detail, 28)
        self.map_page.body_layout.addLayout(cols, stretch=1)
        self.map.set_me(self.cfg.my_lat, self.cfg.my_lon)

        # ---- settings page
        self.settings_page = SettingsPage(self.cfg)

        for i, (label, page, stretch) in enumerate([("Map", self.map_page, 1), ("Settings", self.settings_page, 0)]):
            btn = SegmentButton(text=label, position="full")
            btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding if stretch else QSizePolicy.Fixed)
            side.addWidget(btn, stretch=stretch)
            self.nav.addButton(btn, i)
            self.stack.addWidget(page)
        self.nav.button(0).setChecked(True)
        self.nav.idClicked.connect(lambda i: crossfade_to_index(self.stack, i))
        row.addWidget(self.sidebar)
        row.addWidget(self.stack, stretch=1)

        self.pending_dialog = PendingDialog(self)

        # ---- wiring
        self.applist.selected.connect(self.select_app)
        self.applist.allowToggled.connect(self.on_app_allow)
        self.applist.pinToggled.connect(self.on_pin_toggled)
        self.map.appSelected.connect(self.on_pin_selected)
        self.map.locationChanged.connect(self.on_location)
        self.detail.changeRequested.connect(self.on_change)
        self.detail.remoteToggled.connect(self.on_remote)
        self.detail.portRequested.connect(self.on_port)
        self.banner.reviewClicked.connect(self.show_pending)
        self.pending_dialog.panel.decided.connect(self.decide)
        s = self.settings_page
        s.daemonSettingChanged.connect(lambda ch: self.bridge.send({"cmd": "settings", "changes": ch}, "settings"))
        s.guiSettingChanged.connect(self.on_gui_setting)
        s.locationApplied.connect(self.on_location)
        s.updateGeoClicked.connect(self.update_geo)
        s.importGeoClicked.connect(self.choose_geo_file)
        s.themeSaved.connect(lambda: guicfg.save(self.cfg))
        self.bridge.overview.connect(self.on_overview)
        self.bridge.down.connect(self.on_down)
        self.bridge.replied.connect(self.on_replied)
        if self.notifier is not None:
            self.notifier.decided.connect(self.decide)
        s.set_geo_status(self.geo.available, geomod.age_days(self.geo.path))
        self._update_hint()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.bridge.poll)
        self.timer.start(poll_ms)
        QTimer.singleShot(0, self.bridge.poll)

    # ---- layout ---------------------------------------------------------------------------------------------------
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.sidebar.setFixedWidth(max(64, min(120, round(self.width() * 0.06))))
        _ = compute_scale(self.width(), self.height())

    def closeEvent(self, event) -> None:
        self.cfg.win_w, self.cfg.win_h = self.width(), self.height()
        if self.keep_running:
            event.ignore()
            self.hide()
            return
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self) -> None:
        self.timer.stop()
        self.bridge.close()
        self.resolver.close()

    def present(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ---- data -----------------------------------------------------------------------------------------------------
    def label_for(self, ip: str) -> str:
        p = self.geo.lookup(ip)
        return p.label if p else ""

    def on_overview(self, ov: dict) -> None:
        self._down_reason = ""
        self._last_ov = ov
        self.pending = ov.get("pending", [])
        if self.cfg.resolve_hostnames:
            ips = [r["ip"] for a in ov.get("apps", []) for r in a.get("remotes", []) if not model.is_local_ip(r["ip"])]
            self.resolver.want(ips)
        names = self.resolver.names if self.cfg.resolve_hostnames else {}
        self.apps = model.build_apps(ov, self.geo.lookup, names, self.cfg.pinned)
        self.settings_page.load_daemon_settings(ov.get("settings", {}))
        if self.notifier is not None:
            self.notifier.sync(self.pending, self.cfg.notifications)
        sig = (model.signature(self.apps, self.pending), self.selected, self.focus_ip, self.cfg.advanced_ports,
               tuple(self.cfg.__dict__.get(k) for k in ("my_lat", "my_lon")), tuple(sorted(ov.get("flow_errors", {}))), ov.get("error", ""))
        self._refresh_banner(ov)
        self.pending_dialog.panel.set_pending(self.pending, self.label_for)
        if sig == self._sig:
            return
        self._sig = sig
        self.applist.set_pinned(self.cfg.pinned)
        self.applist.set_apps(self.apps, self.selected)
        self.map.set_data(model.build_pins(self.apps), model.local_items(self.apps), self.selected)
        self.detail.show_app(self.app(self.selected), self.cfg.advanced_ports, self.focus_ip)
        self._update_hint()

    def on_down(self, reason: str) -> None:
        self._down_reason = reason
        self.banner.show_state(reason, kind="off")

    def _refresh_banner(self, ov: dict) -> None:
        n = len(self.pending)
        if n:
            self.banner.show_state(f"{n} connection{'s' if n != 1 else ''} waiting for your decision", button=True)
        elif ov.get("error"):
            self.banner.show_state("The firewall rules couldn't be applied: " + ov["error"].splitlines()[0], kind="off")
        elif ov.get("flow_errors"):
            self.banner.show_state("Connection tracking isn't working: " + next(iter(ov["flow_errors"].values())), kind="off")
        else:
            self.banner.show_state("")

    def _update_hint(self) -> None:
        if not self.geo.available:
            self.map.set_hint("No location data yet: Settings → Download / update location data")
        elif (self.cfg.my_lat, self.cfg.my_lon) == (guicfg.DEFAULT_LAT, guicfg.DEFAULT_LON):
            self.map.set_hint('Drag "My Location" to where you want to appear')
        else:
            self.map.set_hint("")

    def app(self, identity: "str | None"):
        return next((a for a in self.apps if a.identity == identity), None) if identity else None

    # ---- selection ------------------------------------------------------------------------------------------------
    def select_app(self, identity: str) -> None:
        self.selected, self.focus_ip = identity, None
        self._sig = None
        self.applist.set_selected(identity)
        self.on_overview_refresh()

    def on_pin_selected(self, identity: str, ip: str) -> None:
        self.selected, self.focus_ip = identity, ip
        self._sig = None
        self.on_overview_refresh()

    def on_overview_refresh(self) -> None:
        self.applist.set_apps(self.apps, self.selected)
        self.map.set_data(model.build_pins(self.apps), model.local_items(self.apps), self.selected)
        self.detail.show_app(self.app(self.selected), self.cfg.advanced_ports, self.focus_ip)

    # ---- actions → daemon -----------------------------------------------------------------------------------------
    def on_app_allow(self, identity: str, allowed: bool) -> None:
        self.bridge.send({"cmd": "app", "identity": identity, "changes": {"block_in": not allowed, "block_out": not allowed}}, "app")

    def on_change(self, identity: str, changes: dict) -> None:
        self.bridge.send({"cmd": "app", "identity": identity, "changes": changes}, "app")

    def on_remote(self, identity: str, ip: str, port: int, proto: str, allowed: bool) -> None:
        app = self.app(identity)
        if allowed:
            verdict = "allow" if (app and app.ask) else "clear"      # in ask mode an explicit allow stops further questions
        else:
            verdict = "block"
        cmd = {"cmd": "rule", "identity": identity, "ip": ip, "verdict": verdict}
        if port:
            cmd.update(port=port, proto=proto)
        self.bridge.send(cmd, "rule")

    def on_port(self, identity: str, action: str, name: str, spec: dict) -> None:
        cmd = {"cmd": "port", "identity": identity, "action": action, "port_name": name}
        if action == "add":
            cmd["spec"] = spec
        self.bridge.send(cmd, "port")

    def decide(self, ask_id: int, decision: str) -> None:
        self.bridge.send({"cmd": "answer", "id": ask_id, "decision": decision}, "answer")

    def on_replied(self, token: str, reply: dict) -> None:
        if not reply.get("ok"):
            show_message(self, "Portcullis", reply.get("error", "That didn't work."))

    def show_pending(self) -> None:
        self.pending_dialog.panel.set_pending(self.pending, self.label_for)
        self.pending_dialog.show()
        self.pending_dialog.raise_()

    def on_location(self, lat: float, lon: float) -> None:
        self.cfg.my_lat, self.cfg.my_lon = lat, lon
        self.cfg.sanitize()
        guicfg.save(self.cfg)
        self.map.set_me(self.cfg.my_lat, self.cfg.my_lon)
        self.settings_page.set_location(self.cfg.my_lat, self.cfg.my_lon)
        self._update_hint()

    def on_pin_toggled(self, identity: str, pinned: bool) -> None:
        if pinned and identity not in self.cfg.pinned:
            self.cfg.pinned.append(identity)
        elif not pinned and identity in self.cfg.pinned:
            self.cfg.pinned.remove(identity)
        guicfg.save(self.cfg)
        self._sig = None
        if getattr(self, "_last_ov", None) is not None:
            self.on_overview(self._last_ov)          # re-sorts: pinned apps first
        else:
            self.on_overview_refresh()

    def on_gui_setting(self) -> None:
        guicfg.save(self.cfg)
        self._sig = None
        self.on_overview_refresh()

    # ---- location database ----------------------------------------------------------------------------------------
    def choose_geo_file(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(self, "Location database", "", "DB-IP City Lite (*.mmdb *.mmdb.gz);;All files (*)")
        if path:
            self.import_geo(path)

    def import_geo(self, path: str) -> None:
        self.update_geo(lambda: geomod.install_file(path), busy="Installing…")

    def update_geo(self, job=None, busy: str = "Downloading…") -> None:
        from concurrent.futures import ThreadPoolExecutor
        self.settings_page.set_geo_status(self.geo.available, None, busy=busy)
        if not hasattr(self, "_geo_pool"):
            self._geo_pool = ThreadPoolExecutor(max_workers=1)
        fut = self._geo_pool.submit(job or self._download)

        def poll():
            if not fut.done():
                QTimer.singleShot(300, poll)
                return
            try:
                fut.result()
                self.geo.reload()
                self._sig = None
                self.settings_page.set_geo_status(self.geo.available, geomod.age_days(self.geo.path))
                self._update_hint()
                self.on_overview_refresh()
            except Exception as e:  # noqa: BLE001
                self.settings_page.set_geo_status(self.geo.available, geomod.age_days(self.geo.path))
                show_message(self, "Location data", str(e))
        QTimer.singleShot(300, poll)
