"""Risk: lofter pr. marked og pr. emne, og udgang før afgørelse når kursen går imod.

Godkender/nedskalerer strategiernes ordrer og finder positioner der skal lukkes.
"""
from __future__ import annotations

import time

from ..accounts import Account, Position
from ..models import OrderIntent, Market, Book
from .analyst import exit_quote


class Risk:
    name = "risk"
    MIN_RESIZE = 0.25  # nedskalér ned til 25 % af ønsket størrelse, ellers afvis

    def approve(self, intents: list[OrderIntent], acct: Account, strat) -> tuple[list[OrderIntent], list]:
        approved, rejects = [], []
        eq = acct.equity()
        planned_m: dict[str, float] = {}
        planned_t: dict[str, float] = {}
        cash = acct.cash
        pairs_rejected: set[str] = set()
        for it in intents:
            key, topic = it.market.key, it.market.topic
            if it.pair_id and it.pair_id in pairs_rejected:
                rejects.append((key, "anden arbitrage-ben afvist"))
                continue
            if acct.holds(key) or (key in planned_m and not it.pair_id):
                rejects.append((key, "har allerede position i markedet"))
                continue
            want = eq * it.size_pct / 100.0
            room_m = eq * strat.p("max_pct_per_market") / 100.0 - acct.exposure_market(key) - planned_m.get(key, 0)
            room_t = eq * strat.p("max_pct_per_topic") / 100.0 - acct.exposure_topic(topic) - planned_t.get(topic, 0)
            size = min(want, room_m, room_t, cash)
            if size < want * self.MIN_RESIZE or size <= 0:
                why = ("loft pr. marked" if room_m <= min(room_t, cash) else
                       "loft pr. emne" if room_t <= cash else "ingen kontanter")
                rejects.append((key, why))
                if it.pair_id:
                    pairs_rejected.add(it.pair_id)
                    approved = [a for a in approved if a.pair_id != it.pair_id]
                continue
            it.size_pct = size / eq * 100.0 if eq > 0 else 0
            planned_m[key] = planned_m.get(key, 0) + size
            planned_t[topic] = planned_t.get(topic, 0) + size
            cash -= size
            approved.append(it)
        return approved, rejects

    def exits(self, acct: Account, strat, quotes: dict, now: float | None = None) -> list[tuple]:
        """quotes: market_key -> (Market, Book). Returnerer [(Position, Market, Book, årsag)]."""
        now = now or time.time()
        out = []
        stop = strat.p("stop_loss_pct")
        tp = strat.p("take_profit_pct")
        hb = strat.p("exit_hours_before_close")
        for pos in acct.positions:
            q = quotes.get(pos.market_key)
            if not q:
                continue
            m, book = q
            mid = (book.mid if book.mid is not None else m.yes_price)
            pos.mark = mid if pos.side == "YES" else 1 - mid
            if pos.pair_id:   # arbitrage holdes til afgørelse
                continue
            change = (pos.mark - pos.entry_price) / pos.entry_price * 100 if pos.entry_price else 0
            if stop and stop < 100 and change <= -stop:
                out.append((pos, m, book, f"stop-loss ({change:.0f} %)"))
            elif tp and change >= tp:
                out.append((pos, m, book, f"gevinstsikring (+{change:.0f} %)"))
            elif hb and (pos.close_ts - now) / 3600 <= hb:
                out.append((pos, m, book, "udgang før afgørelse"))
        return out
