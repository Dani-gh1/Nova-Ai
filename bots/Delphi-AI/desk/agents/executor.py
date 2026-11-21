"""Executor: fast % af kontoen, slippage gennem orderbogen, gebyrer, maks. åbne positioner.

Papirhandel: der sendes aldrig ordrer nogen steder. Fyld simuleres mod den offentlige orderbog.
"""
from __future__ import annotations

import time

from .. import config
from ..accounts import Account, Position, to_pct, to_usd
from ..models import OrderIntent, Fill, Market, Book
from ..util import fee_total


def walk(levels: list, notional: float | None = None, contracts: float | None = None,
         limit: float | None = None, sell: bool = False) -> tuple[float, float]:
    """Gå gennem prisniveauer. Returnerer (kontrakter, USD). Stopper ved limit-prisen."""
    got, spent = 0.0, 0.0
    for price, size in levels:
        if limit is not None and ((not sell and price > limit + 1e-9) or (sell and price < limit - 1e-9)):
            break
        if notional is not None:
            take = min(size, (notional - spent) / price) if price > 0 else 0
        else:
            take = min(size, contracts - got)
        if take <= 0:
            break
        got += take
        spent += take * price
        if (notional is not None and spent >= notional - 1e-9) or (contracts is not None and got >= contracts - 1e-9):
            break
    return got, spent


class Executor:
    name = "executor"

    def buy(self, it: OrderIntent, acct: Account, strat, now: float | None = None) -> tuple[Position | None, Fill]:
        now = now or time.time()
        if len(acct.positions) >= strat.p("max_positions"):
            return None, Fill(False, reason="maks. antal åbne positioner")
        if acct.entries_this_cycle >= strat.p("max_entries_per_cycle"):
            return None, Fill(False, reason="maks. nye handler i denne cyklus")
        stake = acct.equity() * it.size_pct / 100.0
        stake = min(stake, acct.cash)
        if stake <= 0.01:
            return None, Fill(False, reason="for lille indsats")
        levels = it.book.levels_for(it.side, "buy")
        if not levels:
            return None, Fill(False, reason="ingen udbud")
        best = levels[0][0]
        m = it.market
        # reservér plads til gebyr: find kontrakter for (indsats - forventet gebyr)
        fee_rate_guess = fee_total(m.platform, best, 1.0, m.fee_rate, m.fee_exponent)
        notional = to_usd(stake) / (1 + fee_rate_guess / max(best, 1e-6))
        got, spent = walk(levels, notional=notional, limit=best + config.EXEC_MAX_SLIPPAGE)
        if got <= 0 or spent < notional * config.EXEC_MIN_FILL_RATIO:
            return None, Fill(False, reason="for tynd orderbog til størrelsen")
        avg = spent / got
        fee = fee_total(m.platform, avg, got, m.fee_rate, m.fee_exponent)
        cost = to_pct(spent + fee)
        if cost > acct.cash + 1e-9:
            return None, Fill(False, reason="ingen kontanter")
        pos = Position(market_key=m.key, platform=m.platform, title=m.title, topic=m.topic, side=it.side,
                       entry_price=avg, contracts=got, cost=cost, fee=to_pct(fee), opened_ts=now,
                       close_ts=m.close_ts, slippage=avg - best, p_fair=it.p_fair, reason=it.reason,
                       pair_id=it.pair_id, url=m.url, mark=avg)
        acct.cash -= cost
        acct.positions.append(pos)
        acct.entries_this_cycle += 1
        return pos, Fill(True, avg, got, spent + fee, fee, avg - best)

    def sell(self, pos: Position, m: Market, book: Book, acct: Account) -> tuple[float, float]:
        """Sælg hele positionen ind i orderbogen. Rest under limit værdisættes til limit-prisen.
        Returnerer (snitpris, provenu i pct)."""
        levels = book.levels_for(pos.side, "sell")
        best = levels[0][0] if levels else (pos.mark or pos.entry_price)
        floor = max(0.0, best - 2 * config.EXEC_MAX_SLIPPAGE)
        got, usd = walk(levels, contracts=pos.contracts, limit=floor, sell=True)
        if got < pos.contracts:
            usd += (pos.contracts - got) * floor
        avg = usd / pos.contracts if pos.contracts else 0.0
        fee = fee_total(m.platform, avg, pos.contracts, m.fee_rate, m.fee_exponent)
        proceeds = to_pct(max(0.0, usd - fee))
        acct.cash += proceeds
        acct.positions.remove(pos)
        return avg, proceeds

    @staticmethod
    def settle(pos: Position, result: float, acct: Account) -> float:
        """Udbetaling ved afgørelse: 1 (100 %) eller 0 pr. kontrakt. 0.5 = annulleret."""
        payout = result if pos.side == "YES" else 1 - result
        proceeds = to_pct(pos.contracts * payout)
        acct.cash += proceeds
        if pos in acct.positions:
            acct.positions.remove(pos)
        return proceeds
