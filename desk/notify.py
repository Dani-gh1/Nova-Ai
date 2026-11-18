"""ntfy-notifikationer (valgfrit). Sendes i en baggrundstråd, så løkken aldrig blokerer."""
from __future__ import annotations

import logging
import queue
import threading
import time
import urllib.parse

from . import config
from .net import post_text

log = logging.getLogger("desk.notify")


class Notifier:
    def __init__(self, topic: str | None = None, server: str | None = None):
        self.topic = config.NTFY_TOPIC if topic is None else topic
        self.server = (server or config.NTFY_SERVER).rstrip("/")
        self.q: queue.Queue = queue.Queue(maxsize=200)
        self._last = 0.0
        self._pending: list[str] = []
        if self.topic:
            threading.Thread(target=self._worker, name="ntfy", daemon=True).start()

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    def send(self, title: str, body: str, priority: str = "default", tags: str = "") -> None:
        """Høj prioritet sendes straks; resten samles og sendes højst hvert NTFY_MIN_INTERVAL."""
        if not self.enabled:
            return
        if priority in ("high", "urgent"):
            self._put(title, body, priority, tags)
            return
        self._pending.append(f"{title}: {body}")
        if time.time() - self._last >= config.NTFY_MIN_INTERVAL:
            self.flush()

    def flush(self) -> None:
        if self._pending:
            msgs, self._pending = self._pending, []
            title = msgs[0].split(":")[0] if len(msgs) == 1 else f"Papirdesk: {len(msgs)} hændelser"
            self._put(title, "\n".join(msgs[:20]), "default", "chart_with_upwards_trend")

    def _put(self, title, body, priority, tags) -> None:
        self._last = time.time()
        try:
            self.q.put_nowait((title, body, priority, tags))
        except queue.Full:
            pass

    def _worker(self) -> None:
        while True:
            title, body, priority, tags = self.q.get()
            # titel som query-parameter, så æ/ø/å overlever (HTTP-headers er latin-1)
            qs = urllib.parse.urlencode({"title": title, "priority": priority, "tags": tags})
            try:
                post_text(f"{self.server}/{self.topic}?{qs}", body)
            except Exception as e:  # noqa: BLE001
                log.warning("ntfy fejlede: %s", e)
