"""``portcullis overlay``: polls the service for what's blocked and keeps the corner panel in step.

Nothing is on screen while nothing is blocked (the window is destroyed, not just emptied, so nothing sits
above a fullscreen game).  Settings come from the window's config file (Settings -> Overlay) and apply live."""
from __future__ import annotations

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtGui import QGuiApplication

from .. import ipc
from ..ui import config as guicfg
from . import layershell, model
from .window import OverlaySurface

logger = logging.getLogger("portcullis.overlay")

TICK_MS = 100          # repaint (countdowns) and check the poll
POLL_S = 0.25          # ask the service this often


class Overlay(QObject):
    def __init__(self, request=ipc.request, clock=time.monotonic, config=guicfg.load_readonly,
                 force_layer_shell: "bool | None" = None):
        super().__init__()
        self.request, self.clock, self.config = request, clock, config
        platform = QGuiApplication.platformName() or ""
        self.is_wayland = platform.startswith("wayland")
        self.layer = (self.is_wayland and layershell.available()) if force_layer_shell is None else force_layer_shell
        self.can_show = (not self.is_wayland) or self.layer
        if not self.can_show:
            logger.warning("Wayland session but no layer-shell shim (PORTCULLIS_LAYERSHELL_LIB): the overlay "
                           "can't be shown.")
        self.surface: "OverlaySurface | None" = None
        self._surface_key = None
        self.lines: "list[model.Line]" = []
        self.blocks: list = []
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="portcullis-overlay")
        self._future = None
        self._last_poll = 0.0
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)

    def start(self) -> None:
        self.timer.start(TICK_MS)
        self.tick()

    def stop(self) -> None:
        self.timer.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._drop_surface()

    # -- polling ------------------------------------------------------------------------------------------------------
    def _fetch(self) -> list:
        try:
            reply = self.request({"cmd": "blocks"}, timeout=1.0)
        except TypeError:                                   # a request() without timeout (tests)
            reply = self.request({"cmd": "blocks"})
        except ConnectionError:
            return []                                       # service down: nothing to show
        return reply.get("blocks", []) if reply.get("ok") else []

    def tick(self) -> None:
        now = self.clock()
        if self._future is not None and self._future.done():
            try:
                self.apply(self._future.result(), now)
            except Exception:  # noqa: BLE001
                logger.exception("overlay poll failed")
            self._future = None
        if self._future is None and now - self._last_poll >= POLL_S:
            self._last_poll = now
            self._future = self._pool.submit(self._fetch)
        if self.surface is not None:
            self.surface.update()                          # countdowns

    def apply(self, blocks: list, now: "float | None" = None) -> None:
        now = self.clock() if now is None else now
        cfg = self.config()
        self.blocks = blocks
        self.lines = model.build_lines(blocks, now, ports=cfg.overlay_ports, addresses=cfg.overlay_addresses,
                                       only_running=cfg.overlay_only_running) if cfg.overlay_enabled else []
        if not self.lines or not self.can_show:
            self._drop_surface()
            return
        key = (cfg.overlay_corner, cfg.overlay_margin, round(cfg.overlay_scale, 2))
        if self.surface is None or key != self._surface_key:
            self._drop_surface()
            self.surface = OverlaySurface(cfg.overlay_corner, cfg.overlay_margin, cfg.overlay_scale,
                                          QGuiApplication.primaryScreen(), self.layer, self.clock)
            self._surface_key = key
            self.surface.set_lines(self.lines)
            self.surface.present()
        else:
            self.surface.set_lines(self.lines)

    def _drop_surface(self) -> None:
        if self.surface is not None:
            self.surface.hide()
            self.surface.deleteLater()
            self.surface = None
            self._surface_key = None


def _server_name() -> str:
    return f"portcullis-overlay-{os.getuid()}"


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # layer-shell turns EVERY window of this process into an overlay surface: only here, before QApplication
    if os.environ.get("QT_QPA_PLATFORM", "").startswith("wayland") or (
            not os.environ.get("QT_QPA_PLATFORM") and os.environ.get("WAYLAND_DISPLAY")):
        layershell.enable_in_this_process()
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication
    from ..ui_kit import set_settings_provider
    set_settings_provider(lambda: guicfg.load_readonly().theme)
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("portcullis-overlay")
    probe = QLocalSocket()                                  # one overlay per user
    probe.connectToServer(_server_name())
    if probe.waitForConnected(200):
        logger.info("an overlay is already running")
        return 0
    server = QLocalServer()
    QLocalServer.removeServer(_server_name())
    server.listen(_server_name())
    overlay = Overlay()
    overlay.start()
    code = app.exec()
    overlay.stop()
    return code
