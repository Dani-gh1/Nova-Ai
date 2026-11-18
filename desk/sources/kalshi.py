"""Kalshi: offentligt trade-api v2 (ingen nøgle). Kun læsning."""
from __future__ import annotations

from .. import config
from ..http_client import get_json, HttpError
from ..models import Market, Book
from ..util import parse_ts, fnum, classify_topic

API = "https://api.elections.kalshi.com/trade-api/v2"


def _d(m: dict, key: str):
    """Kalshi har både *_dollars-felter og ældre cent-felter."""
    if m.get(key + "_dollars") not in (None, ""):
        return fnum(m[key + "_dollars"])
    if m.get(key) not in (None, ""):
        return fnum(m[key]) / 100.0
    return None


def _q(m: dict, key: str) -> float:
    if m.get(key + "_fp") not in (None, ""):
        return fnum(m[key + "_fp"])
    return fnum(m.get(key))


def parse_market(m: dict, event: dict | None = None) -> Market | None:
    if m.get("market_type", "binary") != "binary":
        return None
    ev = event or {}
    bid, ask, last, prev = _d(m, "yes_bid"), _d(m, "yes_ask"), _d(m, "last_price"), _d(m, "previous_price")
    if bid is not None and ask is not None and 0 < bid <= ask < 1:
        price = (bid + ask) / 2
    elif last:
        price = last
    else:
        price = bid or ask or 0.5
    sub = m.get("yes_sub_title") or m.get("subtitle") or ""
    ev_title = ev.get("title") or ""
    title = m.get("title") or ev_title
    if sub and sub.lower() not in title.lower():
        title = f"{title}: {sub}"
    close = parse_ts(m.get("close_time"))
    exp = parse_ts(m.get("expected_expiration_time"))
    if exp and close and exp < close:
        close = exp  # forventet afgørelse ligger før formel lukning (fx sport)
    status_raw = (m.get("status") or "").lower()
    result_raw = (m.get("result") or "").lower()
    result = {"yes": 1.0, "no": 0.0, "void": 0.5}.get(result_raw)
    if status_raw in ("settled", "finalized", "determined") and result is not None:
        status = "resolved"
    elif status_raw in ("active", "open", "initialized"):
        status = "open"
    else:
        status = "closed"
    series = ev.get("series_ticker") or m.get("series_ticker") or ""
    return Market(
        platform="kalshi", market_id=m["ticker"], title=title, event_title=ev_title,
        rules=((m.get("rules_primary") or "") + "\n" + (m.get("rules_secondary") or "")).strip(),
        topic=classify_topic([ev.get("category")], f"{ev_title} {title}"),
        close_ts=close, created_ts=parse_ts(m.get("open_time") or m.get("created_time")),
        yes_price=price, best_bid=bid if bid else None, best_ask=ask if ask and ask < 1 else None,
        price_24h_ago=prev if prev else None,
        volume=_q(m, "volume"), volume_24h=_q(m, "volume_24h"),
        liquidity=fnum(m.get("liquidity_dollars")) or _q(m, "open_interest") * price,
        status=status, result=result if status == "resolved" else None,
        series=series, fee_rate=config.KALSHI_FEE_RATE,
        url=f"https://kalshi.com/markets/{series.lower()}" if series else "",
    )


def _events(status: str, max_pages: int, extra: dict | None = None):
    cursor = None
    for _ in range(max_pages):
        params = {"status": status, "with_nested_markets": "true", "limit": 200}
        params.update(extra or {})
        if cursor:
            params["cursor"] = cursor
        d = get_json(f"{API}/events", params)
        for ev in d.get("events") or []:
            yield ev
        cursor = d.get("cursor")
        if not cursor:
            break


def list_open(max_pages: int | None = None) -> list[Market]:
    out = []
    for ev in _events("open", max_pages or config.SCOUT_MAX_PAGES_KALSHI):
        for m in ev.get("markets") or []:
            mk = parse_market(m, ev)
            if mk and mk.status == "open":
                out.append(mk)
    return out


def get_book(market: Market) -> Book:
    d = get_json(f"{API}/markets/{market.market_id}/orderbook")
    ob = d.get("orderbook_fp") or {}
    if ob:
        yes = [(fnum(p), fnum(s)) for p, s in ob.get("yes_dollars") or []]
        no = [(fnum(p), fnum(s)) for p, s in ob.get("no_dollars") or []]
    else:  # ældre format i cent
        ob = d.get("orderbook") or {}
        yes = [(fnum(p) / 100, fnum(s)) for p, s in ob.get("yes") or []]
        no = [(fnum(p) / 100, fnum(s)) for p, s in ob.get("no") or []]
    return Book(bids=yes, asks=[(round(1 - p, 4), s) for p, s in no])


def fetch_markets(tickers: list[str]) -> list[Market]:
    out = []
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        try:
            d = get_json(f"{API}/markets", {"tickers": ",".join(chunk), "limit": 200})
        except HttpError:
            continue
        for m in d.get("markets") or []:
            mk = parse_market(m)
            if mk:
                out.append(mk)
    return out


def candles(series: str, ticker: str, start_ts: float, end_ts: float, period_min: int = 60) -> list[dict]:
    d = get_json(f"{API}/series/{series}/markets/{ticker}/candlesticks", {
        "start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": period_min})
    out = []
    for c in d.get("candlesticks") or []:
        price = c.get("price") or {}
        close = fnum(price.get("close_dollars"), None) if price.get("close_dollars") else None
        yb = fnum((c.get("yes_bid") or {}).get("close_dollars"), None)
        ya = fnum((c.get("yes_ask") or {}).get("close_dollars"), None)
        mid = (yb + ya) / 2 if yb is not None and ya is not None and 0 < yb <= ya < 1 else None
        p = mid if mid is not None else close
        if p is None:
            continue
        out.append({"t": float(c["end_period_ts"]), "p": p, "v": fnum(c.get("volume_fp"))})
    return out


def list_resolved(start_ts: float, end_ts: float, max_markets: int, min_volume: float,
                  max_pages: int = 40) -> list[Market]:
    out = []
    for ev in _events("settled", max_pages, {"min_close_ts": int(start_ts)}):
        for m in ev.get("markets") or []:
            mk = parse_market(m, ev)
            if (mk and mk.status == "resolved" and mk.result in (0.0, 1.0) and mk.volume >= min_volume
                    and start_ts <= mk.close_ts <= end_ts):
                out.append(mk)
    out.sort(key=lambda m: -m.volume)
    return out[:max_markets]
