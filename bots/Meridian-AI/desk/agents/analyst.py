"""Analyst: scorer 0-100 for long og short pr. instrument.

Komponenter (hver giver en long- og short-score, 50 = neutral):
  trend        – EMA-trend på M15, H1 og H4 vægtet
  momentum     – RSI og ændringsrate på H1 målt i ATR
  volatility   – ATR-baseret: belønner sundt udsving, straffer dødt eller kaotisk marked
  carry        – renteforskel mellem basis- og kursvaluta
  correlation  – bekræfter korrelerede instrumenter samme retning?
Ekstra (bruges af specifikke strategier): breakout (sessionsrange), meanrev (Bollinger z).
"""
from __future__ import annotations

from datetime import timedelta

from .. import config, indicators as ind, market
from ..models import AnalystReport, FilterReport, Signal


def _ls(x: float) -> tuple:
    """-1..1 -> (long, short) scorer 0..100."""
    x = ind.clamp(x)
    return round(50 + 50 * x, 1), round(50 - 50 * x, 1)


def vol_score(ratio: float) -> float:
    if ratio < 0.6:
        return 20 + 50 * (ratio / 0.6) * 0.6
    if ratio <= 1.6:
        return 70 + 30 * min(1.0, (ratio - 0.6) / 0.6)
    if ratio <= 2.5:
        return 100 - 50 * (ratio - 1.6) / 0.9
    return 40.0


class Analyst:
    name = "analyst"

    def __init__(self):
        self._cache: dict = {}

    def _trend(self, md, sym):
        total, detail = 0.0, {}
        for tf, w in config.TREND_TF_WEIGHTS.items():
            cs = md.candles(sym, tf)
            key = ("trend", sym, tf, cs[-1].time if cs else None)
            if key not in self._cache:
                closes = [c.close for c in cs]
                self._cache[key] = ind.trend_strength(closes, config.EMA_FAST, config.EMA_SLOW,
                                                      ind.atr(cs, config.ATR_LEN))
            detail[tf] = round(self._cache[key], 3)
            total += w * self._cache[key]
        return total, detail

    def correlations(self, md, symbols) -> dict:
        rets = {}
        for s in symbols:
            cs = md.candles(s, "H1")[-(config.CORR_LOOKBACK + 1):]
            rets[s] = ind.returns([c.close for c in cs])
        out = {}
        for i, a in enumerate(symbols):
            for b in symbols[i + 1:]:
                ta = md.candles(a, "H1")[-1].time if md.candles(a, "H1") else None
                key = ("corr", a, b, ta)
                if key not in self._cache:
                    self._cache[key] = round(ind.correlation(rets[a], rets[b]), 3)
                out[(a, b)] = out[(b, a)] = self._cache[key]
        return out

    def _session_range(self, m15, now, hours_back_start, hours_back_end):
        lo_t, hi_t = now - timedelta(hours=hours_back_start), now - timedelta(hours=hours_back_end)
        w = [c for c in m15 if lo_t <= c.time < hi_t]
        if len(w) < 4:
            return None
        return max(c.high for c in w), min(c.low for c in w)

    def run(self, report: FilterReport, md) -> AnalystReport:
        now = report.time
        if len(self._cache) > 20000:
            self._cache.clear()
        all_syms = [s for s in config.INSTRUMENTS if len(md.candles(s, "H1")) > 30]
        corr = self.correlations(md, all_syms)
        trends = {}
        for s in all_syms:
            trends[s] = self._trend(md, s)[0]
        signals = []
        for fp in report.passed:
            sym = fp.pick.instrument
            ins = market.instrument(sym)
            m15, h1 = md.candles(sym, "M15"), md.candles(sym, "H1")
            price = m15[-1].close
            atr15, atr1 = ind.atr(m15, config.ATR_LEN), ind.atr(h1, config.ATR_LEN)
            comps, ctx = {}, {}

            t, tdet = self._trend(md, sym)
            comps["trend"] = _ls(t)
            ctx["trend_tf"] = tdet

            closes1 = [c.close for c in h1]
            r = ind.rsi(closes1[-100:], config.RSI_LEN)
            roc = (closes1[-1] - closes1[-13]) / (2 * atr1) if len(closes1) > 13 and atr1 else 0
            comps["momentum"] = _ls(0.5 * ind.clamp((r - 50) / 25) + 0.5 * ind.clamp(roc))
            ctx["rsi_h1"] = round(r, 1)

            v = round(vol_score(fp.pick.atr_ratio), 1)
            comps["volatility"] = (v, v)

            rb = config.POLICY_RATES.get(ins.base, 0.0)
            rq = config.POLICY_RATES.get(ins.quote, 0.0)
            comps["carry"] = _ls((rb - rq) / config.CARRY_FULL_SCALE_PCT)
            ctx["rate_diff"] = round(rb - rq, 2)

            conf, wsum, peers = 0.0, 0.0, []
            for other in all_syms:
                if other == sym:
                    continue
                rho = corr.get((sym, other), 0.0)
                if abs(rho) >= config.CORR_MIN:
                    conf += rho * trends[other]
                    wsum += abs(rho)
                    peers.append((other, rho))
            c = conf / wsum if wsum else 0.0
            comps["correlation"] = _ls(c * (1 if t * c >= 0 else 0.5)) if wsum else (50.0, 50.0)
            ctx["peers"] = sorted(peers, key=lambda x: -abs(x[1]))[:4]

            # ekstra: breakout af seneste 6 timers range (før nuværende time) og Bollinger z
            rng = self._session_range(m15, now, 7, 1)
            if rng:
                hi, lo = rng
                up = (price - hi) / atr15 if atr15 else 0
                dn = (lo - price) / atr15 if atr15 else 0
                comps["breakout"] = (round(50 + 50 * ind.clamp(up, -1, 1), 1),
                                     round(50 + 50 * ind.clamp(dn, -1, 1), 1))
                ctx["range"] = (hi, lo)
            else:
                comps["breakout"] = (50.0, 50.0)
            closes15 = [x.close for x in m15]
            sd = ind.stdev(closes15, config.BB_LEN)
            z = (price - ind.sma(closes15, config.BB_LEN)) / (2 * sd) if sd else 0.0
            r15 = ind.rsi(closes15[-60:], config.RSI_LEN)
            mr = -0.6 * ind.clamp(z) - 0.4 * ind.clamp((r15 - 50) / 25)
            comps["meanrev"] = _ls(mr)
            ctx["z"] = round(z, 2)
            ctx["sessions"] = report.market.sessions
            ctx["minutes_in"] = {g: market.minutes_into_session(g, now) for g in report.market.sessions}

            w = config.DEFAULT_WEIGHTS
            long_s = sum(w[k] * comps[k][0] for k in w) / sum(w.values())
            short_s = sum(w[k] * comps[k][1] for k in w) / sum(w.values())
            signals.append(Signal(sym, now, price, atr1, atr15, round(long_s, 1), round(short_s, 1),
                                  comps, ctx, fp))
        return AnalystReport(now, report.market, tuple(signals), corr)
