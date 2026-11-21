import tempfile
import types
from pathlib import Path

import config
from desk.models import Bar, Snapshot


def cfg(**over):
    """Kopi af config med midlertidige stier og evt. overstyringer."""
    ns = types.SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    tmp = Path(tempfile.mkdtemp(prefix="papirdesk-test-"))
    ns.DATA_DIR, ns.CACHE_DIR = tmp, tmp / "cache"
    ns.STATE_FILE, ns.DB_FILE = tmp / "state.json", tmp / "desk.sqlite"
    ns.BACKTEST_RESULTS, ns.BACKTEST_REPORT = tmp / "bt.json", tmp / "bt.md"
    ns.NTFY_TOPIC = ""
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def trend_bars(n=260, start=100.0, step=0.002, vol=1_000_000 * 5, rng=0.02):
    bars, px = [], start
    from datetime import date, timedelta
    d = date(2024, 1, 2)
    for i in range(n):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        o = px
        c = px * (1 + step)
        bars.append(Bar(d.isoformat(), o, max(o, c) * (1 + rng / 2), min(o, c) * (1 - rng / 2), c, vol))
        px = c
        d += timedelta(days=1)
    return bars


def snap(symbol="AAA", bars=None, price=None, open_=None, volume=None, sector="Information Technology",
         is_etf=False, **kw):
    bars = bars if bars is not None else trend_bars()
    last = bars[-1].close
    price = price if price is not None else last * 1.01
    return Snapshot(symbol=symbol, sector=sector, is_etf=is_etf, price=price,
                    open=open_ if open_ is not None else last, prev_close=last,
                    day_high=max(price, open_ or last), day_low=min(price, open_ or last),
                    volume_today=volume if volume is not None else bars[-1].volume,
                    day_fraction=kw.pop("day_fraction", 1.0), bars=bars, asof="2025-01-02T12:00:00", **kw)
