"""Regelsæt. Hvert regelsæt handler på sin egen virtuelle konto.

Et regelsæt er ren data: vægte for Analystens komponenter, krav til flag,
minimum-score/kant, gearing, marginprocent, stop/take-profit, holdetid.
Strategi-trinnet omsætter et Signal til en TradeIdea (eller ingenting);
Risk og Executor gør resten.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from . import config
from .agents.analyst import Analyst
from .models import Signal, TradeIdea


@dataclass(frozen=True)
class Strategy:
    name: str
    label: str
    group: str
    description: str
    weights: Optional[dict] = None
    sides: str = "both"                 # both | long | short
    require_long: tuple = ()            # flagnavne, "!" = skal være falsk
    require_short: tuple = ()
    min_score: float = 60.0
    min_edge: float = 15.0              # score(side) - score(modsat side)
    leverage: int = 2
    margin_pct: float = field(default_factory=lambda: config.DEFAULT_MARGIN_PCT)
    stop_pct: float = 0.03
    atr_stop_mult: float = 0.0          # >0: stop = max(stop_pct, mult * ATR)
    take_profit_pct: Optional[float] = None
    max_hold_hours: float = 48.0
    max_open: int = field(default_factory=lambda: config.DEFAULT_MAX_OPEN)

    def _flags_ok(self, sig: Signal, req: tuple) -> bool:
        for r in req:
            neg = r.startswith("!")
            val = bool(sig.flags.get(r.lstrip("!"), False))
            if val == neg:
                return False
        return True

    def scores(self, sig: Signal) -> tuple[float, float]:
        if not self.weights:
            return sig.long_score, sig.short_score
        return (Analyst.combine(sig.components_long, self.weights),
                Analyst.combine(sig.components_short, self.weights))

    def evaluate(self, sig: Signal) -> Optional[TradeIdea]:
        ls, ss = self.scores(sig)
        options = []
        if self.sides in ("both", "long") and self._flags_ok(sig, self.require_long):
            options.append(("long", ls, ls - ss))
        if self.sides in ("both", "short") and self._flags_ok(sig, self.require_short):
            options.append(("short", ss, ss - ls))
        options = [o for o in options if o[1] >= self.min_score and o[2] >= self.min_edge]
        if not options:
            return None
        side, score, edge = max(options, key=lambda o: o[1])
        return TradeIdea(strategy=self.name, symbol=sig.symbol, side=side,
                         score=round(score, 2), edge=round(edge, 2), signal=sig)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["weights"] = self.weights or dict(config.ANALYST_WEIGHTS)
        return d


S = Strategy
STRATEGIES: list[Strategy] = [
    S("funding_carry", "Funding-carry 1x", "Funding-arbitrage",
      "Går imod den side, der betaler funding, når funding er ekstrem. 1x og lang holdetid "
      "for at høste funding - tættest på en delta-neutral carry uden spot-ben.",
      weights={"funding": 1.0, "trend": 0.2}, require_long=("funding_extreme", "!funding_positive"),
      require_short=("funding_extreme", "funding_positive"), min_score=58, min_edge=10,
      leverage=1, margin_pct=0.10, stop_pct=0.05, max_hold_hours=72, max_open=5),
    S("funding_carry_2x", "Funding-carry 2x", "Funding-arbitrage",
      "Som Funding-carry, men 2x og kortere holdetid.",
      weights={"funding": 1.0, "trend": 0.2}, require_long=("funding_extreme", "!funding_positive"),
      require_short=("funding_extreme", "funding_positive"), min_score=60, min_edge=12,
      leverage=2, stop_pct=0.035, max_hold_hours=48),
    S("trend_2x", "Trend 2x", "Trend lav gearing",
      "Følger trend + momentum bekræftet af stigende OI. ATR-stop.",
      weights={"trend": 0.5, "momentum": 0.3, "oi": 0.2}, min_score=65, min_edge=25,
      leverage=2, stop_pct=0.025, atr_stop_mult=3.0, take_profit_pct=0.08, max_hold_hours=72),
    S("trend_slow_1x", "Langsom trend 1x", "Trend lav gearing",
      "Ren trend uden gearing, bredt stop og ingen fast gevinstmål.",
      weights={"trend": 0.7, "momentum": 0.3}, min_score=60, min_edge=20,
      leverage=1, margin_pct=0.10, stop_pct=0.05, max_hold_hours=120),
    S("liq_revert", "Likvidations-rebound 2x", "Mean reversion",
      "Køber efter bølger af tvangslukkede longs (og omvendt) - forventer at prisen "
      "trækker tilbage, når tvangssalget er overstået.",
      weights={"liquidations": 0.6, "funding": 0.4},
      require_long=("liq_wave", "liq_longs_dominant"), require_short=("liq_wave", "!liq_longs_dominant"),
      min_score=55, min_edge=8, leverage=2, stop_pct=0.025, take_profit_pct=0.03, max_hold_hours=12),
    S("liq_revert_3x", "Likvidations-rebound 3x", "Mean reversion",
      "Hurtigere og mere gearet variant af likvidations-rebound.",
      weights={"liquidations": 0.7, "funding": 0.3},
      require_long=("liq_wave", "liq_longs_dominant"), require_short=("liq_wave", "!liq_longs_dominant"),
      min_score=58, min_edge=10, leverage=3, stop_pct=0.02, take_profit_pct=0.025, max_hold_hours=8),
    S("breakout_oi", "Breakout + OI 3x", "Breakout",
      "Brud over/under 24t-range, når nye penge strømmer ind (OI stiger).",
      weights={"momentum": 0.4, "oi": 0.4, "trend": 0.2},
      require_long=("breakout_up", "oi_rising"), require_short=("breakout_down", "oi_rising"),
      min_score=60, min_edge=15, leverage=3, stop_pct=0.02, take_profit_pct=0.06, max_hold_hours=24),
    S("breakout_tight", "Breakout stramt 2x", "Breakout",
      "Range-brud uden OI-krav, men med stramt ATR-stop.",
      weights={"momentum": 0.5, "trend": 0.3, "oi": 0.2},
      require_long=("breakout_up",), require_short=("breakout_down",),
      min_score=62, min_edge=20, leverage=2, stop_pct=0.012, atr_stop_mult=1.5,
      take_profit_pct=0.04, max_hold_hours=24),
    S("aggressive_5x", "Aggressiv 5x", "Aggressiv",
      "Høj gearing, stort stop helt ude ved grænsen før likvidering. Mange likvideringer "
      "ved kursgab er forventeligt - det er pointen at se dem.",
      min_score=60, min_edge=15, leverage=5, margin_pct=0.10, stop_pct=0.15,
      take_profit_pct=0.10, max_hold_hours=48, max_open=6),
    S("aggressive_mom_5x", "Aggressiv momentum 5x", "Aggressiv",
      "5x på stærkt momentum med moderat stop.",
      weights={"momentum": 0.6, "trend": 0.4}, min_score=70, min_edge=30, leverage=5,
      margin_pct=0.08, stop_pct=0.04, take_profit_pct=0.08, max_hold_hours=24, max_open=5),
    S("picky", "Kræsen 2x", "Kræsen",
      "Handler kun de allerstærkeste signaler. Få handler, høj tærskel.",
      min_score=68, min_edge=30, leverage=2, stop_pct=0.03, take_profit_pct=0.06,
      max_hold_hours=48, max_open=2),
    S("picky_long", "Kræsen kun long 2x", "Kræsen",
      "Kræsen, men kun long-siden.",
      sides="long", min_score=66, min_edge=26, leverage=2, stop_pct=0.03,
      take_profit_pct=0.06, max_hold_hours=48, max_open=2),
    S("fade_crowded_longs", "Shorter trængte longs 2x", "Positionering",
      "Shorter, når longs er trængte (høj funding / højt long/short-forhold).",
      weights={"funding": 0.6, "momentum": 0.2, "liquidations": 0.2}, sides="short",
      require_short=("crowded_long",), min_score=60, min_edge=12, leverage=2,
      stop_pct=0.03, take_profit_pct=0.05, max_hold_hours=36),
    S("squeeze_shorts", "Short squeeze 2x", "Positionering",
      "Går long, når shorts er trængte og OI stiger - spiller på en squeeze.",
      weights={"funding": 0.5, "oi": 0.3, "momentum": 0.2}, sides="long",
      require_long=("crowded_short",), min_score=60, min_edge=12, leverage=2,
      stop_pct=0.03, take_profit_pct=0.05, max_hold_hours=36),
    S("oi_divergence", "OI-divergens 2x", "Mean reversion",
      "Faldende OI mod prisretningen: et rally drevet af short-covering shortes, "
      "et fald drevet af long-deleveraging købes.",
      weights={"oi": 0.7, "momentum": 0.3}, require_long=("oi_falling",), require_short=("oi_falling",),
      min_score=56, min_edge=10, leverage=2, stop_pct=0.025, take_profit_pct=0.035, max_hold_hours=24),
    S("balanced", "Balanceret 2x", "Standard",
      "Analystens standardvægte uden særlige krav. Referencen de andre måles mod.",
      min_score=62, min_edge=15, leverage=2, stop_pct=0.02, atr_stop_mult=2.5,
      take_profit_pct=0.05, max_hold_hours=36),
    S("contrarian_ls", "Kontrær positionering 1x", "Positionering",
      "Ren kontrær på funding + long/short-forhold, ingen gearing.",
      weights={"funding": 1.0}, min_score=62, min_edge=20, leverage=1, margin_pct=0.08,
      stop_pct=0.04, max_hold_hours=48),
    S("scalper", "Scalper 3x", "Aggressiv",
      "Korte handler med stramt stop og lille gevinstmål. Gebyrer og slippage fylder meget.",
      min_score=64, min_edge=20, leverage=3, stop_pct=0.008, take_profit_pct=0.012,
      max_hold_hours=4, max_open=5),
]

BY_NAME = {s.name: s for s in STRATEGIES}
assert len(BY_NAME) == len(STRATEGIES) >= 15
