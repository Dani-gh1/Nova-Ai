"""Markedsdata: datakilder (OANDA practice, Twelve Data, Yahoo Finance, syntetisk) og cache.

Sikkerhed: HTTP-klienten kan KUN lave GET-kald. Der findes ingen kode der opretter ordrer.
"""
from __future__ import annotations

import bisect
import json
import time as _time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import config, market
from .models import Candle, Quote

UTC = timezone.utc
TF_MINUTES = {"M15": 15, "H1": 60, "H4": 240}


class DataError(RuntimeError):
    pass


def http_get_json(url: str, headers: dict | None = None) -> dict:
    """Eneste vej ud på nettet for data. Bevidst kun GET."""
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    assert req.get_method() == "GET"
    try:
        with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT_S) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # netværk, HTTP-fejl, JSON
        raise DataError(f"GET {url.split('?')[0]} fejlede: {e}") from e


def model_spread(symbol: str, now: datetime) -> float:
    """Realistisk spread når kilden ikke leverer bid/ask: typisk spread x sessionsfaktor."""
    ins = market.instrument(symbol)
    base = ins.typical_spread_pips * ins.pip
    if market.in_rollover_blackout(now):
        return base * 3.0
    own = set(config.INSTRUMENT_SESSIONS.get(symbol, ()))
    active = set(market.active_sessions(now))
    return base if own & active else base * 1.6


def resample(candles: list, minutes: int) -> list:
    """Saml M15-lys til større tidsrammer (justeret til UTC-midnat)."""
    out: list = []
    cur = None
    for c in candles:
        epoch_min = int(c.time.timestamp() // 60)
        bucket = datetime.fromtimestamp((epoch_min - epoch_min % minutes) * 60, UTC)
        if cur is None or cur[0] != bucket:
            if cur:
                out.append(Candle(*cur))
            cur = [bucket, c.open, c.high, c.low, c.close, c.volume]
        else:
            cur[2] = max(cur[2], c.high)
            cur[3] = min(cur[3], c.low)
            cur[4] = c.close
            cur[5] += c.volume
    if cur:
        out.append(Candle(*cur))
    return out


def closed_only(candles: list, tf: str, now: datetime) -> list:
    d = timedelta(minutes=TF_MINUTES[tf])
    return [c for c in candles if c.time + d <= now]


# ---------------------------------------------------------------- kilder
class OandaProvider:
    """OANDA v20 practice-API (gratis demokonto). Bruger kun /instruments/{x}/candles."""
    name = "oanda"

    def __init__(self, token: str = None, base_url: str = None):
        self.token = token or config.OANDA_TOKEN
        self.base = (base_url or config.OANDA_BASE_URL).rstrip("/")
        if not self.token:
            raise DataError("OANDA_TOKEN mangler (miljøvariabel)")

    def candles(self, symbol: str, tf: str, count: int, now: datetime):
        q = urllib.parse.urlencode({"granularity": tf, "count": count, "price": "MBA"})
        url = f"{self.base}/v3/instruments/{symbol}/candles?{q}"
        data = http_get_json(url, {"Authorization": f"Bearer {self.token}"})
        out, quote = [], None
        for c in data.get("candles", []):
            t = datetime.strptime(c["time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
            m = c["mid"]
            if c.get("complete"):
                out.append(Candle(t, float(m["o"]), float(m["h"]), float(m["l"]), float(m["c"]),
                                  float(c.get("volume", 0))))
            if "bid" in c and "ask" in c:
                quote = Quote(symbol, now, float(c["bid"]["c"]), float(c["ask"]["c"]))
        return out, quote


class TwelveDataProvider:
    """Twelve Data (gratis: 800 kald/dag). Henter kun M15 og bygger H1/H4 selv."""
    name = "twelvedata"
    native = ("M15",)

    def __init__(self, key: str = None):
        self.key = key or config.TWELVEDATA_KEY
        if not self.key:
            raise DataError("TWELVEDATA_KEY mangler (miljøvariabel)")

    def candles(self, symbol: str, tf: str, count: int, now: datetime):
        interval = {"M15": "15min", "H1": "1h", "H4": "4h"}[tf]
        q = urllib.parse.urlencode({
            "symbol": config.TWELVEDATA_SYMBOLS[symbol], "interval": interval,
            "outputsize": min(count, 5000), "timezone": "UTC", "apikey": self.key})
        data = http_get_json(f"{config.TWELVEDATA_BASE_URL}/time_series?{q}")
        if data.get("status") == "error":
            raise DataError(f"Twelve Data: {data.get('message')}")
        out = []
        for v in reversed(data.get("values", [])):
            t = datetime.strptime(v["datetime"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            out.append(Candle(t, float(v["open"]), float(v["high"]), float(v["low"]),
                              float(v["close"]), float(v.get("volume") or 0)))
        out = closed_only(out, tf, now)
        if not out:
            return out, None
        sp = model_spread(symbol, now)
        last = out[-1].close
        return out, Quote(symbol, now, last - sp / 2, last + sp / 2)


class YahooProvider:
    """Yahoo Finance: kræver ingen nøgle, så botten kan scanne markedet uden opsætning.

    Uofficielt, men åbent endpoint (kun GET). Henter 15-minutters lys, første gang ca. to måneders historik (nok
    til H4-indikatorerne) og derefter kun det seneste døgn, som flettes ind. H1/H4 bygges af M15. Yahoo giver ingen
    bid/ask, så spreaden modelleres som hos Twelve Data. Volumen bruges ikke af strategierne.
    """
    name = "yahoo"
    native = ("M15",)
    KEEP = 6000
    HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; paper-desk)", "Accept": "application/json"}

    def __init__(self, base_url: str = None):
        self.base = (base_url or config.YAHOO_BASE_URL).rstrip("/")
        self._hist: dict = {}       # symbol -> {unix-tid: Candle}
        self._seeded: set = set()

    @staticmethod
    def parse(data: dict) -> list:
        chart = data.get("chart") or {}
        res = (chart.get("result") or [None])[0]
        if not res:
            raise DataError(f"Yahoo: {chart.get('error') or 'ingen data'}")
        ts = res.get("timestamp") or []
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        cols = [q.get(k) or [None] * len(ts) for k in ("open", "high", "low", "close", "volume")]
        out = []
        for i, t in enumerate(ts):
            o, h, l, c, v = (col[i] if i < len(col) else None for col in cols)
            if None in (o, h, l, c):             # Yahoo har huller (None) uden for handelstid
                continue
            out.append(Candle(datetime.fromtimestamp(t, UTC), float(o), float(h), float(l), float(c), float(v or 0)))
        return out

    def candles(self, symbol: str, tf: str, count: int, now: datetime):
        sym = config.YAHOO_SYMBOLS.get(symbol)
        if not sym:
            raise DataError(f"Yahoo: ukendt symbol {symbol}")
        params = {"interval": "15m", "includePrePost": "false"}
        if symbol not in self._seeded:
            end = int(now.timestamp())
            params.update(period1=end - config.YAHOO_HISTORY_DAYS * 86400, period2=end + 3600)
        else:
            params["range"] = "1d"
        data = http_get_json(f"{self.base}/v8/finance/chart/{urllib.parse.quote(sym)}?{urllib.parse.urlencode(params)}",
                             self.HEADERS)
        hist = self._hist.setdefault(symbol, {})
        for c in self.parse(data):
            hist[int(c.time.timestamp())] = c
        self._seeded.add(symbol)
        for k in sorted(hist)[:-self.KEEP]:
            del hist[k]
        out = closed_only([hist[k] for k in sorted(hist)], "M15", now)
        if not out:
            return out, None
        sp = model_spread(symbol, now)
        last = out[-1].close
        return out, Quote(symbol, now, last - sp / 2, last + sp / 2)


# ---------------------------------------------------------------- markedsdata-lag
class LiveMarketData:
    """Cacher lys pr. tidsramme og henter kun igen efter config.REFRESH_S."""

    def __init__(self, provider):
        self.provider = provider
        self._candles: dict = {}
        self._fetched: dict = {}
        self._quotes: dict = {}
        self.now = datetime.now(UTC)
        self.errors: list = []

    def refresh(self, now: datetime, symbols: list) -> None:
        self.now = now
        self.errors = []
        tfs = ("M15", "H1", "H4")
        for s in symbols:
            for tf in tfs:
                key = (s, tf)
                if _time.time() - self._fetched.get(key, 0) < config.REFRESH_S[tf] and tf != "M15":
                    continue
                if tf != "M15" and getattr(self.provider, "native", None) == ("M15",):
                    continue
                try:
                    cs, q = self.provider.candles(s, tf, config.CANDLE_COUNT[tf], now)
                    self._candles[key] = cs
                    self._fetched[key] = _time.time()
                    if tf == "M15" and q:
                        self._quotes[s] = q
                except DataError as e:
                    self.errors.append(str(e))
            if getattr(self.provider, "native", None) == ("M15",) and (s, "M15") in self._candles:
                m15 = self._candles[(s, "M15")]
                self._candles[(s, "H1")] = closed_only(resample(m15, 60), "H1", now)
                self._candles[(s, "H4")] = closed_only(resample(m15, 240), "H4", now)

    def candles(self, symbol: str, tf: str) -> list:
        return self._candles.get((symbol, tf), [])

    def quote(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol)


class ReplayMarketData:
    """Afspiller historiske/syntetiske M15-lys tidstro – bruges af backtest og tests."""

    def __init__(self, m15: dict, spreads: dict | None = None):
        self.m15 = m15
        self.h1 = {s: resample(c, 60) for s, c in m15.items()}
        self.h4 = {s: resample(c, 240) for s, c in m15.items()}
        self._times = {(s, tf): [c.time for c in series]
                       for tf, d in (("M15", self.m15), ("H1", self.h1), ("H4", self.h4))
                       for s, series in d.items()}
        self.spreads = spreads or {}
        self.now = None
        self.errors: list = []

    def refresh(self, now: datetime, symbols: list = None) -> None:
        self.now = now

    def candles(self, symbol: str, tf: str) -> list:
        series = {"M15": self.m15, "H1": self.h1, "H4": self.h4}[tf].get(symbol, [])
        cutoff = self.now - timedelta(minutes=TF_MINUTES[tf])
        i = bisect.bisect_right(self._times[(symbol, tf)], cutoff)
        lo = max(0, i - config.CANDLE_COUNT[tf])
        return series[lo:i]

    def quote(self, symbol: str) -> Quote | None:
        cs = self.candles(symbol, "M15")
        if not cs:
            return None
        sp = self.spreads.get((symbol, cs[-1].time)) or model_spread(symbol, self.now)
        return Quote(symbol, self.now, cs[-1].close - sp / 2, cs[-1].close + sp / 2)


def make_provider(name: str = None):
    name = name or config.DATA_PROVIDER
    if name == "auto":      # den bedste kilde, der er adgang til; Yahoo kræver ingen nøgle
        if config.OANDA_TOKEN:
            return OandaProvider()
        if config.TWELVEDATA_KEY:
            return TwelveDataProvider()
        return YahooProvider()
    if name == "yahoo":
        return YahooProvider()
    if name == "oanda":
        return OandaProvider()
    if name == "twelvedata":
        return TwelveDataProvider()
    if name == "synthetic":
        from .synthetic import SyntheticProvider
        return SyntheticProvider()
    raise DataError(f"Ukendt datakilde: {name}")
