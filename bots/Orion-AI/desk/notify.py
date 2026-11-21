"""ntfy-notifikationer (kun køb, salg, stop og nedbrud). Fejl ignoreres."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

TITLES = {"buy": "Papirkøb", "sell": "Papirsalg", "stop": "Stop ramt", "crash": "Papirdesk nedbrud"}
TAGS = {"buy": "green_circle", "sell": "white_circle", "stop": "red_circle", "crash": "warning"}


class Notifier:
    def __init__(self, url: str, events, timeout: float = 5):
        self.url = url.strip()
        self.events = set(events)
        self.timeout = timeout
        self.sent: list[tuple[str, str]] = []   # til tests/fejlsøgning

    def send(self, event: str, message: str) -> bool:
        if event not in self.events:
            return False
        self.sent.append((event, message))
        if not self.url:
            return False
        try:
            # JSON-publicering så danske tegn i titlen virker (headers er latin-1)
            u = urllib.parse.urlsplit(self.url)
            server = f"{u.scheme}://{u.netloc}"
            topic = u.path.strip("/").split("/")[-1]
            body = {"topic": topic, "title": TITLES.get(event, "Papirdesk"),
                    "message": message, "tags": [TAGS.get(event, "")],
                    "priority": 4 if event in ("crash", "stop") else 3}
            req = urllib.request.Request(server, data=json.dumps(body).encode("utf-8"),
                                         method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=self.timeout).close()
            return True
        except Exception:
            return False
