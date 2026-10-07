"""``portcullis daemon`` -- the system service (runs with CAP_NET_ADMIN as its own user)."""
from __future__ import annotations

import logging
import signal
import threading

from . import appid, ask, flows, ipc, rules
from .engine import Engine
from .flowqueue import FlowService
from .flows import FlowTable, parse_conntrack
from .profiles import ProfileStore, StoreError
from .settings import SettingsStore

logger = logging.getLogger("portcullis.daemon")


class Controller:
    """Turns control-socket requests into store / engine operations."""

    def __init__(self, store: ProfileStore, engine: Engine, wake: threading.Event,
                 settings: "SettingsStore | None" = None, broker: "ask.Broker | None" = None):
        self.store, self.engine, self.wake = store, engine, wake
        self.settings, self.broker = settings, broker

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
            if cmd == "blocks":
                return {"ok": True, "blocks": self.engine.blocks()}
            if cmd == "overview":
                return {"ok": True, **self.engine.overview()}
            if cmd == "rule":                      # {identity, ip, [port, proto], verdict: allow|block|clear}
                rule = {k: req[k] for k in ("ip", "port", "proto") if k in req}
                verdict = req.get("verdict")
                if verdict not in ("allow", "block", "clear"):
                    return {"ok": False, "error": "verdict must be allow, block or clear"}
                p = self.store.set_rule(req["identity"], {**rule, "verdict": "allow" if verdict == "clear" else verdict},
                                        clear=verdict == "clear")
                return self._changed({"ok": True, "name": p.name})
            if cmd == "app":                       # {identity, changes: {block_in, ask, ...}} -- creates the profile if needed
                p = self.store.ensure_for(req["identity"])
                if req.get("changes"):
                    p = self.store.update(p.name, req["changes"])
                return self._changed({"ok": True, "name": p.name})
            if cmd == "port":                      # {identity | name, action: add|remove|enable|disable|toggle, [port_name], [spec]}
                if req.get("identity"):
                    p = self.store.ensure_for(req["identity"])
                else:
                    p = self.store.find(req["name"])
                    if p is None:
                        return {"ok": False, "error": f"no profile named {req['name']!r}"}
                p = self.store.port_action(p, req.get("action", ""), req.get("port_name", ""), req.get("spec"))
                if any(not x["enabled"] for x in p.ports) and not p.enabled:
                    self.store.update(p.name, {"enabled": True})
                return self._changed({"ok": True, "name": p.name, "ports": p.ports})
            if cmd == "answer":
                if self.broker is None:
                    return {"ok": False, "error": "ask mode isn't running"}
                try:
                    done = self.broker.answer(int(req["id"]), req["decision"])
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                return {"ok": True, "answered": done}
            if cmd == "settings":
                if self.settings is None:
                    return {"ok": False, "error": "no settings store"}
                if req.get("changes"):
                    self.settings.update(req["changes"])
                    self.engine.step()
                    self.wake.set()
                return {"ok": True, "settings": vars(self.settings.get())}
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


def read_conntrack() -> "set | None":
    try:
        with open("/proc/net/nf_conntrack") as fh:
            return parse_conntrack(fh.read())
    except OSError:
        return None


def read_sockets() -> "list | None":
    """Every TCP/UDP socket with its cgroup, via ``ss`` (sock_diag; needs no privileges).  None if ss is missing."""
    import subprocess
    try:
        r = subprocess.run(["ss", "-H", "-tunaO", "--cgroup"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return flows.parse_ss(r.stdout) if r.returncode == 0 else None


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    store, settings = ProfileStore(), SettingsStore()
    table = FlowTable()
    wake, stop = threading.Event(), threading.Event()
    holder: dict = {}
    svc_ref: dict = {}

    def add_rule(identity: str, rule: dict) -> None:
        store.set_rule(identity, rule)
        wake.set()                           # the main loop re-applies the ruleset (a block rule becomes a kernel drop)

    broker = ask.Broker(owner_for=lambda ident: holder["engine"].owner_of(ident), settings=settings.get,
                        add_rule=add_rule, sink=lambda pkt, v: svc_ref["svc"].give(pkt, v))
    service = FlowService(table, broker)
    svc_ref["svc"] = service
    engine = Engine(store, flows=service, table=table, broker=broker, settings=settings.get)
    holder["engine"] = engine
    rules.remove_table()                    # stale rules from a previous run must not outlive their queues
    service.start()
    server = ipc.ControlServer(ipc.socket_path(), Controller(store, engine, wake, settings, broker))
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
                socks = read_sockets() if settings.get().track_flows else None
                if socks is not None:
                    table.observe(flows.socket_flows(socks, engine.current_apps()))
                table.refresh_open(read_conntrack())
                for what in store.expire_blocks():
                    logger.info("auto-unblock: %s", what)
                    engine.step()
            except Exception:  # noqa: BLE001 -- one bad pass must not kill the service
                logger.exception("pass failed")
            due = store.unblock_due()
            wake.wait(1.0 if due is None else max(0.02, min(1.0, due)))
            wake.clear()
    finally:
        server.shutdown()
        engine.shutdown()                   # removes the nft table: no rules outlive the daemon
        service.stop()
    return 0
