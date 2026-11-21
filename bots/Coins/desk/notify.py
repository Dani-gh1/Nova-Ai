"""Push notifications to the phone via ntfy (https://ntfy.sh): a free app, no account.

Set NTFY_TOPIC in the environment (a long, hard-to-guess name: anyone who knows it can read
the messages) and subscribe to the same topic in the ntfy app. NTFY_SERVER changes the server
(default https://ntfy.sh). Without NTFY_TOPIC nothing is sent.

Messages go out from a background thread, so a slow or unreachable ntfy never delays a trade
or a stop. A failed message is dropped after two tries; the desk keeps running either way.
Paper money is shown in percent only.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import urllib.request

REASONS = {"stop_loss": "Stop loss", "trailing_stop": "Trailing stop", "stale_price": "Ingen kurs i 5 min.",
           "take_profit": "Gevinstmål nået", "time_stop": "Tidsgrænse nået"}


def _describe(e: Exception) -> str:
    """The error without the topic name (which works like a password)."""
    code = getattr(e, "code", None)
    if code:
        body = ""
        try:
            body = e.read().decode(errors="replace")[:200]      # ntfy explains 4xx errors in the body
        except Exception:   # noqa: BLE001
            pass
        return f"HTTP {code} {body}".strip()
    return type(e).__name__ + ": " + str(getattr(e, "reason", e))[:200]


def pct(x: float, digits: int = 1) -> str:
    """+1,5 % / −0,8 % (Danish decimal comma, real minus sign)."""
    s = f"{abs(x):.{digits}f}".replace(".", ",")
    return ("+" if x >= 0 else "−") + s + " %"


class Notifier:
    def __init__(self, topic: str | None, server: str = "https://ntfy.sh", timeout: float = 10.0):
        self.topic = (topic or "").strip()
        self.server = server.rstrip("/")
        self.timeout = timeout
        self.sent = 0
        self.failed = 0
        self.last_error: str | None = None
        self.on_error = None              # callback(str), set by the desk so failures reach the log
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._thread: threading.Thread | None = None

    @classmethod
    def from_env(cls) -> "Notifier":
        return cls(os.environ.get("NTFY_TOPIC"), os.environ.get("NTFY_SERVER") or "https://ntfy.sh")

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    def send(self, title: str, message: str, tags: tuple[str, ...] = (), priority: int = 3) -> None:
        if not self.enabled:
            return
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="notify", daemon=True)
            self._thread.start()
        try:
            self._q.put_nowait({"topic": self.topic, "title": title, "message": message,
                                "tags": list(tags), "priority": priority})
        except queue.Full:
            self.failed += 1

    def flush(self, timeout: float = 15.0) -> None:
        """Wait (a little) for queued messages, so the 'stopped' message leaves before the process does."""
        if self._thread is None:
            return
        done = threading.Event()
        try:
            self._q.put({"_flush": done}, timeout=1)
        except queue.Full:
            return
        done.wait(timeout)

    def _worker(self) -> None:
        while True:
            msg = self._q.get()
            if "_flush" in msg:
                msg["_flush"].set()
                continue
            for _ in range(2):
                try:
                    self._post(msg)
                    self.sent += 1
                    break
                except Exception as e:   # noqa: BLE001 - a notification must never take the desk down
                    self.last_error = _describe(e)
                    continue
            else:
                self.failed += 1
                if self.on_error:
                    try:
                        self.on_error(self.last_error or "unknown error")
                    except Exception:   # noqa: BLE001
                        pass

    def _post(self, msg: dict) -> None:
        req = urllib.request.Request(self.server + "/", data=json.dumps(msg).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()
