"""Polymarket: Gamma API (markeder/events) + CLOB API (orderbog, prishistorik). Kun læsning."""
from __future__ import annotations

import json

from .. import config
from ..http_client import get_json, HttpError
from ..models import Market, Book
from ..util import parse_ts, fnum, classify_topic

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
PAGE = 50  # gamma svarer 406 på store sider


def _jl(x):
    if isinstance(x, list):
        return x
    try:
        return json.loads(x or "[]")
    except (TypeError, ValueError):
        return []


def parse_market(m: dict, event: dict | None = None) -> Market | None:
    outcomes = _jl(m.get("outcomes"))
    prices = [fnum(p) for p in _jl(m.get("outcomePrices"))]
    tokens = _jl(m.get("clobTokenIds"))
    if len(outcomes) != 2 or len(prices) != 2 or not tokens:
        return None
    ev = event or ((m.get("events") or [None])[0]) or {}
    labels = [t.get("label") for t in (ev.get("tags") or []) if isinstance(t, dict)]
    if m.get("category"):
        labels.append(m["category"])
    bid = fnum(m.get("bestBid"), None) if m.get("bestBid") not in (None, "") else None
    ask = fnum(m.get("bestAsk"), None) if m.get("bestAsk") not in (None, "") else None
    if bid is not None and ask is not None and 0 < bid <= ask < 1:
        price = (bid + ask) / 2
    elif m.get("lastTradePrice") not in (None, ""):
        price = fnum(m.get("lastTradePrice"), prices[0])
    else:
        price = prices[0]
    chg = m.get("oneDayPriceChange")
    title = m.get("question") or ev.get("title") or ""
    if [o.lower() for o in outcomes] != ["yes", "no"]:
        title = f"{title} (JA = {outcomes[0]})"
    closed = bool(m.get("closed"))
    result = None
    status = "open" if m.get("active", True) and not closed else "closed"
    if closed and prices in ([1.0, 0.0], [0.0, 1.0], [0.5, 0.5]):
        result = prices[0]
        status = "resolved"
    fee_rate, fee_exp = config.POLYMARKET_DEFAULT_FEE_RATE, 1.0
    fs = m.get("feeSchedule")
    if m.get("feesEnabled") and isinstance(fs, dict):
        fee_rate, fee_exp = fnum(fs.get("rate")), fnum(fs.get("exponent"), 1.0)
    uma = str(m.get("umaResolutionStatus") or "").lower()
    slug = ev.get("slug") or m.get("slug") or ""
    return Market(
        platform="polymarket", market_id=str(m.get("id")), title=title,
        event_title=ev.get("title") or "", rules=m.get("description") or ev.get("description") or "",
        topic=classify_topic(labels, title), close_ts=parse_ts(m.get("endDate") or ev.get("endDate")),
        created_ts=parse_ts(m.get("startDate") or m.get("createdAt")),
        yes_price=price, best_bid=bid, best_ask=ask,
        price_24h_ago=(price - fnum(chg)) if chg not in (None, "") else None,
        volume=fnum(m.get("volumeNum") or m.get("volume")), volume_24h=fnum(m.get("volume24hr")),
        liquidity=fnum(m.get("liquidityNum") or m.get("liquidity")), status=status, result=result,
        yes_token=str(tokens[0]), fee_rate=fee_rate, fee_exponent=fee_exp,
        url=f"https://polymarket.com/event/{slug}" if slug else "",
        resolving=uma in ("proposed", "disputed", "challenged"),
    )


def list_open(max_pages: int | None = None) -> list[Market]:
    out: list[Market] = []
    for page in range(max_pages or config.SCOUT_MAX_PAGES_POLYMARKET):
        events = get_json(f"{GAMMA}/events", {
            "closed": "false", "active": "true", "archived": "false", "limit": PAGE,
            "offset": page * PAGE, "order": "volume24hr", "ascending": "false"})
        if not events:
            break
        for ev in events:
            for m in ev.get("markets") or []:
                if m.get("closed") or not m.get("enableOrderBook", True) or m.get("active") is False:
                    continue
                mk = parse_market(m, ev)
                if mk:
                    out.append(mk)
    return out


def get_book(market: Market) -> Book:
    d = get_json(f"{CLOB}/book", {"token_id": market.yes_token})
    return Book(bids=[(b["price"], b["size"]) for b in d.get("bids") or []],
                asks=[(a["price"], a["size"]) for a in d.get("asks") or []])


def fetch_markets(ids: list[str]) -> list[Market]:
    out = []
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        seen = set()
        for closed in ("false", "true"):
            try:
                rows = get_json(f"{GAMMA}/markets", [("id", x) for x in chunk] + [("closed", closed), ("limit", 50)])
            except HttpError:
                continue
            for m in rows or []:
                if str(m.get("id")) in seen:
                    continue
                mk = parse_market(m)
                if mk:
                    seen.add(mk.market_id)
                    out.append(mk)
    return out


def price_history(token: str, start_ts: float, end_ts: float, fidelity_min: int = 60) -> list[tuple[float, float]]:
    d = get_json(f"{CLOB}/prices-history", {"market": token, "startTs": int(start_ts),
                                           "endTs": int(end_ts), "fidelity": fidelity_min})
    return [(float(h["t"]), float(h["p"])) for h in d.get("history") or []]


def list_resolved(start_ts: float, end_ts: float, max_markets: int, min_volume: float) -> list[Market]:
    from ..util import iso
    out: list[Market] = []
    offset = 0
    while len(out) < max_markets and offset < 5000:
        events = get_json(f"{GAMMA}/events", {
            "closed": "true", "limit": PAGE, "offset": offset, "order": "volume", "ascending": "false",
            "end_date_min": iso(start_ts), "end_date_max": iso(end_ts)})
        if not events:
            break
        offset += PAGE
        for ev in events:
            for m in ev.get("markets") or []:
                mk = parse_market(m, ev)
                if (mk and mk.status == "resolved" and mk.result in (0.0, 1.0)
                        and mk.volume >= min_volume and start_ts <= mk.close_ts <= end_ts):
                    out.append(mk)
    out.sort(key=lambda m: -m.volume)
    return out[:max_markets]
