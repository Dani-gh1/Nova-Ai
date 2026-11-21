"""Beskeder til telefonen via ntfy (appen "ntfy", emnet står i NTFY_TOPIC i /etc/nova-ai.env)."""
from __future__ import annotations

import os
import threading
import time
import urllib.request


class Notifier:
    def __init__(self, topic: str | None = None, server: str = "https://ntfy.sh", now=time.time, post=None):
        self.topic = topic if topic is not None else os.environ.get("NTFY_TOPIC", "")
        self.server, self.now = server, now
        self.post = post or self._post
        self.last: dict[str, float] = {}
        self.lock = threading.Lock()

    def _post(self, title: str, text: str) -> None:
        req = urllib.request.Request(f"{self.server}/{self.topic}", data=text.encode(), method="POST",
                                     headers={"Title": title.encode("utf-8").decode("latin-1", "ignore"), "User-Agent": "nova-ai"})
        urllib.request.urlopen(req, timeout=10).close()

    def send(self, title: str, text: str, key: str | None = None, every: float = 0) -> bool:
        """Sender en besked. Med key og every sendes samme slags besked højst én gang pr. `every` sekunder."""
        if not self.topic:
            return False
        with self.lock:
            if key and every and self.now() - self.last.get(key, -1e18) < every:
                return False
            if key:
                self.last[key] = self.now()
        try:
            self.post(title, text)
            return True
        except OSError:
            return False

    def async_send(self, *a, **kw) -> None:
        threading.Thread(target=self.send, args=a, kwargs=kw, daemon=True).start()
