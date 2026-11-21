"""ntfy-notifikationer (valgfrit; slået fra når config.NTFY_TOPIC er tom)."""
from __future__ import annotations

import logging
import threading

import config
from desk import http

log = logging.getLogger("desk.notify")


def send(title: str, message: str, priority: str = "default", tags: str = "chart_with_upwards_trend"):
    if not config.NTFY_TOPIC:
        return

    def _go():
        try:
            http.post(f"{config.NTFY_SERVER.rstrip('/')}/{config.NTFY_TOPIC}", message.encode("utf-8"),
                      {"Title": title.encode("utf-8").decode("latin-1", "ignore"),
                       "Priority": priority, "Tags": tags})
        except Exception as e:          # notifikation må aldrig vælte desken
            log.warning("ntfy fejlede: %s", e)

    threading.Thread(target=_go, daemon=True).start()
