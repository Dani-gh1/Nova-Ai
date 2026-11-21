"""Risk: stop/trailing i ATR-multipla, valuta-eksponering, korrelation og dagligt tabsloft."""
from __future__ import annotations

from datetime import datetime

from .. import accounts, config, market
from ..models import Account, RiskDecision, TradeIntent


class Risk:
    name = "risk"

    def run(self, intent: TradeIntent, acc: Account, params, quotes: dict, corr: dict,
            now: datetime) -> RiskDecision:
        def no(reason):
            return RiskDecision(intent, False, reason)

        sig = intent.signal
        sym = sig.instrument
        eq = accounts.equity(acc, quotes)
        if acc.halted_epoch:
            return no("strategien er stoppet i denne epoke (maks. drawdown)")
        if acc.halted_day == market.trading_day(now):
            return no("dagligt tabsloft nået")
        day_loss = (acc.day_start_equity - eq) / acc.day_start_equity * 100
        if day_loss >= params.daily_loss_cap:
            return no(f"dagligt tabsloft ({day_loss:.2f}% >= {params.daily_loss_cap}%)")
        mtc = market.minutes_to_close(now)
        if mtc is not None and mtc < config.CLOSE_BEFORE_WEEKEND_MIN + 60 and market.ny(now).weekday() == 4:
            return no("for tæt på weekendlukning")
        if any(p.instrument == sym for p in acc.positions):
            return no("har allerede en position i instrumentet")

        risk_pct = params.risk_pct
        # valuta-eksponering: fem EUR-handler er én stor EUR-handel
        ins = market.instrument(sym)
        exp = accounts.currency_exposure(acc, quotes, eq)
        s = accounts.sign(intent.direction)
        q = quotes.get(sym)
        for ccy, k in ((ins.base, s), (ins.quote, -s)):
            r, n = exp.get(ccy, (0.0, 0.0))
            if abs(r + k * risk_pct) > params.max_ccy_risk + 1e-9:
                return no(f"for stor {ccy}-eksponering ({abs(r + k * risk_pct):.1f}% risiko > {params.max_ccy_risk}%)")
        # korrelerede positioner i samme effektive retning
        same = 0
        for p in acc.positions:
            rho = corr.get((sym, p.instrument), 0.0)
            if abs(rho) >= config.CORR_HIGH and rho * accounts.sign(p.direction) * s > 0:
                same += 1
        if same >= config.MAX_CORRELATED_POSITIONS:
            return no(f"{same} stærkt korrelerede positioner i samme retning")

        stop = params.stop_atr * sig.atr_h1
        if q is not None and stop < 3 * q.spread:
            stop = 3 * q.spread
        if stop <= 0:
            return no("ATR er nul")
        tp = stop * params.tp_r if params.tp_r else None
        return RiskDecision(intent, True, "godkendt", stop_distance=stop,
                            trail_distance=params.trail_atr * sig.atr_h1 if params.trail_atr else 0.0,
                            trail_activate=stop * config.TRAIL_ACTIVATE_R,
                            take_profit_distance=tp, risk_pct=risk_pct)
