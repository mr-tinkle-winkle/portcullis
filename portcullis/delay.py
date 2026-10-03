"""
Fake latency: one NFQUEUE per (profile, direction).  The kernel hands every matching packet to
this process and waits for a verdict; we hold each packet for the profile's delay and then
accept it unchanged.  Packets leave in the order they arrived (a delay change never reorders).

Costs, honestly: every delayed packet passes through userspace, so this suits game-sized traffic
(thousands of small packets per second) and is the wrong tool for saturating a fast download.
The delay is the configured delay plus up to a few ms of scheduling jitter.
"""
from __future__ import annotations

import logging
import os
import select
import socket
import threading
import time
from collections import deque

logger = logging.getLogger("portcullis.delay")

MAX_HELD = 50000            # beyond this, packets are accepted immediately instead of piling up
KERNEL_QUEUE_LEN = 65535


class DelayQueue:
    def __init__(self, qnum: int, get_delay_ms, *, clock=time.monotonic, max_held: int = MAX_HELD):
        self.qnum = qnum
        self._get_delay_ms = get_delay_ms
        self._clock = clock
        self._max_held = max_held
        self._held: "deque[tuple[float, object]]" = deque()
        self._last_due = 0.0
        self._stop = threading.Event()
        self._wake_r, self._wake_w = os.pipe()
        self.released = 0
        self.error = ""

    # -- scheduling core (no netfilter involved; unit-tested directly) ------------------------------
    def on_packet(self, pkt) -> None:
        delay = max(0.0, float(self._get_delay_ms())) / 1000.0
        now = self._clock()
        if (delay <= 0 and not self._held) or len(self._held) >= self._max_held:
            pkt.accept()
            self.released += 1
            return
        # (no pkt.retain(): that copies the payload, which we never read; a verdict only needs the
        #  packet id and queue handle, so it can be given later -- verified against real packets)
        due = max(now + delay, self._last_due)           # monotonic: never overtake an earlier packet
        self._last_due = due
        self._held.append((due, pkt))

    def release_due(self) -> int:
        now, n = self._clock(), 0
        while self._held and self._held[0][0] <= now:
            self._held.popleft()[1].accept()
            n += 1
        self.released += n
        return n

    def release_all(self) -> None:
        while self._held:
            self._held.popleft()[1].accept()
            self.released += 1

    def timeout(self) -> "float | None":
        if not self._held:
            return None
        return max(0.0, self._held[0][0] - self._clock())

    @property
    def held(self) -> int:
        return len(self._held)

    # -- the real thing --------------------------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()
        os.write(self._wake_w, b"x")

    def run(self) -> None:
        try:
            from netfilterqueue import COPY_META, NetfilterQueue
        except Exception as e:  # noqa: BLE001
            self.error = f"netfilterqueue is not available: {e}"
            logger.error("%s", self.error)
            return
        nfq = NetfilterQueue()
        try:
            nfq.bind(self.qnum, self.on_packet, max_len=KERNEL_QUEUE_LEN, mode=COPY_META)
        except Exception as e:  # noqa: BLE001
            self.error = f"cannot bind queue {self.qnum}: {e}"
            logger.error("%s", self.error)
            return
        sock = socket.fromfd(nfq.get_fd(), socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)                          # run_socket() reads until EAGAIN: it must not block
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([sock, self._wake_r], [], [], self.timeout())
                if sock in ready:
                    nfq.run_socket(sock)
                self.release_due()
        except Exception as e:  # noqa: BLE001
            self.error = f"queue {self.qnum} failed: {e}"
            logger.exception("queue %s failed", self.qnum)
        finally:
            self.release_all()                           # never strand a packet
            try:
                nfq.unbind()
            finally:
                sock.close()
