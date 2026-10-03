"""
Control socket: newline-delimited JSON, one request and one reply per connection.

The daemon listens on a Unix socket owned by the ``portcullis`` group (mode 0660): anyone in that
group can change profiles -- i.e. can block or delay the network of that user's apps -- and
nothing else.  The client side is Qt-free and tiny.
"""
from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
from pathlib import Path


def socket_path() -> str:
    return os.environ.get("PORTCULLIS_SOCKET", "/run/portcullis/control.sock")


def request(cmd: dict, path: "str | None" = None, timeout: float = 3.0) -> dict:
    """Send one request.  Raises ConnectionError with a readable reason if the daemon can't be reached."""
    path = path or socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall(json.dumps(cmd).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode())
    except FileNotFoundError:
        raise ConnectionError(f"the portcullis service isn't running ({path} doesn't exist)") from None
    except PermissionError:
        raise ConnectionError(f"no permission to use {path}: your user must be in the 'portcullis' group "
                              "(log out and in after adding it)") from None
    except (OSError, ValueError) as e:
        raise ConnectionError(f"can't talk to the portcullis service: {e}") from None
    finally:
        s.close()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(1 << 20)
            reply = self.server.handler(json.loads(line.decode()))
        except Exception as e:  # noqa: BLE001
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self.wfile.write(json.dumps(reply).encode() + b"\n")


class ControlServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, handler, mode: int = 0o660):
        self.handler = handler
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        super().__init__(path, _Handler)
        os.chmod(path, mode)

    def serve_in_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, name="control", daemon=True)
        t.start()
        return t
