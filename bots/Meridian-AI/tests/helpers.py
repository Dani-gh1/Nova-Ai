from datetime import datetime, timedelta, timezone

from desk.models import Candle, Quote

UTC = timezone.utc


def utc(*a):
    return datetime(*a, tzinfo=UTC)


def make_candles(start, n, tf_min, price=1.1, step=0.0, rng=0.0010):
    out = []
    p = price
    for i in range(n):
        o = p
        p = p + step
        out.append(Candle(start + timedelta(minutes=tf_min * i), o, max(o, p) + rng / 2,
                          min(o, p) - rng / 2, p))
    return out


class FakeMD:
    """Minimal markedsdata: faste lys pr. instrument/tidsramme og faste kurser."""

    def __init__(self, now):
        self.now = now
        self.c = {}
        self.q = {}
        self.errors = []

    def set(self, sym, price=1.1, step=0.0, rng=0.0010, spread=0.0001):
        for tf, m in (("M15", 15), ("H1", 60), ("H4", 240)):
            n = 300 if tf != "H4" else 200
            start = self.now - timedelta(minutes=m * n)
            self.c[(sym, tf)] = make_candles(start, n, m, price - step * n, step, rng)
        last = self.c[(sym, "M15")][-1].close
        self.q[sym] = Quote(sym, self.now, last - spread / 2, last + spread / 2)

    def refresh(self, now, symbols=None):
        self.now = now

    def candles(self, sym, tf):
        return self.c.get((sym, tf), [])

    def quote(self, sym):
        return self.q.get(sym)
