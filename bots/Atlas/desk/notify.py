"""Notifikationer via ntfy (https://ntfy.sh eller egen server). Fejl ignoreres."""
from __future__ import annotations

import threading
import urllib.request


class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.NTFY_TOPIC)

    def send(self, message: str, title: str = "Papirdesk", tags: str = "", priority: str = "default",
             blocking: bool = False) -> bool:
        if not self.enabled:
            return False
        url = f"{self.cfg.NTFY_SERVER.rstrip('/')}/{self.cfg.NTFY_TOPIC}"
        # HTTP-headere skal være latin-1; ntfy accepterer RFC 2047 til titler med andre tegn.
        safe_title = title.encode("latin-1", "replace").decode("latin-1")
        headers = {"Title": safe_title, "Priority": priority}
        if tags:
            headers["Tags"] = tags

        def go():
            try:
                req = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
                urllib.request.urlopen(req, timeout=10).read()
                return True
            except Exception:  # noqa: BLE001
                return False
        if blocking:
            return go()
        threading.Thread(target=go, daemon=True).start()
        return True
