"""Filter: dræber kandidater med lav gennemsnitsvolumen, bred spread,
earnings inden for X dage, handelsstop eller for kort historik.
Output: (list[Passed], list[Kill]).
"""
from __future__ import annotations

from datetime import date

from ..models import Candidate, Kill, Passed


class Filter:
    name = "Filter"

    def __init__(self, cfg, logger=None):
        self.cfg = cfg
        self.log = logger

    def check(self, c: Candidate, today: date, session: str = "regular") -> str | None:
        cfg, f, s = self.cfg, c.features, c.snapshot
        if s.halted:
            return "handelsstop (halt)"
        if (session == "regular" and s.last_trade_age_s is not None
                and s.last_trade_age_s > cfg.HALT_STALE_SECONDS):
            return f"ingen handler i {s.last_trade_age_s / 60:.0f} min (muligt stop)"
        if f.get("bars", 0) < cfg.MIN_HISTORY_BARS:
            return "for kort kurshistorik"
        if s.price < cfg.MIN_PRICE:
            return f"kurs under {cfg.MIN_PRICE}"
        if (f.get("avg_vol20") or 0) < cfg.MIN_AVG_VOLUME:
            return "lav gennemsnitsvolumen"
        if (f.get("avg_dollar_vol20") or 0) < cfg.MIN_AVG_DOLLAR_VOLUME:
            return "lav gennemsnitlig omsætning"
        sp = f.get("spread_pct")
        if sp is None and s.bid and s.ask and s.ask >= s.bid > 0:
            sp = (s.ask - s.bid) / ((s.ask + s.bid) / 2)
        if sp is None and not cfg.SPREAD_UNKNOWN_ALLOWED:
            return "spread ukendt"
        if sp is not None and sp > cfg.MAX_SPREAD_PCT:
            return f"bred spread ({sp * 100:.2f} %)"
        if not s.is_etf and s.earnings_date is not None:
            days = (s.earnings_date - today).days
            if 0 <= days <= cfg.EARNINGS_BLACKOUT_DAYS:
                return f"earnings om {days} dage"
        return None

    def run(self, candidates: list[Candidate], today: date, session: str = "regular"):
        passed, killed = [], []
        for c in candidates:
            reason = self.check(c, today, session)
            if reason:
                killed.append(Kill(c.symbol, reason))
            else:
                passed.append(Passed(c))
        if self.log:
            self.log.event(self.name, f"{len(passed)} bestået, {len(killed)} dræbt",
                           data={"killed": [(k.symbol, k.reason) for k in killed[:60]]})
            for p in passed:
                self.log.handoff(self.name, "Analyst", p.candidate.symbol, {"signals": p.candidate.signals})
        return passed, killed
