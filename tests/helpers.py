"""Fælles testdata: syntetiske markeder og orderbøger (ingen netværk)."""
import time

from desk.models import Market, Book

NOW = 1_800_000_000.0
RULES = ("This market resolves Yes if the stated event occurs before the close date according to the "
         "official source. Otherwise it resolves No.")


def mk(mid="1", platform="polymarket", price=0.5, hours=48.0, volume=100_000, vol24=10_000, topic="politik",
       title="Will X happen?", p24=None, spread=0.02, created_days=30, **kw) -> Market:
    return Market(platform=platform, market_id=mid, title=title, event_title=kw.pop("event_title", ""),
                  rules=kw.pop("rules", RULES), topic=topic, close_ts=NOW + hours * 3600,
                  created_ts=NOW - created_days * 86400, yes_price=price, best_bid=round(price - spread / 2, 4),
                  best_ask=round(price + spread / 2, 4), price_24h_ago=p24, volume=volume, volume_24h=vol24,
                  liquidity=50_000, fee_rate=kw.pop("fee_rate", 0.0), **kw)


def book(price=0.5, spread=0.02, size=5_000) -> Book:
    b, a = round(price - spread / 2, 4), round(price + spread / 2, 4)
    return Book(bids=[(b, size), (b - 0.01, size), (b - 0.03, size * 3)],
                asks=[(a, size), (a + 0.01, size), (a + 0.03, size * 3)])
