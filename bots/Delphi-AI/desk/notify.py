"""ntfy-notifikationer. Papirpenge vises altid i procent."""
from __future__ import annotations

import time
import urllib.parse

from . import config
from .http_client import post_text

_last_error_ts = 0.0


def send(title: str, message: str, priority: str = "default", tags: str = "") -> bool:
    if not config.NTFY_TOPIC:
        return False
    url = f"{config.NTFY_URL.rstrip('/')}/{urllib.parse.quote(config.NTFY_TOPIC)}"
    headers = {"Title": title.encode("ascii", "replace").decode(), "Priority": priority}
    if tags:
        headers["Tags"] = tags
    try:
        post_text(url, message, headers)
        return True
    except Exception:
        return False


def error(message: str) -> bool:
    global _last_error_ts
    if time.time() - _last_error_ts < config.NTFY_ERROR_COOLDOWN_MIN * 60:
        return False
    _last_error_ts = time.time()
    return send("desk: fejl", message[:1000], priority="high", tags="warning")
