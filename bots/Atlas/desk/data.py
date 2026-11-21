"""Gratis datakilder (kun stdlib):

* Yahoo Finance chart-endpoint (v8): dagsbarer og intradag-kurser/volumen.
* Nasdaq.com API: bid/ask (spread) og earnings-kalender.
* NasdaqTrader RSS: aktuelle handelsstop (halts).

Alle kald går gennem en simpel hastighedsbegrænser med genforsøg. Dagsbarer
caches på disk, så botten kun henter fuld historik én gang pr. handelsdag.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import Bar, Snapshot

UA = "Mozilla/5.0 (X11; Linux x86_64) papirdesk/1.0"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?{qs}"
NASDAQ_INFO = "https://api.nasdaq.com/api/quote/{sym}/info?assetclass={cls}"
NASDAQ_EARNINGS = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
HALTS_RSS = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"


class Http:
    def __init__(self, rps: float = 4.0, timeout: float = 15, retries: int = 3):
        self.min_interval = 1.0 / rps if rps > 0 else 0
        self.timeout = timeout
        self.retries = retries
        self._lock = threading.Lock()
        self._next = 0.0

    def _wait(self):
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self.min_interval
        if wait > 0:
            time.sleep(wait)

    def get_text(self, url: str, headers: dict | None = None) -> str:
        last: Exception | None = None
        for attempt in range(self.retries):
            self._wait()
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (400, 401, 403, 404):
                    break
                time.sleep(1.5 * (2 ** attempt))
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last = e
                time.sleep(1.0 * (2 ** attempt))
        raise RuntimeError(f"GET {url} fejlede: {last}")

    def get_json(self, url: str, headers: dict | None = None):
        return json.loads(self.get_text(url, headers))


def yahoo_symbol(sym: str) -> str:
    return sym.replace(".", "-")


def parse_yahoo_daily(payload: dict, tz: ZoneInfo) -> list[Bar]:
    res = payload["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        # Juster OHLC for splits/udbytte med adjclose-forholdet, så indikatorer ikke springer.
        f = (adj[i] / c) if adj and adj[i] and c else 1.0
        d = datetime.fromtimestamp(t, tz).date().isoformat()
        bars.append(Bar(d, o * f, h * f, l * f, c * f, float(v or 0)))
    # fjern dubletter (Yahoo kan sende dagens bar to gange)
    dedup: dict[str, Bar] = {}
    for b in bars:
        dedup[b.date] = b
    return list(dedup.values())


class MarketData:
    """Live-dataudbyder. Bygger Snapshot-objekter til agenterne."""

    def __init__(self, cfg, clock, http: Http | None = None):
        self.cfg = cfg
        self.clock = clock
        self.http = http or Http(cfg.REQUESTS_PER_SECOND, cfg.HTTP_TIMEOUT, cfg.HTTP_RETRIES)
        self.tz = ZoneInfo(cfg.MARKET_TZ)
        self.daily_dir = cfg.CACHE_DIR / "daily"
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        self._earnings: dict[str, date] = {}
        self._earnings_day: date | None = None
        self.errors: list[str] = []

    # --- dagsbarer ------------------------------------------------------------
    def daily_bars(self, sym: str, rng: str | None = None, max_age_days: int = 0) -> list[Bar]:
        """Afsluttede dagsbarer (dagens ufærdige bar fjernes i åbningstiden)."""
        rng = rng or self.cfg.DAILY_HISTORY_RANGE
        path = self.daily_dir / f"{sym}_{rng}.json"
        st = self.clock.status()
        today = st.now_market.date()
        # Dagens bar er kun færdig når den regulære session er slut.
        if st.is_trading_day and st.close_at and st.now_market < st.close_at:
            complete = (today - timedelta(days=1)).isoformat()
        else:
            complete = today.isoformat()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                fetched = date.fromisoformat(data["fetched"])
                if (today - fetched).days <= max_age_days and data.get("complete_through", "") >= complete:
                    return [b for b in (Bar(*x) for x in data["bars"]) if b.date <= complete]
            except (ValueError, KeyError, TypeError):
                pass
        qs = urllib.parse.urlencode({"range": rng, "interval": "1d", "events": "div,split"})
        payload = self.http.get_json(YAHOO_CHART.format(sym=yahoo_symbol(sym), qs=qs))
        bars = parse_yahoo_daily(payload, self.tz)
        path.write_text(json.dumps({"fetched": today.isoformat(), "complete_through": complete,
                                    "bars": [[b.date, b.open, b.high, b.low, b.close, b.volume] for b in bars]}))
        return [b for b in bars if b.date <= complete]

    def bulk_daily(self, symbols: list[str], rng: str | None = None, max_age_days: int = 0) -> dict[str, list[Bar]]:
        out: dict[str, list[Bar]] = {}

        def one(s):
            try:
                return s, self.daily_bars(s, rng, max_age_days)
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{s}: {e}")
                return s, None

        with ThreadPoolExecutor(self.cfg.FETCH_WORKERS) as ex:
            for s, bars in ex.map(one, symbols):
                if bars:
                    out[s] = bars
        return out

    # --- intradag -------------------------------------------------------------
    def intraday(self, sym: str) -> dict | None:
        qs = urllib.parse.urlencode({"range": "1d", "interval": "5m", "includePrePost": "false"})
        payload = self.http.get_json(YAHOO_CHART.format(sym=yahoo_symbol(sym), qs=qs))
        res = payload["chart"]["result"][0]
        meta = res["meta"]
        q = res["indicators"]["quote"][0]
        ts = res.get("timestamp") or []
        opens = [o for o in q.get("open", []) if o is not None]
        vols = [v for v in q.get("volume", []) if v is not None]
        highs = [h for h in q.get("high", []) if h is not None]
        lows = [x for x in q.get("low", []) if x is not None]
        price = meta.get("regularMarketPrice")
        if not price:
            return None
        rmt = meta.get("regularMarketTime")
        return {
            "price": float(price),
            "open": float(opens[0]) if opens else float(price),
            "prev_close": float(meta.get("chartPreviousClose") or meta.get("previousClose") or price),
            "day_high": float(meta.get("regularMarketDayHigh") or (max(highs) if highs else price)),
            "day_low": float(meta.get("regularMarketDayLow") or (min(lows) if lows else price)),
            "volume": float(meta.get("regularMarketVolume") or sum(vols)),
            "last_trade_age_s": (time.time() - rmt) if rmt else None,
            "bar_date": datetime.fromtimestamp(ts[0], self.tz).date().isoformat() if ts else None,
        }

    def bulk_intraday(self, symbols: list[str]) -> dict[str, dict]:
        out = {}

        def one(s):
            try:
                return s, self.intraday(s)
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{s}: {e}")
                return s, None

        with ThreadPoolExecutor(self.cfg.FETCH_WORKERS) as ex:
            for s, d in ex.map(one, symbols):
                if d:
                    out[s] = d
        return out

    # --- Nasdaq: spread / earnings / halts --------------------------------------
    def bid_ask(self, sym: str, is_etf: bool) -> tuple[float | None, float | None]:
        url = NASDAQ_INFO.format(sym=urllib.parse.quote(sym.replace("-", ".")), cls="etf" if is_etf else "stocks")
        try:
            d = self.http.get_json(url, {"Accept": "application/json"})
            pd = (d.get("data") or {}).get("primaryData") or {}

            def num(x):
                try:
                    return float(str(x).replace("$", "").replace(",", ""))
                except (TypeError, ValueError):
                    return None
            return num(pd.get("bidPrice")), num(pd.get("askPrice"))
        except Exception:  # noqa: BLE001
            return None, None

    def earnings_calendar(self, days_ahead: int) -> dict[str, date]:
        today = datetime.now(self.tz).date()
        if self._earnings_day == today:
            return self._earnings
        out: dict[str, date] = {}
        d, n = today, 0
        while n <= days_ahead:
            if self.clock.is_trading_day(d):
                try:
                    js = self.http.get_json(NASDAQ_EARNINGS.format(d=d.isoformat()), {"Accept": "application/json"})
                    for row in ((js.get("data") or {}).get("rows") or []):
                        sym = (row.get("symbol") or "").replace(".", "-").replace("/", "-")
                        if sym and sym not in out:
                            out[sym] = d
                except Exception as e:  # noqa: BLE001
                    self.errors.append(f"earnings {d}: {e}")
                n += 1
            d += timedelta(days=1)
        self._earnings, self._earnings_day = out, today
        return out

    def halted_symbols(self) -> set[str]:
        try:
            xml = self.http.get_text(HALTS_RSS).lstrip("﻿")
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"halts: {e}")
            return set()
        return parse_halts(xml, datetime.now(self.tz))

    # --- samlet snapshot --------------------------------------------------------
    def snapshots(self, universe: dict[str, dict], daily: dict[str, list[Bar]],
                  with_spread_for: set[str] | None = None) -> dict[str, Snapshot]:
        st = self.clock.status()
        syms = [s for s in universe if s in daily]
        intr = self.bulk_intraday(syms)
        earnings = self.earnings_calendar(self.cfg.EARNINGS_BLACKOUT_DAYS + 1)
        halts = self.halted_symbols()
        today = st.now_market.date().isoformat()
        out = {}
        for s in syms:
            i = intr.get(s)
            if not i:
                continue
            if i.get("bar_date") and i["bar_date"] != today and st.session == "regular":
                continue  # forældet intradag-data
            meta = universe[s]
            out[s] = Snapshot(
                symbol=s, sector=meta["sector"], is_etf=meta["etf"], price=i["price"],
                open=i["open"], prev_close=i["prev_close"], day_high=i["day_high"],
                day_low=i["day_low"], volume_today=i["volume"], day_fraction=st.day_fraction,
                bars=daily[s], earnings_date=earnings.get(s), halted=s in halts,
                last_trade_age_s=i["last_trade_age_s"], asof=st.now_market.isoformat())
        return out

    def add_spreads(self, snaps: dict[str, Snapshot], symbols: list[str]):
        for s in symbols:
            sn = snaps.get(s)
            if sn and sn.bid is None:
                sn.bid, sn.ask = self.bid_ask(s, sn.is_etf)


def parse_halts(xml: str, now_market: datetime) -> set[str]:
    """Symboler der er stoppet nu (stop i dag uden passeret genoptagelsestid)."""
    ns = {"ndaq": "http://www.nasdaqtrader.com/"}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return set()
    out = set()
    today = now_market.strftime("%m/%d/%Y")
    for item in root.iter("item"):
        def g(tag):
            el = item.find(f"ndaq:{tag}", ns)
            return (el.text or "").strip() if el is not None and el.text else ""
        sym = g("IssueSymbol")
        if not sym or g("HaltDate") != today:
            continue
        rdate, rtime = g("ResumptionDate"), g("ResumptionTradeTime")
        if not rtime:
            out.add(sym)
            continue
        try:
            resume = datetime.strptime(f"{rdate or today} {rtime[:8]}", "%m/%d/%Y %H:%M:%S").replace(
                tzinfo=now_market.tzinfo)
            if resume > now_market:
                out.add(sym)
        except ValueError:
            out.add(sym)
    return {s.replace(".", "-") for s in out}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
