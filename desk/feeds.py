"""Markedsdata-feeds med fælles interface.

* BinanceFeed / BybitFeed: live, offentlige REST-endpoints + likvidations-websocket.
* HistoricalFeed (i backtest.py) implementerer samme interface til backtest.

Agenterne kender kun interfacet `MarketFeed` og ved ikke, om data er live.
"""
from __future__ import annotations

import collections
import json
import logging
import threading
import time
from typing import Iterable

from . import config
from .models import Kline, MarketSnapshot, OrderBook
from .net import HttpError, WebSocket, get_json

log = logging.getLogger("desk.feed")
HOUR_MS = 3_600_000


class MarketFeed:
    """Interface. Alle tider i millisekunder (UTC)."""

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def universe(self) -> list[MarketSnapshot]:
        raise NotImplementedError

    def mark_prices(self) -> dict[str, float]:
        raise NotImplementedError

    def funding_rates(self) -> dict[str, float]:
        raise NotImplementedError

    def orderbook(self, symbol: str) -> OrderBook:
        raise NotImplementedError

    def klines(self, symbol: str, limit: int = 120) -> list[Kline]:
        raise NotImplementedError

    def ticker(self) -> tuple[dict[str, float], dict[str, float]]:
        """(markpriser, funding-rater) - overskrives hvor ét kald kan give begge."""
        return self.mark_prices(), self.funding_rates()

    def minute_range(self, symbol: str, since_ms: int) -> tuple[float, float, float] | None:
        """(åbning, høj, lav) siden `since_ms` fra 1m-candles, eller None."""
        return None


class LiquidationTracker:
    """Rullende buffer af likvidationer fra en websocket i en baggrundstråd."""

    def __init__(self, url: str, subscribe: Iterable[str] = (), parser=None,
                 ping_payload: str | None = None, window_ms: int = 4 * HOUR_MS):
        self.url = url
        self.subscribe = list(subscribe)
        self.parser = parser
        self.ping_payload = ping_payload
        self.window_ms = window_ms
        self.events: collections.deque = collections.deque()
        self.lock = threading.Lock()
        self.connected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="liq-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def add(self, ts: int, symbol: str, side: str, usd: float) -> None:
        with self.lock:
            self.events.append((ts, symbol, side, usd))
            cutoff = ts - self.window_ms
            while self.events and self.events[0][0] < cutoff:
                self.events.popleft()

    def totals(self, now_ms: int, window_ms: int = HOUR_MS) -> dict[str, tuple[float, float]]:
        """symbol -> (likviderede longs USD, likviderede shorts USD)."""
        out: dict[str, list[float]] = collections.defaultdict(lambda: [0.0, 0.0])
        with self.lock:
            for ts, sym, side, usd in self.events:
                if ts >= now_ms - window_ms:
                    out[sym][0 if side == "long" else 1] += usd
        return {k: (v[0], v[1]) for k, v in out.items()}

    def _run(self) -> None:
        backoff = 2
        while not self._stop.is_set():
            ws = WebSocket(self.url)
            try:
                ws.connect()
                for i in range(0, len(self.subscribe), 10):
                    ws.send(json.dumps({"op": "subscribe", "args": self.subscribe[i:i + 10]}))
                self.connected = True
                backoff = 2
                last_ping = time.time()
                while not self._stop.is_set():
                    if self.ping_payload and time.time() - last_ping > 20:
                        ws.send(self.ping_payload)
                        last_ping = time.time()
                    msg = ws.recv()
                    if msg is None:
                        break
                    for ev in self.parser(msg) if self.parser else ():
                        self.add(*ev)
            except Exception as e:  # noqa: BLE001 - genforbind altid
                log.warning("likvidations-websocket: %s", e)
            finally:
                self.connected = False
                ws.close()
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 120)


# --------------------------------------------------------------------- Binance
def _binance_liq_parser(msg: str):
    try:
        d = json.loads(msg)
        o = d.get("o") or {}
        qty = float(o.get("z") or o.get("q") or 0)
        px = float(o.get("ap") or o.get("p") or 0)
        side = "long" if o.get("S") == "SELL" else "short"   # SELL = long likvideret
        yield int(o.get("T") or d.get("E") or time.time() * 1000), o["s"], side, qty * px
    except (ValueError, KeyError, TypeError):
        return


class BinanceFeed(MarketFeed):
    BASE = "https://fapi.binance.com"
    WS = "wss://fstream.binance.com/ws/!forceOrder@arr"

    def __init__(self, start_ws: bool = True):
        self._symbols: list[str] = []
        self._symbols_at = 0.0
        self._ls_cache: dict[str, tuple[float, float]] = {}
        self.liqs = LiquidationTracker(self.WS, parser=_binance_liq_parser)
        if start_ws:
            self.liqs.start()

    def _perp_symbols(self) -> set[str]:
        if not self._symbols or time.time() - self._symbols_at > 3600:
            info = get_json(f"{self.BASE}/fapi/v1/exchangeInfo")
            self._symbols = [s["symbol"] for s in info["symbols"]
                             if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING"
                             and s.get("quoteAsset") == config.UNIVERSE_QUOTE]
            self._symbols_at = time.time()
        return set(self._symbols)

    def _premium(self) -> list[dict]:
        return get_json(f"{self.BASE}/fapi/v1/premiumIndex")

    def mark_prices(self) -> dict[str, float]:
        return {d["symbol"]: float(d["markPrice"]) for d in self._premium()}

    def funding_rates(self) -> dict[str, float]:
        return {d["symbol"]: float(d["lastFundingRate"] or 0) for d in self._premium()}

    def ticker(self):
        prem = self._premium()
        return ({d["symbol"]: float(d["markPrice"]) for d in prem},
                {d["symbol"]: float(d["lastFundingRate"] or 0) for d in prem})

    def minute_range(self, symbol, since_ms):
        ks = [k for k in self.klines(symbol, 10, interval="1m") if k.open_time + 60_000 > since_ms]
        if not ks:
            return None
        return ks[0].open, max(k.high for k in ks), min(k.low for k in ks)

    def universe(self) -> list[MarketSnapshot]:
        perps = self._perp_symbols()
        prem = {d["symbol"]: d for d in self._premium()}
        tick = [t for t in get_json(f"{self.BASE}/fapi/v1/ticker/24hr") if t["symbol"] in perps]
        tick.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
        now = self.now_ms()
        liq = self.liqs.totals(now)
        out = []
        for t in tick[:config.UNIVERSE_TOP_N]:
            sym = t["symbol"]
            p = prem.get(sym)
            if not p:
                continue
            snap = MarketSnapshot(
                symbol=sym, ts=now, mark_price=float(p["markPrice"]),
                last_price=float(t["lastPrice"]),
                funding_rate=float(p.get("lastFundingRate") or 0),
                next_funding_ms=int(p.get("nextFundingTime") or 0),
                volume_24h_usd=float(t["quoteVolume"]),
                change_24h=float(t["priceChangePercent"]) / 100,
            )
            try:
                hist = get_json(f"{self.BASE}/futures/data/openInterestHist",
                                {"symbol": sym, "period": "5m", "limit": 49})
                vals = [float(h["sumOpenInterestValue"]) for h in hist]
                if vals:
                    snap.open_interest_usd = vals[-1]
                    if len(vals) > 12 and vals[-13]:
                        snap.oi_change_1h = vals[-1] / vals[-13] - 1
                    if vals[0]:
                        snap.oi_change_4h = vals[-1] / vals[0] - 1
            except HttpError as e:
                log.debug("OI %s: %s", sym, e)
            snap.long_short_ratio = self._ls_ratio(sym)
            snap.liq_long_usd_1h, snap.liq_short_usd_1h = liq.get(sym, (0.0, 0.0))
            out.append(snap)
        return out

    def _ls_ratio(self, sym: str) -> float:
        cached = self._ls_cache.get(sym)
        if cached and time.time() - cached[1] < 300:
            return cached[0]
        try:
            d = get_json(f"{self.BASE}/futures/data/globalLongShortAccountRatio",
                         {"symbol": sym, "period": "5m", "limit": 1})
            r = float(d[-1]["longShortRatio"]) if d else 1.0
        except (HttpError, KeyError, ValueError):
            r = 1.0
        self._ls_cache[sym] = (r, time.time())
        return r

    def orderbook(self, symbol: str) -> OrderBook:
        d = get_json(f"{self.BASE}/fapi/v1/depth", {"symbol": symbol, "limit": 100})
        return OrderBook(bids=[(float(p), float(q)) for p, q in d["bids"]],
                         asks=[(float(p), float(q)) for p, q in d["asks"]])

    def klines(self, symbol: str, limit: int = 120, interval: str = "1h",
               start_ms: int | None = None) -> list[Kline]:
        params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1500)}
        if start_ms is not None:
            params["startTime"] = start_ms
        d = get_json(f"{self.BASE}/fapi/v1/klines", params)
        return [Kline(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                      float(k[5]), float(k[7])) for k in d]

    def funding_history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        out, cur = [], start_ms
        while cur < end_ms:
            d = get_json(f"{self.BASE}/fapi/v1/fundingRate",
                         {"symbol": symbol, "startTime": cur, "endTime": end_ms, "limit": 1000})
            if not d:
                break
            out += [(int(x["fundingTime"]), float(x["fundingRate"])) for x in d]
            cur = int(d[-1]["fundingTime"]) + 1
            if len(d) < 1000:
                break
        return out

    def oi_history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        """1t OI-værdi (Binance gemmer kun ca. 30 dage)."""
        out, cur = [], max(start_ms, end_ms - 29 * 24 * HOUR_MS)
        while cur < end_ms:
            try:
                d = get_json(f"{self.BASE}/futures/data/openInterestHist",
                             {"symbol": symbol, "period": "1h", "limit": 500,
                              "startTime": cur, "endTime": end_ms})
            except HttpError:
                break
            if not d:
                break
            out += [(int(x["timestamp"]), float(x["sumOpenInterestValue"])) for x in d]
            cur = int(d[-1]["timestamp"]) + 1
            if len(d) < 500:
                break
        return out

    def ls_history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        out, cur = [], max(start_ms, end_ms - 29 * 24 * HOUR_MS)
        while cur < end_ms:
            try:
                d = get_json(f"{self.BASE}/futures/data/globalLongShortAccountRatio",
                             {"symbol": symbol, "period": "1h", "limit": 500,
                              "startTime": cur, "endTime": end_ms})
            except HttpError:
                break
            if not d:
                break
            out += [(int(x["timestamp"]), float(x["longShortRatio"])) for x in d]
            cur = int(d[-1]["timestamp"]) + 1
            if len(d) < 500:
                break
        return out


# ----------------------------------------------------------------------- Bybit
def _bybit_liq_parser(msg: str):
    try:
        d = json.loads(msg)
        if not str(d.get("topic", "")).startswith("allLiquidation"):
            return
        for x in d.get("data", []):
            side = "long" if x.get("S") == "Buy" else "short"   # Buy = long likvideret
            yield int(x["T"]), x["s"], side, float(x["v"]) * float(x["p"])
    except (ValueError, KeyError, TypeError):
        return


class BybitFeed(MarketFeed):
    BASE = "https://api.bybit.com"
    WS = "wss://stream.bybit.com/v5/public/linear"

    def __init__(self, start_ws: bool = True):
        self._ls_cache: dict[str, tuple[float, float]] = {}
        self._start_ws = start_ws
        self.liqs: LiquidationTracker | None = None

    def _tickers(self) -> list[dict]:
        d = get_json(f"{self.BASE}/v5/market/tickers", {"category": "linear"})
        return [t for t in d["result"]["list"] if t["symbol"].endswith(config.UNIVERSE_QUOTE)]

    def _ensure_ws(self, symbols: list[str]) -> None:
        if self.liqs is None and self._start_ws:
            self.liqs = LiquidationTracker(self.WS, [f"allLiquidation.{s}" for s in symbols],
                                           parser=_bybit_liq_parser,
                                           ping_payload=json.dumps({"op": "ping"}))
            self.liqs.start()

    def mark_prices(self) -> dict[str, float]:
        return {t["symbol"]: float(t["markPrice"]) for t in self._tickers()}

    def funding_rates(self) -> dict[str, float]:
        return {t["symbol"]: float(t.get("fundingRate") or 0) for t in self._tickers()}

    def ticker(self):
        tick = self._tickers()
        return ({t["symbol"]: float(t["markPrice"]) for t in tick},
                {t["symbol"]: float(t.get("fundingRate") or 0) for t in tick})

    def minute_range(self, symbol, since_ms):
        ks = [k for k in self.klines(symbol, 10, interval="1") if k.open_time + 60_000 > since_ms]
        if not ks:
            return None
        return ks[0].open, max(k.high for k in ks), min(k.low for k in ks)

    def universe(self) -> list[MarketSnapshot]:
        tick = self._tickers()
        tick.sort(key=lambda t: float(t.get("turnover24h") or 0), reverse=True)
        tick = [t for t in tick if t.get("fundingRate")][:config.UNIVERSE_TOP_N]
        self._ensure_ws([t["symbol"] for t in tick])
        now = self.now_ms()
        liq = self.liqs.totals(now) if self.liqs else {}
        out = []
        for t in tick:
            sym = t["symbol"]
            snap = MarketSnapshot(
                symbol=sym, ts=now, mark_price=float(t["markPrice"]),
                last_price=float(t["lastPrice"]), funding_rate=float(t["fundingRate"]),
                next_funding_ms=int(t.get("nextFundingTime") or 0),
                volume_24h_usd=float(t.get("turnover24h") or 0),
                change_24h=float(t.get("price24hPcnt") or 0),
                open_interest_usd=float(t.get("openInterestValue") or 0),
            )
            try:
                d = get_json(f"{self.BASE}/v5/market/open-interest",
                             {"category": "linear", "symbol": sym, "intervalTime": "5min",
                              "limit": 49})
                vals = [float(x["openInterest"]) for x in d["result"]["list"]][::-1]
                if len(vals) > 12 and vals[-13]:
                    snap.oi_change_1h = vals[-1] / vals[-13] - 1
                if vals and vals[0]:
                    snap.oi_change_4h = vals[-1] / vals[0] - 1
            except (HttpError, KeyError, ValueError) as e:
                log.debug("OI %s: %s", sym, e)
            snap.long_short_ratio = self._ls_ratio(sym)
            snap.liq_long_usd_1h, snap.liq_short_usd_1h = liq.get(sym, (0.0, 0.0))
            out.append(snap)
        return out

    def _ls_ratio(self, sym: str) -> float:
        cached = self._ls_cache.get(sym)
        if cached and time.time() - cached[1] < 300:
            return cached[0]
        try:
            d = get_json(f"{self.BASE}/v5/market/account-ratio",
                         {"category": "linear", "symbol": sym, "period": "5min", "limit": 1})
            x = d["result"]["list"][0]
            sell = float(x["sellRatio"])
            r = float(x["buyRatio"]) / sell if sell else 1.0
        except (HttpError, KeyError, ValueError, IndexError):
            r = 1.0
        self._ls_cache[sym] = (r, time.time())
        return r

    def orderbook(self, symbol: str) -> OrderBook:
        d = get_json(f"{self.BASE}/v5/market/orderbook",
                     {"category": "linear", "symbol": symbol, "limit": 200})["result"]
        return OrderBook(bids=[(float(p), float(q)) for p, q in d["b"]],
                         asks=[(float(p), float(q)) for p, q in d["a"]])

    def klines(self, symbol: str, limit: int = 120, interval: str = "60",
               start_ms: int | None = None) -> list[Kline]:
        params = {"category": "linear", "symbol": symbol, "interval": interval,
                  "limit": min(limit, 1000)}
        if start_ms is not None:
            params["start"] = start_ms
        d = get_json(f"{self.BASE}/v5/market/kline", params)["result"]["list"][::-1]
        return [Kline(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                      float(k[5]), float(k[6])) for k in d]


def make_feed(start_ws: bool = True) -> MarketFeed:
    if config.EXCHANGE.lower() == "bybit":
        return BybitFeed(start_ws=start_ws)
    return BinanceFeed(start_ws=start_ws)
