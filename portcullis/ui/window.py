"""The main window: sidebar (Map / Settings) -> pages.  The Map page is the app list, the world map
and the connection panel of the selected app, with a banner for the service state and for questions
waiting in ask mode."""
from __future__ import annotations

from PySide6.QtCore import QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (QApplication, QButtonGroup, QDialog, QHBoxLayout, QMainWindow, QSizePolicy, QSplitter,
                               QSplitterHandle, QStackedWidget, QVBoxLayout, QWidget)

from .. import geo as geomod
from ..ui_kit import SegmentButton, Theme, compute_scale, crossfade_to_index, rounded_rect_path, show_message
from . import config as guicfg
from . import model
from .bridge import Bridge
from .mapview import MapWidget
from .notifier import Notifier
from .pages import Page, SettingsPage
from .resolver import Resolver
from .service import ServiceControl
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


class _Handle(QSplitterHandle):
    """A splitter handle you can see: a short rounded grip, brighter under the mouse."""

    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self.setAttribute(Qt.WA_Hover, True)
        self._theme = Theme()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(self._theme.text())
        c.setAlphaF(0.55 if self.underMouse() else 0.18)
        h = min(64.0, self.height() * 0.2)
        r = QRectF(self.width() / 2 - 2, self.height() / 2 - h / 2, 4, h)
        p.fillPath(rounded_rect_path(r, 2), c)


class Splitter(QSplitter):
    """App list | map | connection panel, each resizable by dragging the gaps between them."""

    def createHandle(self) -> QSplitterHandle:
        return _Handle(self.orientation(), self)


class MainWindow(QMainWindow):
    def __init__(self, bridge: "Bridge | None" = None, geo: "geomod.Geo | None" = None, cfg=None,
                 notifier: "Notifier | None" = None, resolver: "Resolver | None" = None, poll_ms: int = 1000,
                 downloader=None, service: "ServiceControl | None" = None):
        super().__init__()
        self.setWindowTitle("Portcullis")
        self.cfg = cfg or guicfg.load()
        self.geo = geo or geomod.Geo()
        self.bridge = bridge or Bridge()
        self.resolver = resolver or Resolver()
        self.notifier = notifier
        self._download = downloader or geomod.update
        self.service = service or ServiceControl(self)
        self.service.finished.connect(self.on_service_restarted)
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
        self.split = Splitter(Qt.Horizontal)
        self.split.setHandleWidth(pad)
        self.split.setChildrenCollapsible(False)
        self.applist = AppListPanel()
        self.map = MapWidget()
        self.detail = DetailPanel()
        self.applist.setMinimumWidth(250)
        self.map.setMinimumWidth(280)
        self.detail.setMinimumWidth(260)
        for i, (w, stretch) in enumerate(((self.applist, 22), (self.map, 50), (self.detail, 28))):
            self.split.addWidget(w)
            self.split.setStretchFactor(i, stretch)
        if self.cfg.split_sizes:
            self.split.setSizes(self.cfg.split_sizes)
        else:
            self.split.setSizes([300, 700, 400])
        self.split.splitterMoved.connect(lambda *_: self._remember_split())
        self.map_page.body_layout.addWidget(self.split, stretch=1)
        self.map.set_me(self.cfg.my_lat, self.cfg.my_lon)
        self.map.set_mode(self.cfg.map_mode)

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
        self.applist.visibilityToggled.connect(self.on_app_visibility)
        self.applist.showHiddenToggled.connect(self.on_show_hidden)
        self.detail.remoteVisibilityToggled.connect(self.on_remote_visibility)
        self.map.modeChanged.connect(self.on_map_mode)
        self.map.appSelected.connect(self.on_pin_selected)
        self.map.locationChanged.connect(self.on_location)
        self.detail.changeRequested.connect(self.on_change)
        self.detail.remoteToggled.connect(self.on_remote)
        self.detail.portRequested.connect(self.on_port)
        self.banner.reviewClicked.connect(self.show_pending)
        self.banner.restartClicked.connect(self.restart_service)
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
               tuple(self.cfg.hidden_apps), tuple(self.cfg.hidden_remotes), self.cfg.show_hidden,
               tuple(self.cfg.__dict__.get(k) for k in ("my_lat", "my_lon")), tuple(sorted(ov.get("flow_errors", {}))), ov.get("error", ""))
        self._refresh_banner(ov)
        self.pending_dialog.panel.set_pending(self.pending, self.label_for)
        if sig == self._sig:
            return
        self._sig = sig
        self._push()
        self._update_hint()

    def on_down(self, reason: str) -> None:
        self._down_reason = reason
        if self.service.busy:
            return
        self.banner.show_state(reason, kind="off", restart=True)

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
        self._push()

    def _push(self) -> None:
        """Hand the current apps to the list, the map (minus anything hidden) and the connection panel."""
        cfg = self.cfg
        self.applist.set_pinned(cfg.pinned)
        self.applist.set_visibility(cfg.hidden_apps, cfg.show_hidden)
        self.applist.set_apps(self.apps, self.selected)
        shown = model.for_map(self.apps, cfg.hidden_apps, cfg.hidden_remotes)
        self.map.set_data(model.build_pins(shown), model.local_items(shown), self.selected)
        self.detail.hidden_remotes = set(cfg.hidden_remotes)
        self.detail.show_app(self.app(self.selected), cfg.advanced_ports, self.focus_ip)

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

    def restart_service(self) -> None:
        self.banner.show_state("Restarting the portcullis service…", kind="special")
        self.service.restart()

    def on_service_restarted(self, ok: bool, message: str) -> None:
        if ok:
            QTimer.singleShot(800, self.bridge.poll)                 # the socket needs a moment to appear
        else:
            self.banner.show_state(self._down_reason or "The service isn't running", kind="off", restart=True)
            show_message(self, "Restart service", "Couldn't restart the service:\n\n" + message)

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

    # ---- visibility, map mode, layout ------------------------------------------------------------------------------
    @staticmethod
    def _toggle(items: list, key: str, present: bool) -> None:
        if present and key not in items:
            items.append(key)
        elif not present and key in items:
            items.remove(key)

    def on_app_visibility(self, identity: str, visible: bool) -> None:
        self._toggle(self.cfg.hidden_apps, identity, not visible)
        guicfg.save(self.cfg)
        self._sig = None
        self._push()

    def on_remote_visibility(self, identity: str, ip: str, visible: bool) -> None:
        self._toggle(self.cfg.hidden_remotes, model.remote_key(identity, ip), not visible)
        guicfg.save(self.cfg)
        self._sig = None
        self._push()

    def on_show_hidden(self, on: bool) -> None:
        self.cfg.show_hidden = on
        self.settings_page.sync_gui(self.cfg)
        guicfg.save(self.cfg)
        self._push()

    def on_map_mode(self, mode: str) -> None:
        self.cfg.map_mode = mode
        self.settings_page.sync_gui(self.cfg)
        guicfg.save(self.cfg)

    def _remember_split(self) -> None:
        self.cfg.split_sizes = self.split.sizes()
        if not hasattr(self, "_split_timer"):                    # save once the drag settles, not on every pixel
            self._split_timer = QTimer(self)
            self._split_timer.setSingleShot(True)
            self._split_timer.timeout.connect(lambda: guicfg.save(self.cfg))
        self._split_timer.start(400)

    def on_gui_setting(self) -> None:
        self.map.set_mode(self.cfg.map_mode)
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
