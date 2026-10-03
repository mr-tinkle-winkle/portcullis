"""``portcullis daemon`` -- the system service (runs with CAP_NET_ADMIN as its own user)."""
from __future__ import annotations

import logging
import signal
import threading

from . import appid, ipc, rules
from .engine import Engine
from .profiles import ProfileStore, StoreError

logger = logging.getLogger("portcullis.daemon")


class Controller:
    """Turns control-socket requests into store / engine operations."""

    def __init__(self, store: ProfileStore, engine: Engine, wake: threading.Event):
        self.store, self.engine, self.wake = store, engine, wake

    def __call__(self, req: dict) -> dict:
        cmd = req.get("cmd")
        try:
            if cmd == "ping":
                return {"ok": True}
            if cmd == "status":
                return {"ok": True, **self.engine.status()}
            if cmd == "apps":
                return {"ok": True, "apps": self.engine.apps()}
            if cmd == "list":
                return {"ok": True, "profiles": self.engine.status()["profiles"]}
            if cmd == "add":
                p = self.store.add(**req.get("fields", {}))
                return self._changed({"ok": True, "name": p.name})
            if cmd == "set":
                p = self.store.update(req["name"], req.get("changes", {}))
                return self._changed({"ok": True, "name": p.name})
            if cmd == "remove":
                self.store.remove(req["name"])
                return self._changed({"ok": True})
            if cmd == "selftest":
                return {"ok": True, "result": rules.selftest(self.engine.v2root)}
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        except StoreError as e:
            return {"ok": False, "error": str(e)}
        except KeyError as e:
            return {"ok": False, "error": f"missing field {e}"}

    def _changed(self, reply: dict) -> dict:
        self.engine.step()                 # apply right away; the periodic pass keeps it in step afterwards
        self.wake.set()
        return reply


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    store = ProfileStore()
    engine = Engine(store)
    rules.remove_table()                    # stale rules from a previous run must not outlive their queues
    wake, stop = threading.Event(), threading.Event()
    server = ipc.ControlServer(ipc.socket_path(), Controller(store, engine, wake))
    server.serve_in_thread()

    def bye(*_):
        stop.set()
        wake.set()

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    logger.info("running; cgroup2 root %s, %d profile(s)", engine.v2root, len(store.all()))
    try:
        while not stop.is_set():
            try:
                engine.step()
            except Exception:  # noqa: BLE001 -- one bad pass must not kill the service
                logger.exception("pass failed")
            wake.wait(1.0)
            wake.clear()
    finally:
        server.shutdown()
        engine.shutdown()                   # removes the nft table: no rules outlive the daemon
    return 0
