"""Executor: papir-udførsel. Fast risiko i % pr. handel, spread/glid/swap, maks. positioner,
karantæne efter lukning, og styring af åbne positioner (stop, trailing, mål, weekend)."""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta

from .. import accounts, config, market
from ..models import Account, ExecutionResult, Position, RiskDecision

_ids = itertools.count(1)
_M15 = timedelta(minutes=15)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


class Executor:
    name = "executor"

    # ---------------------------------------------------------------- åbning
    def run(self, decision: RiskDecision, acc: Account, params, quotes: dict,
            now: datetime) -> ExecutionResult:
        def no(reason):
            return ExecutionResult(decision, False, reason)

        if not decision.approved:
            return no("afvist af Risk: " + decision.reason)
        intent = decision.intent
        sym = intent.signal.instrument
        if len(acc.positions) >= params.max_positions:
            return no(f"maks. {params.max_positions} åbne positioner")
        until = acc.quarantine.get(sym)
        if until and until > _iso(now):
            return no(f"karantæne til {until[11:16]} UTC")
        q = quotes.get(sym)
        if q is None:
            return no("ingen kurs")
        ins = market.instrument(sym)
        eq = accounts.equity(acc, quotes)
        uq = accounts.usd_per_unit(ins.quote, quotes)
        risk_usd = eq * decision.risk_pct / 100.0
        units = risk_usd / (decision.stop_distance * uq)
        # gearingsloft pr. instrument og nominel eksponering pr. valuta
        notional = accounts.notional_usd(units, sym, q.mid, quotes)
        max_notional = ins.max_leverage * eq * 0.5
        if notional > max_notional:
            units *= max_notional / notional
            notional = max_notional
        exp = accounts.currency_exposure(acc, quotes, eq)
        s = accounts.sign(intent.direction)
        for ccy, k in ((ins.base, s), (ins.quote, -s)):
            n = exp.get(ccy, (0.0, 0.0))[1] + k * notional / eq
            if abs(n) > config.MAX_CCY_NOTIONAL_X:
                return no(f"nominel {ccy}-eksponering {abs(n):.1f}x > {config.MAX_CCY_NOTIONAL_X}x")
        if units <= 0:
            return no("størrelse nul")
        entry = q.ask if intent.direction == "long" else q.bid
        stop = entry - s * decision.stop_distance
        tp = entry + s * decision.take_profit_distance if decision.take_profit_distance else None
        actual_risk_pct = units * decision.stop_distance * uq / eq * 100
        pos = Position(
            id=f"{intent.strategy}-{now:%Y%m%d%H%M}-{next(_ids)}", strategy=intent.strategy,
            instrument=sym, direction=intent.direction, units=round(units, 4), entry=entry,
            stop=stop, initial_stop=stop, take_profit=tp, trail_distance=decision.trail_distance,
            trail_activate=decision.trail_activate, opened_at=_iso(now),
            risk_pct=round(actual_risk_pct, 4), score=intent.score, best_price=entry,
            last_checked=_iso(now))
        acc.positions.append(pos)
        cost = accounts.pct(units * q.spread * uq)
        return ExecutionResult(decision, True, "udført (papir)", pos.id, entry, pos.units, cost)

    # ---------------------------------------------------------------- styring
    def manage(self, acc: Account, md, quotes: dict, now: datetime, params, news=None) -> list:
        closed = []
        mtc = market.minutes_to_close(now)
        for pos in list(acc.positions):
            q = quotes.get(pos.instrument)
            if q is None:
                continue
            hs = q.spread / 2
            slip = q.spread * config.SLIPPAGE_SPREAD_FRAC
            s = accounts.sign(pos.direction)
            exit_px, why = None, ""
            last = datetime.fromisoformat(pos.last_checked)
            for c in md.candles(pos.instrument, "M15")[-40:]:
                close_t = c.time + _M15
                if close_t <= last or close_t > now:
                    continue
                if s > 0:
                    lo, hi, op = c.low - hs, c.high - hs, c.open - hs
                    if lo <= pos.stop:
                        exit_px, why = min(pos.stop, op) - slip, self._stop_label(pos)
                    elif pos.take_profit and hi >= pos.take_profit:
                        exit_px, why = pos.take_profit, "gevinstmål"
                    else:
                        pos.best_price = max(pos.best_price, hi)
                else:
                    lo, hi, op = c.low + hs, c.high + hs, c.open + hs
                    if hi >= pos.stop:
                        exit_px, why = max(pos.stop, op) + slip, self._stop_label(pos)
                    elif pos.take_profit and lo <= pos.take_profit:
                        exit_px, why = pos.take_profit, "gevinstmål"
                    else:
                        pos.best_price = min(pos.best_price, lo)
                if exit_px is not None:
                    break
                if pos.trail_distance and s * (pos.best_price - pos.entry) >= pos.trail_activate:
                    new = pos.best_price - s * pos.trail_distance
                    if s * (new - pos.stop) > 0:
                        pos.stop = new
            pos.last_checked = _iso(now)
            if exit_px is None:
                held_h = (now - datetime.fromisoformat(pos.opened_at)).total_seconds() / 3600
                if mtc is not None and mtc <= config.CLOSE_BEFORE_WEEKEND_MIN and market.ny(now).weekday() == 4:
                    exit_px, why = accounts.exit_price(pos.direction, q), "lukket før weekend"
                elif held_h >= params.max_hold_hours:
                    exit_px, why = accounts.exit_price(pos.direction, q), "maks. holdetid"
                elif news is not None and params.flat_before_news_min:
                    near = news.nearest(pos.instrument, now)
                    if near and 0 <= near[0] <= params.flat_before_news_min:
                        exit_px, why = accounts.exit_price(pos.direction, q), f"lukket før nyhed: {near[1].title}"
            if exit_px is not None:
                closed.append(self.close(acc, pos, exit_px, why, quotes, now, params))
        return closed

    @staticmethod
    def _stop_label(pos: Position) -> str:
        return "trailing stop" if pos.stop != pos.initial_stop else "stop"

    def close(self, acc: Account, pos: Position, price: float, why: str, quotes: dict,
              now: datetime, params) -> dict:
        gross = accounts.pnl_usd(pos, price, quotes)
        realized = gross + pos.swap_paid
        acc.balance += realized
        acc.positions.remove(pos)
        acc.trades += 1
        if realized > 0:
            acc.wins += 1
        acc.quarantine[pos.instrument] = _iso(now + timedelta(minutes=params.quarantine_min))
        risk_usd = abs(pos.entry - pos.initial_stop) * pos.units * accounts.usd_per_unit(
            market.instrument(pos.instrument).quote, quotes)
        return {
            "id": pos.id, "strategy": pos.strategy, "instrument": pos.instrument,
            "direction": pos.direction, "opened_at": pos.opened_at, "closed_at": _iso(now),
            "entry": pos.entry, "exit": price, "reason": why, "score": pos.score,
            "risk_pct": pos.risk_pct, "pnl_pct": accounts.pct(realized),
            "swap_pct": accounts.pct(pos.swap_paid),
            "r_multiple": round(realized / risk_usd, 3) if risk_usd else 0.0,
        }
