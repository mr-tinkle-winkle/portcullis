"""``portcullis gui`` -- one window process per user.  A second ``portcullis gui`` just raises the
first; ``portcullis gui --hidden`` (what the NixOS user service runs) keeps a tray icon and the
desktop notifications for ask mode alive without a visible window."""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from ..ui_kit import set_settings_provider
from . import config as guicfg
from .notifier import Notifier
from .window import MainWindow


def make_icon(size: int = 64) -> QIcon:
    """A small portcullis (gate) glyph, drawn so no image asset is needed."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(230, 230, 230)
    p.setPen(QPen(c, size * 0.07))
    m = size * 0.14
    p.drawRoundedRect(int(m), int(m), int(size - 2 * m), int(size - 2 * m), size * 0.1, size * 0.1)
    for i in range(1, 4):
        x = m + (size - 2 * m) * i / 4
        p.drawLine(int(x), int(m), int(x), int(size - m * 0.4))
    for j in (0.38, 0.62):
        y = m + (size - 2 * m) * j
        p.drawLine(int(m), int(y), int(size - m), int(y))
    p.end()
    return QIcon(pm)


def _server_name() -> str:
    return f"portcullis-gui-{os.getuid()}"


def run(hidden: bool = False, argv: "list[str] | None" = None) -> int:
    # An already-running window just comes to the front.
    probe = QLocalSocket()
    probe.connectToServer(_server_name())
    if probe.waitForConnected(300):
        probe.write(b"show" if not hidden else b"ping")
        probe.flush()
        probe.waitForBytesWritten(300)
        return 0

    set_settings_provider(lambda: guicfg.load_readonly().theme)      # cached: the kit calls this constantly
    app = QApplication(argv if argv is not None else sys.argv[:1])
    app.setApplicationName("Portcullis")
    app.setWindowIcon(make_icon())
    cfg = guicfg.load()
    window = MainWindow(cfg=cfg)
    window.notifier = Notifier(label_for=window.label_for)
    window.notifier.decided.connect(window.decide)

    server = QLocalServer()
    QLocalServer.removeServer(_server_name())
    server.listen(_server_name())

    def on_conn():
        sock = server.nextPendingConnection()
        sock.waitForReadyRead(200)
        if bytes(sock.readAll()).strip() == b"show":
            window.present()
    server.newConnection.connect(on_conn)

    tray = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(make_icon(), app)
        menu = QMenu()
        menu.addAction("Open Portcullis", window.present)
        menu.addAction("Quit", app.quit)
        tray.setContextMenu(menu)
        tray.setToolTip("Portcullis")
        tray.activated.connect(lambda reason: window.present() if reason == QSystemTrayIcon.Trigger else None)
        tray.show()
        app.setQuitOnLastWindowClosed(False)
        window.keep_running = True
    if not hidden or tray is None:
        window.show()
    code = app.exec()
    window.shutdown()
    return code
