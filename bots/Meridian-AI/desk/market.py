"""Markedets ur: weekendlukning, helligdage, sessioner, handelsdag og rollover."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from . import config
from .models import Instrument, MarketStatus

UTC = timezone.utc


@lru_cache(maxsize=None)
def tz(name: str) -> ZoneInfo:
    return ZoneInfo(name)


@lru_cache(maxsize=None)
def instrument(symbol: str) -> Instrument:
    kind, pip, spread, lev = config.INSTRUMENTS[symbol]
    return Instrument(symbol, kind, pip, spread, lev)


def all_instruments() -> list:
    return [instrument(s) for s in config.INSTRUMENTS]


def ny(now: datetime) -> datetime:
    return now.astimezone(tz(config.MARKET_TZ))


def trading_day(now: datetime) -> str:
    """Forex-handelsdagen skifter kl. 17:00 New York-tid."""
    return (ny(now) + timedelta(hours=24 - config.MARKET_CLOSE_FRIDAY_HOUR)).date().isoformat()


@lru_cache(maxsize=None)
def _holidays() -> frozenset:
    return frozenset(tuple(int(x) for x in d.split("-")) for d in config.MARKET_HOLIDAYS)


def _is_holiday(local: datetime) -> bool:
    return (local.month, local.day) in _holidays()


def is_market_open(now: datetime) -> bool:
    t = ny(now)
    wd, h = t.weekday(), t.hour
    if wd == 5:
        return False
    if wd == 4 and h >= config.MARKET_CLOSE_FRIDAY_HOUR:
        return False
    if wd == 6 and h < config.MARKET_OPEN_SUNDAY_HOUR:
        return False
    if _is_holiday(t):
        return False
    return True


def is_instrument_open(symbol: str, now: datetime) -> bool:
    if not is_market_open(now):
        return False
    if instrument(symbol).kind != "fx":
        t = ny(now)
        a, b = config.COMMODITY_DAILY_BREAK
        if t.weekday() < 5 and a <= t.hour < b:
            return False
    return True


def active_sessions(now: datetime) -> tuple:
    """Aktive sessionsgrupper: 'asia', 'london', 'newyork' (DST-korrekt via zoneinfo)."""
    if not is_market_open(now):
        return ()
    active = set()
    for name, (zone, start, end) in config.SESSIONS.items():
        local = now.astimezone(tz(zone))
        if local.weekday() < 5 and start <= local.hour < end:
            active.add(name)
    groups = []
    for g, members in config.SESSION_GROUPS.items():
        if any(m in active for m in members):
            groups.append(g)
    return tuple(groups)


def session_start(group: str, now: datetime) -> datetime | None:
    """Starttidspunkt (UTC) for den aktive session i gruppen, ellers None."""
    best = None
    for m in config.SESSION_GROUPS[group]:
        zone, start, end = config.SESSIONS[m]
        local = now.astimezone(tz(zone))
        if start <= local.hour < end and local.weekday() < 5:
            s = local.replace(hour=start, minute=0, second=0, microsecond=0).astimezone(UTC)
            best = s if best is None or s < best else best
    return best


def minutes_into_session(group: str, now: datetime) -> float | None:
    s = session_start(group, now)
    return None if s is None else (now - s).total_seconds() / 60


def next_change(now: datetime) -> datetime:
    """Næste åbning (hvis lukket) eller næste weekendlukning (hvis åben)."""
    t = now.replace(second=0, microsecond=0)
    t -= timedelta(minutes=t.minute % 15)
    state = is_market_open(now)
    memo = _NEXT_MEMO.get(state)
    if memo and memo[0] <= t < memo[1]:
        return memo[1]
    res = _next_change_slot(t, state)
    _NEXT_MEMO[state] = (t, res)
    return res


_NEXT_MEMO: dict = {}


@lru_cache(maxsize=4096)
def _next_change_slot(t: datetime, state: bool) -> datetime:
    step = timedelta(minutes=15)
    for _ in range(4 * 24 * 10):
        t += step
        if is_market_open(t) != state:
            return t
    return t


def minutes_to_close(now: datetime) -> float | None:
    if not is_market_open(now):
        return None
    return (next_change(now) - now).total_seconds() / 60


def in_rollover_blackout(now: datetime) -> bool:
    t = ny(now)
    roll = t.replace(hour=config.MARKET_CLOSE_FRIDAY_HOUR, minute=0, second=0, microsecond=0)
    before, after = config.ROLLOVER_BLACKOUT_MIN
    for r in (roll, roll - timedelta(days=1), roll + timedelta(days=1)):
        if r - timedelta(minutes=before) <= t < r + timedelta(minutes=after):
            return True
    return False


def status(now: datetime) -> MarketStatus:
    open_ = is_market_open(now)
    sess = active_sessions(now)
    if not open_:
        label = "Lukket (weekend)" if ny(now).weekday() in (4, 5, 6) else "Lukket (helligdag)"
    elif sess:
        label = "Åbent – " + " + ".join(config.SESSION_NAMES_DA[s] for s in sess)
    else:
        label = "Åbent – mellem sessioner"
    return MarketStatus(open_, sess, label, next_change(now), trading_day(now))
