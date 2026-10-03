"""
Desktop notifications for ask mode, via org.freedesktop.Notifications over D-Bus (KDE Plasma shows
the four choices as buttons).  Every question gets one notification; it is closed again when the
question is answered (here, in the window, or by timing out).

The backend is injectable so the logic is testable without a session bus.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot

ACTIONS = (("allow_always", "Always allow"), ("allow_temp", "Allow temporarily"),
           ("ignore", "Ignore"), ("block_always", "Always block"))
SERVICE, PATH, IFACE = "org.freedesktop.Notifications", "/org/freedesktop/Notifications", "org.freedesktop.Notifications"


def describe(a: dict, place_label: str = "") -> "tuple[str, str]":
    """(title, body) for one pending question."""
    from .. import appid
    name = appid.pretty_name(a["identity"])
    verb = "wants to connect to" if a["direction"] == "out" else "is being contacted by"
    port = f" port {a['port']}/{a['proto']}" if a.get("port") else ""
    where = f" ({place_label})" if place_label else ""
    return f"{name} {verb} {a['ip']}", f"{a['ip']}{where}{port} - new {'outgoing' if a['direction'] == 'out' else 'incoming'} connection"


class DBusBackend(QObject):
    """The real thing.  ``notify`` returns the notification's id (0 on failure)."""
    actionInvoked = Signal(int, str)
    closed = Signal(int)

    def __init__(self):
        super().__init__()
        from PySide6.QtDBus import QDBusConnection
        self._bus = QDBusConnection.sessionBus()
        self.ok = self._bus.isConnected()
        if self.ok:
            # Qt wants the receiver and a SLOT()-style signature ("1name(args)") -- the 5-argument form is a TypeError
            for name, slot in (("ActionInvoked", "1_on_action(uint,QString)"), ("NotificationClosed", "1_on_closed(uint,uint)")):
                try:
                    self._bus.connect(SERVICE, PATH, IFACE, name, self, slot)
                except Exception:  # noqa: BLE001 -- no notification buttons is better than no window
                    self.ok = False

    @Slot("uint", str)
    def _on_action(self, nid, key) -> None:
        self.actionInvoked.emit(int(nid), str(key))

    @Slot("uint", "uint")
    def _on_closed(self, nid, _reason=0) -> None:
        self.closed.emit(int(nid))

    def notify(self, title: str, body: str, actions: "list[str]", timeout_ms: int, replaces: int = 0) -> int:
        if not self.ok:
            return 0
        from PySide6.QtDBus import QDBusInterface
        iface = QDBusInterface(SERVICE, PATH, IFACE, self._bus)
        if not iface.isValid():
            return 0
        reply = iface.call("Notify", "Portcullis", replaces, "network-wired", title, body, actions,
                           {"urgency": 2, "resident": True}, timeout_ms)
        args = reply.arguments()
        try:
            return int(args[0])
        except (IndexError, TypeError, ValueError):
            return 0

    def close(self, nid: int) -> None:
        if not self.ok or not nid:
            return
        from PySide6.QtDBus import QDBusInterface
        QDBusInterface(SERVICE, PATH, IFACE, self._bus).call("CloseNotification", int(nid))


class Notifier(QObject):
    """Mirrors the daemon's pending list as notifications; emits ``decided(ask_id, decision)``."""
    decided = Signal(int, str)

    def __init__(self, backend=None, label_for=lambda ip: "", parent=None):
        super().__init__(parent)
        self.backend = backend if backend is not None else DBusBackend()
        self._label_for = label_for
        self._by_ask: "dict[int, int]" = {}          # ask id -> notification id
        self._ask_of: "dict[int, int]" = {}          # notification id -> ask id
        if hasattr(self.backend, "actionInvoked"):
            self.backend.actionInvoked.connect(self._on_action)
            self.backend.closed.connect(self._on_closed)

    def sync(self, pending: "list[dict]", enabled: bool = True) -> None:
        live = {a["id"] for a in pending}
        for ask_id in [i for i in self._by_ask if i not in live or not enabled]:
            nid = self._by_ask.pop(ask_id)
            self._ask_of.pop(nid, None)
            self.backend.close(nid)
        if not enabled:
            return
        for a in pending:
            if a["id"] in self._by_ask:
                continue
            title, body = describe(a, self._label_for(a["ip"]))
            flat = [x for key, text in ACTIONS for x in (key, text)]
            nid = self.backend.notify(title, body, flat, max(3000, int(a.get("seconds_left", 20) * 1000)))
            self._by_ask[a["id"]] = nid
            if nid:
                self._ask_of[nid] = a["id"]

    def _on_action(self, nid: int, key: str) -> None:
        ask_id = self._ask_of.get(nid)
        if ask_id is not None and key in dict(ACTIONS):
            self.decided.emit(ask_id, key)

    def _on_closed(self, nid: int) -> None:
        ask_id = self._ask_of.pop(nid, None)
        if ask_id is not None:
            self._by_ask.pop(ask_id, None)
            self._by_ask[ask_id] = 0                 # dismissed without a choice: don't pop it up again
