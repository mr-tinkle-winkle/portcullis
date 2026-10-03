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
from . import update
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


def read_message(sock, timeout_ms: int = 500) -> str:
    """One newline-terminated message (bytes can arrive in pieces); without a newline, whatever came in time."""
    import time
    buf, end = b"", time.monotonic() + timeout_ms / 1000
    while b"\n" not in buf and time.monotonic() < end:
        if sock.bytesAvailable() or sock.waitForReadyRead(50):
            buf += bytes(sock.readAll())
    return buf.decode(errors="replace").strip()


def _hand_over(hidden: bool) -> bool:
    """Talk to an already-running window.  Returns True when it took the request (this process should exit).

    Protocol: ``show|ping <build>``.  A window of the *same* build answers ``ok`` (and comes to the front for
    ``show``); a window of a *different* build answers ``bye`` and quits, so this newer process takes over.  A
    window too old to answer gets the plain ``show`` it understands."""
    probe = QLocalSocket()
    probe.connectToServer(_server_name())
    if not probe.waitForConnected(300):
        return False
    verb = "ping" if hidden else "show"
    probe.write(f"{verb} {update.build_id()}\n".encode())
    probe.flush()
    probe.waitForBytesWritten(300)
    reply = read_message(probe, 800)
    probe.disconnectFromServer()
    if reply == "ok":
        return True
    if reply == "bye":
        for _ in range(50):                                   # wait for it to let go of the name
            again = QLocalSocket()
            again.connectToServer(_server_name())
            if not again.waitForConnected(100):
                return False
            again.disconnectFromServer()
            import time
            time.sleep(0.1)
        return False
    old = QLocalSocket()                                      # an old window: just bring it up, like before
    old.connectToServer(_server_name())
    if not old.waitForConnected(300):
        return False                                          # it went away meanwhile: start normally
    old.write(b"show" if not hidden else b"ping")
    old.flush()
    old.waitForBytesWritten(300)
    return True


def run(hidden: bool = False, argv: "list[str] | None" = None) -> int:
    # An already-running window of this build just comes to the front; an older build steps aside.
    if _hand_over(hidden):
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
        verb, _, build = read_message(sock).partition(" ")
        if build and build != update.build_id():             # a different (newer) build started: let it take over
            sock.write(b"bye\n")
            sock.flush()
            sock.waitForBytesWritten(300)
            server.close()
            app.quit()
            return
        sock.write(b"ok\n")
        sock.flush()
        sock.waitForBytesWritten(300)
        if verb == "show":
            window.present()
    server.newConnection.connect(on_conn)

    def relaunch(exe: str) -> None:
        """Become the newly installed build (same process id, so a systemd user service keeps tracking it)."""
        window.shutdown()
        server.close()
        if tray is not None:
            tray.hide()
        args = [exe, "gui"] + ([] if window.isVisible() else ["--hidden"])
        os.execv(exe, args)
    window.relaunch = relaunch

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
