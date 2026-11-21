"""Risk: no position exists without a stop.

Before entry  - Risk issues a RiskPlan (stop -6%, trail arms at +12%, trails 6% under the
                high). The Executor refuses any order without an approved plan, and sets
                the stop atomically with the fill: stop = fill price x (1 - 6%).
After entry   - every poll Risk marks each position, raises the high-water mark, arms the
                trail once price >= entry x 1.12, ratchets the stop up (never down), and
                emits an ExitSignal the moment the mark is at or below the stop.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..config import AnalystConfig, RiskConfig
from ..messages import ExitSignal, Position, RiskPlan, ScoreCard


@dataclass(frozen=True)
class RiskDecision:
    position_id: str
    high_price: float
    stop_price: float
    trail_active: bool
    mark_price: float
    exit: ExitSignal | None


class Risk:
    name = "risk"

    def __init__(self, cfg: RiskConfig, analyst_cfg: AnalystConfig):
        self.cfg = cfg
        self.min_score = analyst_cfg.min_score

    def plan(self, sc: ScoreCard) -> RiskPlan:
        approved, reason = True, ""
        if not sc.passed or sc.score < self.min_score:
            approved, reason = False, f"score {sc.score} below {self.min_score}"
        return RiskPlan(scorecard=sc, stop_loss_pct=self.cfg.stop_loss_pct,
                        trail_trigger_pct=self.cfg.trail_trigger_pct,
                        trail_distance_pct=self.cfg.trail_distance_pct,
                        approved=approved, reason=reason)

    @staticmethod
    def initial_stop(entry_price: float, plan: RiskPlan) -> float:
        return entry_price * (1.0 - plan.stop_loss_pct / 100.0)

    @staticmethod
    def trail_trigger(entry_price: float, plan: RiskPlan) -> float:
        return entry_price * (1.0 + plan.trail_trigger_pct / 100.0)

    def evaluate(self, pos: Position, mark: float, now: float | None = None) -> RiskDecision:
        high = max(pos.high_price, mark)
        stop = pos.stop_price
        trail = pos.trail_active or high >= pos.trail_trigger_price
        if trail:
            stop = max(stop, high * (1.0 - pos.trail_distance_pct / 100.0))
        exit_signal = None
        if mark <= stop:
            exit_signal = ExitSignal(position_id=pos.id, mint=pos.mint, symbol=pos.symbol,
                                     reason="trailing_stop" if trail else "stop_loss",
                                     mark_price=mark, stop_price=stop, ts=now or time.time())
        return RiskDecision(pos.id, high, stop, trail, mark, exit_signal)
