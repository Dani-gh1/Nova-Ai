"""Filter: dræber kandidater ved stor spread, vigtige nyheder tæt på eller dødt marked."""
from __future__ import annotations

from datetime import datetime

from .. import config, indicators as ind, market
from ..models import FilterPass, FilterReport, ScoutReport


class Filter:
    name = "filter"

    def __init__(self, news):
        self.news = news

    def run(self, report: ScoutReport, md) -> FilterReport:
        now = report.time
        passed, killed = [], []
        blackout = market.in_rollover_blackout(now)
        for pick in report.picks:
            sym = pick.instrument
            ins = market.instrument(sym)
            if blackout:
                killed.append((sym, "rollover-vindue (17:00 NY) – spreads udvides"))
                continue
            m15 = md.candles(sym, "M15")
            q = md.quote(sym)
            if not m15 or q is None:
                killed.append((sym, "mangler data"))
                continue
            age = (now - m15[-1].time).total_seconds() / 60 - 15
            if age > config.FILTER_MAX_DATA_AGE_MIN:
                killed.append((sym, f"forældede data ({age:.0f} min)"))
                continue
            atr15 = ind.atr(m15, config.ATR_LEN)
            spread_atr = q.spread / atr15 if atr15 > 0 else 99
            typical = ins.typical_spread_pips * ins.pip
            if spread_atr > config.FILTER_MAX_SPREAD_ATR:
                killed.append((sym, f"spread {spread_atr:.0%} af ATR(M15)"))
                continue
            if q.spread > typical * config.FILTER_MAX_SPREAD_X_TYPICAL:
                killed.append((sym, f"spread {q.spread / typical:.1f}x normal"))
                continue
            near = self.news.nearest(sym, now)
            mins, title = (near[0], near[1].title + f" ({near[1].currency})") if near else (None, "")
            if near and abs(mins) <= config.FILTER_NEWS_WINDOW_MIN:
                when = f"om {mins:.0f} min" if mins >= 0 else f"for {-mins:.0f} min siden"
                killed.append((sym, f"nyhed {when}: {title}"))
                continue
            if pick.atr_ratio < config.FILTER_MIN_ATR_RATIO:
                killed.append((sym, f"meget lav volatilitet ({pick.atr_ratio:.2f})"))
                continue
            passed.append(FilterPass(pick, q.spread, round(spread_atr, 4), mins, title))
        return FilterReport(now, report.market, tuple(passed), tuple(killed))
