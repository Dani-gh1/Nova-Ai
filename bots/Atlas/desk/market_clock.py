"""NYSE/Nasdaq-kalender: tidszoner, helligdage, halve dage, før-/eftermarked.

Alt regnes i børsens tidszone (America/New_York) via zoneinfo, så sommertid
(der skifter på andre datoer i USA end i Danmark) håndteres automatisk.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import config as _default_cfg


def _easter(year: int) -> date:
    """Gregoriansk påskedag (anonym algoritme)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=64)
def nyse_holidays(year: int) -> dict[date, str]:
    h: dict[date, str] = {}
    ny = date(year, 1, 1)
    if ny.weekday() == 6:
        h[ny + timedelta(days=1)] = "Nytår"
    elif ny.weekday() != 5:          # NYSE flytter ikke nytår til fredag før
        h[ny] = "Nytår"
    h[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    h[_nth_weekday(year, 2, 0, 3)] = "Presidents' Day"
    h[_easter(year) - timedelta(days=2)] = "Langfredag"
    h[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        h[_observed(date(year, 6, 19))] = "Juneteenth"
    h[_observed(date(year, 7, 4))] = "Independence Day"
    h[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    h[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving"
    h[_observed(date(year, 12, 25))] = "Juledag"
    # Nytår næste år på en lørdag observeres IKKE 31/12 af NYSE – ingen ekstra regel.
    return h


@lru_cache(maxsize=64)
def nyse_early_closes(year: int) -> set[date]:
    out = set()
    j3 = date(year, 7, 3)
    if j3.weekday() <= 3:                       # man-tor
        out.add(j3)
    out.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))   # dagen efter Thanksgiving
    c24 = date(year, 12, 24)
    if c24.weekday() <= 3:
        out.add(c24)
    return out


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class MarketStatus:
    session: str                 # "pre" | "regular" | "post" | "closed"
    label: str                   # dansk tekst
    is_trading_day: bool
    now_market: datetime
    open_at: datetime | None     # dagens regulære åbning (hvis handelsdag)
    close_at: datetime | None
    next_open: datetime          # næste regulære åbning (strengt efter nu hvis åbent)
    next_close: datetime | None
    holiday: str | None
    early_close: bool
    minutes_since_open: float | None
    day_fraction: float          # 0..1 af den regulære session


class MarketClock:
    def __init__(self, cfg=_default_cfg):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.MARKET_TZ)
        self.extra_holidays = {date.fromisoformat(d) for d in cfg.EXTRA_HOLIDAYS}
        self.extra_early = {date.fromisoformat(d) for d in cfg.EXTRA_EARLY_CLOSES}

    # --- kalender -----------------------------------------------------------
    def holiday_name(self, d: date) -> str | None:
        if d in self.extra_holidays:
            return "Ekstraordinær lukning"
        return nyse_holidays(d.year).get(d)

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and self.holiday_name(d) is None

    def is_early_close(self, d: date) -> bool:
        return self.is_trading_day(d) and (d in nyse_early_closes(d.year) or d in self.extra_early)

    def next_trading_day(self, d: date, include_today: bool = False) -> date:
        cur = d if include_today else d + timedelta(days=1)
        while not self.is_trading_day(cur):
            cur += timedelta(days=1)
        return cur

    def prev_trading_day(self, d: date) -> date:
        cur = d - timedelta(days=1)
        while not self.is_trading_day(cur):
            cur -= timedelta(days=1)
        return cur

    def trading_days_between(self, a: date, b: date) -> int:
        """Antal handelsdage i (a, b]."""
        n, cur = 0, a
        while cur < b:
            cur += timedelta(days=1)
            if self.is_trading_day(cur):
                n += 1
        return n

    def session_times(self, d: date) -> dict[str, datetime]:
        early = self.is_early_close(d)
        mk = lambda t: datetime.combine(d, _hm(t), self.tz)  # noqa: E731
        return {
            "pre": mk(self.cfg.PREMARKET_START),
            "open": mk(self.cfg.REGULAR_OPEN),
            "close": mk(self.cfg.EARLY_CLOSE if early else self.cfg.REGULAR_CLOSE),
            "post_end": mk(self.cfg.EARLY_POSTMARKET_END if early else self.cfg.POSTMARKET_END),
        }

    # --- status -------------------------------------------------------------
    def status(self, now: datetime | None = None) -> MarketStatus:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        local = now.astimezone(self.tz)
        d = local.date()
        trading = self.is_trading_day(d)
        holiday = self.holiday_name(d)
        open_at = close_at = None
        session, label = "closed", "Lukket"
        if trading:
            t = self.session_times(d)
            open_at, close_at = t["open"], t["close"]
            if t["pre"] <= local < t["open"]:
                session, label = "pre", "Førmarked"
            elif t["open"] <= local < t["close"]:
                session, label = "regular", "Åbent"
            elif t["close"] <= local < t["post_end"]:
                session, label = "post", "Eftermarked"
        elif holiday:
            label = f"Lukket ({holiday})"
        elif d.weekday() >= 5:
            label = "Lukket (weekend)"

        if trading and local < open_at:
            nxt = open_at
        else:
            nd = self.next_trading_day(d)
            nxt = self.session_times(nd)["open"]
        next_close = close_at if session == "regular" else self.session_times(nxt.date())["close"]

        mso = frac = None
        if session == "regular":
            mso = (local - open_at).total_seconds() / 60
            frac = (local - open_at).total_seconds() / (close_at - open_at).total_seconds()
        elif session in ("post",) or (trading and close_at and local >= close_at):
            frac = 1.0
        return MarketStatus(session, label, trading, local, open_at, close_at, nxt, next_close,
                            holiday, self.is_early_close(d) if trading else False, mso,
                            frac if frac is not None else 0.0)

    def next_prep_time(self, now: datetime) -> datetime:
        """Tidspunktet hvor botten skal vågne og forberede næste handelsdag."""
        st = self.status(now)
        return st.next_open - timedelta(minutes=self.cfg.PREP_MINUTES_BEFORE_OPEN)
