"""HTTP (urllib) og en minimal WebSocket-klient (RFC 6455) - kun stdlib.

Bruges udelukkende til OFFENTLIGE markedsdata. Ingen nøgler, ingen signering.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger("desk.net")
USER_AGENT = "papirdesk/1.0 (paper trading, public data only)"


class HttpError(RuntimeError):
    pass


def get_json(url: str, params: dict | None = None, timeout: float | None = None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for attempt in range(config.HTTP_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout or config.HTTP_TIMEOUT) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (418, 429):          # rate limit: vent længere
                time.sleep(5 * (attempt + 1))
            elif e.code in (400, 403, 404, 451):
                break                          # nytter ikke at prøve igen
            else:
                time.sleep(1 + attempt)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1 + attempt)
    raise HttpError(f"GET {url} fejlede: {last}")


def post_text(url: str, body: str, headers: dict | None = None, timeout: float = 10) -> None:
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body.encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout):
        pass


class WebSocket:
    """Minimal synkron WebSocket-klient til tekstbeskeder."""

    def __init__(self, url: str, timeout: float = 30.0):
        u = urllib.parse.urlparse(url)
        self.host = u.hostname or ""
        self.port = u.port or (443 if u.scheme == "wss" else 80)
        self.path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.secure = u.scheme == "wss"
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buf = b""

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.secure:
            ctx = ssl.create_default_context()
            ca = os.environ.get("SSL_CERT_FILE")
            if ca and os.path.exists(ca):
                ctx.load_verify_locations(ca)
            raw = ctx.wrap_socket(raw, server_hostname=self.host)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {self.path} HTTP/1.1\r\nHost: {self.host}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\nUser-Agent: {USER_AGENT}\r\n\r\n")
        raw.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket-handshake afbrudt")
            head += chunk
        status, _, rest = head.partition(b"\r\n\r\n")
        if b" 101 " not in status.split(b"\r\n")[0]:
            raise ConnectionError(f"WebSocket-handshake afvist: {status[:80]!r}")
        self._buf = rest
        self.sock = raw

    def _recv_exact(self, n: int) -> bytes:
        assert self.sock is not None
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WebSocket lukket")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text: str, opcode: int = 1) -> None:
        assert self.sock is not None
        payload = text.encode()
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def recv(self) -> str | None:
        """Returnér næste tekstbesked (håndterer ping/fragmentering). None ved close."""
        message = b""
        while True:
            b1, b2 = self._recv_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b2 & 0x80 else None
            data = self._recv_exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 8:
                return None
            if opcode == 9:
                self.send(data.decode(errors="ignore"), opcode=10)
                continue
            if opcode == 10:
                continue
            message += data
            if fin:
                return message.decode(errors="ignore")

    def close(self) -> None:
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None
