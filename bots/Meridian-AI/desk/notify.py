"""ntfy-notifikationer (https://ntfy.sh). Slået fra når NTFY_TOPIC ikke er sat."""
from __future__ import annotations

import urllib.request

from . import config


class Notifier:
    def __init__(self, topic: str | None = None, url: str | None = None):
        self.topic = topic if topic is not None else config.NTFY_TOPIC
        self.url = (url or config.NTFY_URL).rstrip("/")
        self.sent: list = []  # til tests / fejlsøgning

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    def send(self, title: str, message: str, priority: str = "default", tags: str = "") -> bool:
        self.sent.append((title, message))
        if not self.enabled:
            return False
        headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"), "Priority": priority}
        if tags:
            headers["Tags"] = tags
        req = urllib.request.Request(f"{self.url}/{self.topic}", data=message.encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10):
                return True
        except Exception:
            return False  # notifikationer må aldrig vælte botten
