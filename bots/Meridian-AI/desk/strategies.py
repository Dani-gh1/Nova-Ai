"""Regelsæt der testes side om side – hvert på sin egen virtuelle konto.

En strategi ser KUN Analystens signal og beslutter en TradeIntent (retning + score).
Risk og Executor bruger strategiens parametre (risiko, stops, maks. positioner ...).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

from . import config, market
from .models import Signal, TradeIntent


@dataclass(frozen=True)
class StrategyParams:
    name: str
    title: str
    description: str
    kind: str = "composite"  # composite / breakout / meanrev / random
    weights: dict = field(default_factory=lambda: dict(config.DEFAULT_WEIGHTS))
    min_score: float = 62.0
    min_edge: float = 10.0  # long- og short-score skal være mindst så langt fra hinanden
    risk_pct: float = config.RISK_PER_TRADE_PCT
    stop_atr: float = config.STOP_ATR_MULT
    trail_atr: float = config.TRAIL_ATR_MULT
    tp_r: Optional[float] = config.TAKE_PROFIT_R
    max_positions: int = config.MAX_OPEN_POSITIONS
    quarantine_min: int = config.QUARANTINE_MIN
    daily_loss_cap: float = config.DAILY_LOSS_CAP_PCT
    max_ccy_risk: float = config.MAX_CCY_RISK_PCT
    max_hold_hours: float = config.MAX_HOLD_HOURS
    sessions_any: tuple = ()
    sessions_only: tuple = ()
    sessions_all: tuple = ()
    kinds: tuple = ()
    instruments: tuple = ()
    news_window: float = 0.0  # ekstra (strengere) nyhedsvindue i minutter
    flat_before_news_min: float = 0.0  # luk åbne positioner så mange min før vigtig nyhed
    session_window: float = 0.0  # breakout: kun de første N min af sessionen
    max_abs_trend: float = 0.0  # meanrev: kun når H4-trenden er svag
    require_peers: bool = False
    random_prob: float = 0.0


S = StrategyParams
STRATEGIES = [
    S("standard", "Standard", "Config-vægte og -tærskler uden justeringer – referencen."),
    S("trend", "Trend", "Følger trenden på tværs af M15/H1/H4 med momentum og korrelation.",
      weights={"trend": .45, "momentum": .2, "volatility": .1, "correlation": .15, "carry": .1},
      min_score=64),
    S("trend-langsom", "Langsom trend", "Kun den store trend + carry. Brede stops, intet fast mål.",
      weights={"trend": .6, "carry": .2, "volatility": .2}, min_score=62, stop_atr=3.0,
      trail_atr=3.5, tp_r=None, max_hold_hours=240),
    S("breakout-london", "Breakout London", "Brud på natte-range i Londons første 2 timer.",
      kind="breakout", weights={"breakout": .6, "trend": .2, "volatility": .2}, min_score=66,
      sessions_any=("london",), session_window=120, stop_atr=1.5, tp_r=2.0),
    S("breakout-newyork", "Breakout New York", "Brud på formiddags-range i New Yorks første 90 min.",
      kind="breakout", weights={"breakout": .6, "momentum": .2, "volatility": .2}, min_score=66,
      sessions_any=("newyork",), session_window=90, stop_atr=1.5, tp_r=2.0),
    S("meanrev-asien", "Mean reversion Asien", "Køber dyk/sælger toppe i den rolige asiatiske session.",
      kind="meanrev", weights={"meanrev": .8, "volatility": .2}, min_score=66, min_edge=20,
      sessions_only=("asia",), stop_atr=1.5, tp_r=1.0, trail_atr=0.0, max_hold_hours=8,
      max_abs_trend=0.5),
    S("nyhedsfilter", "Nyhedsfilter", "Standard, men 2 timers nyhedsvindue og flad før store nyheder.",
      news_window=120, flat_before_news_min=30),
    S("kraesen", "Kræsen", "Kun de allerbedste signaler, halv risiko, få positioner.",
      min_score=74, min_edge=25, risk_pct=0.5, max_positions=2),
    S("aggressiv", "Aggressiv", "Lav tærskel, dobbelt risiko, mange positioner.",
      min_score=57, min_edge=6, risk_pct=2.0, max_positions=8, daily_loss_cap=6.0, max_ccy_risk=6.0),
    S("mange-smaa", "Mange små", "Mange små handler med kvart risiko og hurtige gevinster.",
      min_score=58, min_edge=6, risk_pct=0.25, max_positions=12, stop_atr=1.5, tp_r=1.0,
      quarantine_min=30),
    S("momentum", "Momentum", "RSI og ændringsrate styrer; trend som støtte.",
      weights={"momentum": .5, "trend": .2, "volatility": .3}, min_score=63),
    S("carry", "Carry", "Renteforskel + trend. Lang holdetid, intet fast mål.",
      weights={"carry": .5, "trend": .3, "volatility": .2}, min_score=60, tp_r=None,
      trail_atr=3.0, max_hold_hours=240),
    S("korrelation", "Korrelation", "Kræver at korrelerede par bekræfter retningen.",
      weights={"correlation": .5, "trend": .3, "volatility": .2}, min_score=64, require_peers=True),
    S("kun-raavarer", "Kun råvarer", "Standard-regler, men kun guld, sølv og olie.",
      kinds=("metal", "energy"), min_score=60),
    S("kun-majors", "Kun majors", "Standard-regler, men kun de syv USD-majors.",
      instruments=("EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD", "NZD_USD")),
    S("swing", "Swing", "Større bevægelser: bredt stop, mål på 3R.",
      weights={"trend": .5, "momentum": .2, "volatility": .15, "carry": .15}, min_score=65,
      stop_atr=3.0, tp_r=3.0, trail_atr=3.0, max_hold_hours=240),
    S("scalp-overlap", "Scalp overlap", "Korte handler kun i London/New York-overlappet.",
      weights={"momentum": .4, "breakout": .3, "volatility": .3}, min_score=62,
      sessions_all=("london", "newyork"), stop_atr=1.0, tp_r=1.5, trail_atr=0.0, max_hold_hours=4),
    S("tilfaeldig", "Tilfældig (kontrol)", "Møntkast med samme risikostyring – skal slås af alle andre.",
      kind="random", random_prob=0.04),
]
BY_NAME = {s.name: s for s in STRATEGIES}


class Strategy:
    def __init__(self, params: StrategyParams, seed: int = 0):
        self.p = params
        self.rng = random.Random(f"{params.name}-{seed}")

    def score(self, sig: Signal) -> tuple:
        w = self.p.weights
        tot = sum(w.values()) or 1.0
        long_s = sum(v * sig.components.get(k, (50, 50))[0] for k, v in w.items()) / tot
        short_s = sum(v * sig.components.get(k, (50, 50))[1] for k, v in w.items()) / tot
        return round(long_s, 1), round(short_s, 1)

    def evaluate(self, sig: Signal) -> tuple:
        """(TradeIntent eller None, begrundelse)."""
        p = self.p
        sess = set(sig.context.get("sessions", ()))
        ins = market.instrument(sig.instrument)
        if p.instruments and sig.instrument not in p.instruments:
            return None, "instrument ikke i strategiens univers"
        if p.kinds and ins.kind not in p.kinds:
            return None, "instrumenttype ikke i strategiens univers"
        if p.sessions_any and not sess & set(p.sessions_any):
            return None, "forkert session"
        if p.sessions_only and sess != set(p.sessions_only):
            return None, "forkert session"
        if p.sessions_all and not set(p.sessions_all) <= sess:
            return None, "forkert session"
        if p.session_window:
            mins = [m for g, m in sig.context.get("minutes_in", {}).items()
                    if g in (p.sessions_any or sess) and m is not None]
            if not mins or min(mins) > p.session_window:
                return None, "uden for sessionsstart"
        mtn = sig.filter_info.minutes_to_news
        if p.news_window and mtn is not None and abs(mtn) <= p.news_window:
            return None, "strengt nyhedsvindue"
        if p.max_abs_trend and abs(sig.context.get("trend_tf", {}).get("H4", 0)) > p.max_abs_trend:
            return None, "for stærk trend til mean reversion"
        if p.require_peers and not sig.context.get("peers"):
            return None, "ingen korrelerede par"

        if p.kind == "random":
            if self.rng.random() >= p.random_prob:
                return None, "møntkast: ingen handel"
            d = self.rng.choice(("long", "short"))
            return TradeIntent(p.name, sig, d, 50.0, "tilfældig kontrol"), "ok"

        ls, ss = self.score(sig)
        d, best, other = ("long", ls, ss) if ls >= ss else ("short", ss, ls)
        if best < p.min_score:
            return None, f"score {best:.0f} < {p.min_score:.0f}"
        if best - other < p.min_edge:
            return None, "uklar retning"
        if p.kind == "breakout" and sig.components["breakout"][0 if d == "long" else 1] < 70:
            return None, "intet reelt brud"
        return TradeIntent(p.name, sig, d, best, f"{p.title}: {d} {best:.0f}"), "ok"
