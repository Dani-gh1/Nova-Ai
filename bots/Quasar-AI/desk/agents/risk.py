"""Risk: gearing, likvideringspris, funding hver 8. time, hårdt stop FØR likvidering,
maks. samlet kontogearing og dagligt tabsloft der pauser handel.

Input: TradeIdea (+ konto)   Output: RiskPlan eller afvisningsgrund (str)
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from .. import config
from ..models import Account, Position, RiskPlan, TradeIdea

HOUR_MS = 3_600_000


def liquidation_price(side: str, entry: float, leverage: float,
                      mmr: Optional[float] = None) -> float:
    """Isoleret margin, lineær USDT-kontrakt (forenklet, ét MMR-trin).

    long:  entry * (1 - 1/L + mmr)     short: entry * (1 + 1/L - mmr)
    """
    mmr = config.MAINTENANCE_MARGIN_RATE if mmr is None else mmr
    if side == "long":
        return entry * (1 - 1 / leverage + mmr)
    return entry * (1 + 1 / leverage - mmr)


def isolated_liq_price(side: str, entry: float, qty: float, margin: float,
                       mmr: Optional[float] = None) -> float:
    """Likvideringspris ud fra den AKTUELLE isolerede margin (falder når funding betales).

    Likvidering når margin + urealiseret = mmr * notional ved likvideringsprisen.
    """
    mmr = config.MAINTENANCE_MARGIN_RATE if mmr is None else mmr
    if qty <= 0:
        return entry
    if side == "long":
        return max(0.0, (entry - margin / qty) / (1 - mmr))
    return (entry + margin / qty) / (1 + mmr)


def tightest_allowed_stop(side: str, entry: float, liq: float) -> float:
    """Den fjerneste stoppris der stadig ligger STOP_MAX_FRACTION_OF_LIQ af vejen mod likvidering."""
    f = config.STOP_MAX_FRACTION_OF_LIQ
    return entry - f * (entry - liq) if side == "long" else entry + f * (liq - entry)


def max_stop_pct(leverage: float) -> float:
    """Største tilladte stopafstand: en fast brøkdel af afstanden til likvidering."""
    return (1 / leverage - config.MAINTENANCE_MARGIN_RATE) * config.STOP_MAX_FRACTION_OF_LIQ


def stop_before_liq(side: str, stop: float, liq: float) -> bool:
    return stop > liq if side == "long" else stop < liq


def utc_day(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


class Risk:
    name = "risk"

    # ------------------------------------------------------------ kontoniveau
    def roll_day(self, acc: Account, equity: float, now_ms: int) -> None:
        today = utc_day(now_ms)
        if acc.day != today:
            acc.day = today
            acc.day_start_equity = equity
            if acc.paused_until_day and acc.paused_until_day < today:
                acc.paused_until_day = ""

    def check_daily_loss(self, acc: Account, equity: float, now_ms: int) -> bool:
        """True hvis kontoen netop er blevet sat på pause."""
        today = utc_day(now_ms)
        if acc.paused_until_day == today or acc.day_start_equity <= 0:
            return False
        if equity <= acc.day_start_equity * (1 - config.DAILY_LOSS_LIMIT):
            acc.paused_until_day = today
            return True
        return False

    def is_paused(self, acc: Account, now_ms: int) -> bool:
        return acc.paused_until_day == utc_day(now_ms)

    # ------------------------------------------------------------- vurdering
    def assess(self, idea: TradeIdea, strat, acc: Account, prices: dict[str, float],
               now_ms: int) -> RiskPlan | str:
        if self.is_paused(acc, now_ms):
            return "dagligt tabsloft nået - pause til midnat UTC"
        if any(p.symbol == idea.symbol for p in acc.positions):
            return "har allerede en position i kontrakten"
        lev = int(max(config.MIN_LEVERAGE, min(config.MAX_LEVERAGE, strat.leverage)))
        equity = acc.equity(prices)
        if equity <= 0:
            return "konto tom"
        margin = equity * strat.margin_pct
        notional = margin * lev
        room = equity * config.MAX_ACCOUNT_LEVERAGE - acc.open_notional(prices)
        if notional > room:
            if room < notional * 0.5:
                return f"samlet gearing ville overstige {config.MAX_ACCOUNT_LEVERAGE}x"
            notional = room
            margin = notional / lev
        if margin > acc.balance:
            return "ikke nok fri margin"

        sig = idea.signal
        stop_pct = strat.stop_pct
        if strat.atr_stop_mult > 0:
            stop_pct = max(stop_pct, strat.atr_stop_mult * sig.atr_pct)
        stop_pct = min(stop_pct, max_stop_pct(lev))
        if stop_pct < config.STOP_MIN_PCT:
            return f"stop {stop_pct * 100:.2f}% for tæt (støj)"

        ref = sig.book.mid if sig.book and sig.book.mid else sig.price
        d = 1 if idea.side == "long" else -1
        stop = ref * (1 - d * stop_pct)
        liq = liquidation_price(idea.side, ref, lev)
        if not stop_before_liq(idea.side, stop, liq):
            return "stop ligger ikke før likvideringsprisen"
        tp = ref * (1 + d * strat.take_profit_pct) if strat.take_profit_pct else None
        return RiskPlan(strategy=strat.name, symbol=idea.symbol, side=idea.side, score=idea.score,
                        leverage=lev, margin=margin, notional=notional, ref_price=ref,
                        stop_price=stop, take_profit_price=tp, liq_price_est=liq,
                        max_hold_hours=strat.max_hold_hours,
                        funding_rate=sig.features.get("funding", 0.0), book=sig.book)

    # ------------------------------------------------------- løbende simulering
    def apply_funding(self, acc: Account, rates: dict[str, float],
                      prices: dict[str, float], now_ms: int) -> list[tuple[Position, float]]:
        """Betal/modtag funding ved hvert 8-timers skæringstidspunkt (00/08/16 UTC).

        Positiv rate: long betaler short. Betalingen trækkes fra (lægges til) positionens
        isolerede margin, så likvideringsprisen flytter sig. Hvis stoppet derved ikke
        længere ligger sikkert før likvideringsprisen, strammes det.
        Returnerer (position, betaling) - positiv = betalt.
        """
        interval = config.FUNDING_INTERVAL_HOURS * HOUR_MS
        boundary = now_ms // interval * interval
        out = []
        for p in acc.positions:
            last = max(p.last_funding_ms, p.opened_ms)
            n = 0
            b = last // interval * interval + interval
            while b <= boundary:
                n += 1
                b += interval
            if n == 0:
                continue
            rate = rates.get(p.symbol, 0.0)
            px = prices.get(p.symbol, p.entry_price)
            pay = p.qty * px * rate * (1 if p.side == "long" else -1) * n
            p.margin -= pay
            p.funding_paid += pay
            p.last_funding_ms = boundary
            p.liq_price = isolated_liq_price(p.side, p.entry_price, p.qty, p.margin)
            limit = tightest_allowed_stop(p.side, p.entry_price, p.liq_price)
            if (p.side == "long" and p.stop_price < limit) or (p.side == "short" and p.stop_price > limit):
                p.stop_price = limit
            out.append((p, pay))
        return out

    def liquidation_hit(self, p: Position, open_px: float, low: float, high: float) -> bool:
        """Likvideret hvis prisen springer forbi likvideringsprisen UDEN at handle ved stoppet
        (kursgab). Ellers udløses det hårde stop først, fordi det altid ligger før."""
        if p.side == "long":
            return open_px <= p.liq_price
        return open_px >= p.liq_price
