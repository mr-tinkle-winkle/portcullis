"""Talking to the daemon without ever blocking the window: requests run on worker threads and the
answers come back as Qt signals (queued onto the GUI thread)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Signal

from .. import ipc


class Bridge(QObject):
    overview = Signal(dict)          # a fresh overview from the daemon
    down = Signal(str)               # the daemon can't be reached: why
    replied = Signal(str, dict)      # (token, reply) for send()

    def __init__(self, request=ipc.request, parent=None):
        super().__init__(parent)
        self._request = request
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="portcullis-ui")
        self._polling = False

    def poll(self) -> None:
        if self._polling:
            return
        self._polling = True
        self._pool.submit(self._poll)

    def _poll(self) -> None:
        try:
            reply = self._request({"cmd": "overview"})
            if reply.get("ok"):
                self.overview.emit(reply)
            else:
                self.down.emit(reply.get("error", "the service refused the request"))
        except ConnectionError as e:
            self.down.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.down.emit(f"{type(e).__name__}: {e}")
        finally:
            self._polling = False

    def send(self, cmd: dict, token: str = "") -> None:
        self._pool.submit(self._send, cmd, token)

    def _send(self, cmd: dict, token: str) -> None:
        try:
            reply = self._request(cmd)
        except ConnectionError as e:
            reply = {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self.replied.emit(token, reply)
        self.poll()                                  # show the effect right away

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
