"""Optional reverse-DNS names for remote addresses (off by default: it asks your DNS resolver about
every address your apps talk to).  Lookups run on a small thread pool; results are cached."""
from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor


class Resolver:
    def __init__(self, lookup=socket.gethostbyaddr, workers: int = 4):
        self._lookup = lookup
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="portcullis-dns")
        self.names: "dict[str, str]" = {}
        self._pending: "set[str]" = set()

    def want(self, ips) -> bool:
        """Start lookups for unknown addresses.  True if anything new was queued."""
        new = [ip for ip in ips if ip not in self.names and ip not in self._pending]
        for ip in new[:50]:
            self._pending.add(ip)
            self._pool.submit(self._one, ip)
        return bool(new)

    def _one(self, ip: str) -> None:
        try:
            name = self._lookup(ip)[0]
            socket.setdefaulttimeout(None)
        except (OSError, IndexError, UnicodeError):
            name = ""
        self.names[ip] = name
        self._pending.discard(ip)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
