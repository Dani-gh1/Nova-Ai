"""Økonomisk kalender: vigtige nyheder (rentebeslutninger, jobtal, inflation).

Kilder: (1) gratis ugekalender i JSON (ForexFactory-format), (2) faste regler i config
(rentemøder, NFP første fredag, US CPI) så filteret virker selv uden net.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from . import config, market
from .data import DataError, http_get_json

UTC = timezone.utc


@dataclass(frozen=True)
class NewsEvent:
    time: datetime
    currency: str
    title: str
    impact: str = "High"
    source: str = "regel"


def _at_local(d: date, hh: int, mm: int, zone: str) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=market.tz(zone)).astimezone(UTC)


def scheduled_events(start: datetime, end: datetime) -> list:
    """Regelbaserede vigtige begivenheder i [start, end]."""
    out = []
    d = start.date() - timedelta(days=1)
    while d <= end.date() + timedelta(days=1):
        if d.weekday() == 4 and d.day <= 7:  # NFP: første fredag 08:30 NY
            out.append(NewsEvent(_at_local(d, 8, 30, "America/New_York"), "USD", "Non-Farm Payrolls (jobtal)"))
        if d.weekday() < 5 and 10 <= d.day <= 14 and d.weekday() == 2:  # US CPI ca. midt i måneden
            out.append(NewsEvent(_at_local(d, 8, 30, "America/New_York"), "USD", "US CPI (inflation)"))
        iso = d.isoformat()
        if iso in config.CENTRAL_BANK_MEETINGS.get("USD", ()):
            out.append(NewsEvent(_at_local(d, 14, 0, "America/New_York"), "USD", "FOMC rentebeslutning"))
        if iso in config.CENTRAL_BANK_MEETINGS.get("EUR", ()):
            out.append(NewsEvent(_at_local(d, 14, 15, "Europe/Berlin"), "EUR", "ECB rentebeslutning"))
        if iso in config.CENTRAL_BANK_MEETINGS.get("GBP", ()):
            out.append(NewsEvent(_at_local(d, 12, 0, "Europe/London"), "GBP", "BoE rentebeslutning"))
        d += timedelta(days=1)
    return [e for e in out if start <= e.time <= end]


def parse_feed(items: list) -> list:
    out = []
    for it in items:
        try:
            t = datetime.fromisoformat(it["date"]).astimezone(UTC)
        except (KeyError, ValueError):
            continue
        out.append(NewsEvent(t, it.get("country", "").upper(), it.get("title", ""),
                             it.get("impact", ""), "kalender"))
    return out


def currencies_for(symbol: str) -> set:
    base, quote = symbol.split("_")
    out = set()
    for c in (base, quote):
        out.add(config.COMMODITY_NEWS_CCY.get(c, c))
    return out


class NewsCalendar:
    def __init__(self, fetch: bool = True, extra: list | None = None):
        self.fetch = fetch
        self.events: list = list(extra or [])
        self._fetched_at = 0.0
        self.last_error = ""

    def refresh(self, now: datetime) -> None:
        rules = scheduled_events(now - timedelta(days=2), now + timedelta(days=8))
        feed = []
        if self.fetch and now.timestamp() - self._fetched_at > config.NEWS_REFRESH_S:
            try:
                feed = parse_feed(http_get_json(config.NEWS_FEED_URL))
                config.DATA_DIR.mkdir(parents=True, exist_ok=True)
                config.NEWS_CACHE_PATH.write_text(json.dumps(
                    [{"date": e.time.isoformat(), "country": e.currency, "title": e.title,
                      "impact": e.impact} for e in feed]))
                self._fetched_at = now.timestamp()
                self.last_error = ""
            except (DataError, OSError) as e:
                self.last_error = str(e)
        if not feed and config.NEWS_CACHE_PATH.exists():
            try:
                feed = parse_feed(json.loads(config.NEWS_CACHE_PATH.read_text()))
            except (OSError, ValueError):
                feed = []
        if feed or not self.events:
            self.events = sorted(rules + feed, key=lambda e: e.time)

    def important(self):
        return [e for e in self.events if e.impact in config.NEWS_IMPACTS]

    def nearest(self, symbol: str, now: datetime):
        """(minutter til nærmeste vigtige nyhed – negativ = siden, begivenhed) for instrumentet."""
        ccys = currencies_for(symbol)
        best = None
        for e in self.important():
            if e.currency not in ccys:
                continue
            m = (e.time - now).total_seconds() / 60
            if m < -180:
                continue
            if best is None or abs(m) < abs(best[0]):
                best = (m, e)
        return best

    def upcoming(self, now: datetime, hours: float = 24) -> list:
        end = now + timedelta(hours=hours)
        return [e for e in self.important() if now - timedelta(minutes=30) <= e.time <= end]
