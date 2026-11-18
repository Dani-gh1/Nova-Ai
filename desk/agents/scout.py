"""Scout: finder åbne markeder med nok volumen og en lukketid inden for vinduet."""
from __future__ import annotations

import time
from collections import Counter

from .. import config
from ..models import Market, Candidate


class Scout:
    name = "scout"

    def __init__(self, fetchers: dict | None = None):
        # fetchers: {"polymarket": callable() -> list[Market], ...} (kan injiceres i tests)
        if fetchers is None:
            from .. import sources
            fetchers = {p.strip(): sources.get(p.strip()).list_open
                        for p in config.SCOUT_PLATFORMS.split(",") if p.strip()}
        self.fetchers = fetchers
        self.errors: list[str] = []

    @staticmethod
    def check(m: Market, now: float) -> str | None:
        """None = godkendt, ellers årsag (dansk)."""
        if m.status != "open":
            return "ikke åbent"
        if m.volume < config.SCOUT_MIN_VOLUME_USD:
            return "for lidt samlet volumen"
        if m.volume_24h < config.SCOUT_MIN_VOLUME_24H_USD:
            return "for lidt 24t-volumen"
        h = m.hours_to_close(now)
        if h < config.SCOUT_MIN_HOURS_TO_CLOSE:
            return "lukker for tidligt"
        if h > config.SCOUT_MAX_DAYS_TO_CLOSE * 24:
            return "lukker uden for vinduet"
        return None

    def run(self, now: float | None = None) -> tuple[list[Candidate], Counter]:
        now = now or time.time()
        self.errors = []
        out, reasons, seen = [], Counter(), set()
        for platform, fetch in self.fetchers.items():
            try:
                markets = fetch()
            except Exception as e:  # én platform nede må ikke stoppe den anden
                self.errors.append(f"{platform}: {e}")
                continue
            for m in markets:
                if m.key in seen:
                    continue
                seen.add(m.key)
                why = self.check(m, now)
                if why:
                    reasons[why] += 1
                else:
                    out.append(Candidate(m))
        return out, reasons
