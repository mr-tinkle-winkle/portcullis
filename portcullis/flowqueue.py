"""
The listeners that see every NEW connection of every app.

nft sends the first packet of each new flow (``ct state new``) of an app's cgroup to a queue of its
own; this module binds all those queues in ONE thread (a select loop over their sockets), reads the
packet's addresses and ports while the callback runs, records the flow, and asks the Broker what
to do.  The verdict may come later (ask mode holds the packet) and is always applied from this
thread.  ``bypass`` in the nft rule means: if this process dies, new connections are simply allowed.
"""
from __future__ import annotations

import logging
import os
import queue
import select
import socket
import threading
import time
from dataclasses import dataclass

from . import flows

logger = logging.getLogger("portcullis.flowqueue")

FLOW_BASE = 20000
MAX_APPS = 400
COPY_RANGE = 128                # header bytes are all we need


def qnum(idx: int, direction: str) -> int:
    return FLOW_BASE + idx * 2 + (1 if direction == "in" else 0)


@dataclass(frozen=True)
class FlowMeta:
    identity: str
    unit: str
    direction: str


class FlowService:
    def __init__(self, table: flows.FlowTable, broker, *, make_nfq=None):
        self.table, self.broker = table, broker
        self._make_nfq = make_nfq
        self._wanted: "dict[int, FlowMeta]" = {}
        self._cmds: "queue.SimpleQueue" = queue.SimpleQueue()
        self._verdicts: "queue.SimpleQueue" = queue.SimpleQueue()
        self._stop = threading.Event()
        self._wake_r, self._wake_w = os.pipe()
        self._thread: "threading.Thread | None" = None
        self.errors: "dict[int, str]" = {}
        self.bound: "set[int]" = set()

    # -- control from other threads ------------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="flows", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._poke()
        if self._thread:
            self._thread.join(3)

    def sync(self, wanted: "dict[int, FlowMeta]") -> None:
        """Bind these queue numbers (and unbind any others)."""
        self._cmds.put(dict(wanted))
        self._poke()

    def give(self, pkt, verdict: str) -> None:
        """Verdict for a held packet; applied by the listener thread."""
        self._verdicts.put((pkt, verdict))
        self._poke()

    def _poke(self) -> None:
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass

    # -- the loop ---------------------------------------------------------------------------------------------------------------
    def _bind(self, n: int, meta: FlowMeta):
        cb = lambda pkt, meta=meta: self._on_packet(meta, pkt)       # noqa: E731
        if self._make_nfq is not None:
            nfq = self._make_nfq()
            nfq.bind(n, cb)
        else:
            from netfilterqueue import COPY_PACKET, NetfilterQueue
            nfq = NetfilterQueue()
            nfq.bind(n, cb, max_len=4096, mode=COPY_PACKET, range=COPY_RANGE)
        sock = socket.fromfd(nfq.get_fd(), socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        return nfq, sock

    def _on_packet(self, meta: FlowMeta, pkt) -> None:
        try:
            parsed = flows.parse_packet(pkt.get_payload())
        except Exception:  # noqa: BLE001
            parsed = None
        if parsed is None:
            pkt.accept()
            return
        rip, rport, lport = flows.remote_of(parsed, meta.direction)
        self.table.record(meta.identity, meta.unit, meta.direction, parsed.proto, rip, rport, lport)
        try:
            verdict = self.broker.on_flow(meta.identity, meta.direction, parsed.proto, rip, rport, pkt)
        except Exception:  # noqa: BLE001 -- a bug must never block the network
            logger.exception("decision failed")
            verdict = "accept"
        if verdict == "accept":
            pkt.accept()
        elif verdict == "drop":
            pkt.drop()

    def _run(self) -> None:
        active: "dict[int, tuple]" = {}               # qnum -> (nfq, sock)
        try:
            while not self._stop.is_set():
                self._apply_cmds(active)
                self._apply_verdicts()
                socks = [s for _, s in active.values()]
                ready, _, _ = select.select(socks + [self._wake_r], [], [], 0.5)
                if self._wake_r in ready:
                    try:
                        os.read(self._wake_r, 4096)
                    except OSError:
                        pass
                for n, (nfq, sock) in list(active.items()):
                    if sock in ready:
                        try:
                            nfq.run_socket(sock)
                        except Exception as e:  # noqa: BLE001
                            self.errors[n] = f"queue {n}: {e}"
                            logger.exception("queue %s failed", n)
                self._apply_verdicts()
                self.broker.expire()
        finally:
            self.broker.release_all()
            self._apply_verdicts()
            for n, (nfq, sock) in active.items():
                try:
                    nfq.unbind()
                finally:
                    sock.close()
            self.bound = set()

    def _apply_cmds(self, active: dict) -> None:
        latest = None
        while True:
            try:
                latest = self._cmds.get_nowait()
            except queue.Empty:
                break
        if latest is None:
            return
        for n in [n for n in active if n not in latest]:
            nfq, sock = active.pop(n)
            try:
                nfq.unbind()
            finally:
                sock.close()
            self.errors.pop(n, None)
        for n, meta in latest.items():
            if n in active:
                continue
            try:
                active[n] = self._bind(n, meta)
                self.errors.pop(n, None)
            except Exception as e:  # noqa: BLE001
                self.errors[n] = f"cannot bind queue {n}: {e}"
                logger.warning("%s", self.errors[n])
        self.bound = set(active)

    def _apply_verdicts(self) -> None:
        while True:
            try:
                pkt, verdict = self._verdicts.get_nowait()
            except queue.Empty:
                return
            try:
                pkt.accept() if verdict == "accept" else pkt.drop()
            except Exception:  # noqa: BLE001
                logger.exception("verdict failed")
