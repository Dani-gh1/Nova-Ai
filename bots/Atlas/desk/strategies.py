"""Regelsæt der kører side om side, hver på sin egen virtuelle konto.

Et regelsæt bestemmer: hvilke Scout-signaler det reagerer på, en indgangsregel
(på Analystens features), egne score-vægte og minimumsscore, samt risiko- og
størrelsesparametre. Ikke-angivne værdier arves fra config.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

Feat = dict


def _ok(f: Feat, *keys) -> bool:
    return all(f.get(k) is not None for k in keys)


@dataclass(frozen=True)
class Strategy:
    name: str
    label: str
    description: str
    signals: frozenset = frozenset()            # tom = alle Scout-signaler accepteres
    entry: Callable[[Feat], bool] = lambda f: True
    entry_text: str = "Ingen ekstra indgangsregel"
    exit: Callable[[Feat, dict], bool] | None = None
    exit_text: str = ""
    weights: dict | None = None                 # None = config.SCORE_WEIGHTS
    min_score: float = 65.0
    position_pct: float | None = None
    max_positions: int | None = None
    atr_stop: float | None = None
    atr_trail: float | None = None
    max_hold_days: int | None = None
    take_profit_atr: float | None = None
    max_sector_pct: float | None = None
    universe: str = "all"                       # "all" | "stocks" | "etf"
    require_spy_uptrend: bool = False
    extra: dict = field(default_factory=dict)

    def p(self, key: str, cfg):
        """Strategiens parameter, ellers config-standarden."""
        v = getattr(self, key)
        if v is not None:
            return v
        return {
            "position_pct": cfg.POSITION_PCT, "max_positions": cfg.MAX_OPEN_POSITIONS,
            "atr_stop": cfg.ATR_STOP_MULT, "atr_trail": cfg.ATR_TRAIL_MULT,
            "max_hold_days": cfg.MAX_HOLD_DAYS, "max_sector_pct": cfg.MAX_SECTOR_PCT,
            "weights": cfg.SCORE_WEIGHTS,
        }[key]

    def score(self, components: dict, cfg) -> float:
        w = self.p("weights", cfg)
        tot = sum(w.values()) or 1.0
        return sum(components.get(k, 0.0) * v for k, v in w.items()) / tot

    def accepts(self, scored, cfg) -> tuple[bool, str]:
        f = scored.features
        if self.universe == "stocks" and scored.is_etf:
            return False, "kun aktier"
        if self.universe == "etf" and not scored.is_etf:
            return False, "kun ETF'er"
        if self.signals and not (self.signals & set(scored.signals)):
            return False, "intet relevant signal"
        if self.require_spy_uptrend and not f.get("spy_uptrend"):
            return False, "SPY ikke i optrend"
        try:
            if not self.entry(f):
                return False, "indgangsregel ikke opfyldt"
        except (TypeError, KeyError):
            return False, "manglende data til indgangsregel"
        sc = self.score(scored.components, cfg)
        if sc < self.min_score:
            return False, f"score {sc:.0f} < {self.min_score:.0f}"
        return True, f"score {sc:.0f}"


# --- exits -------------------------------------------------------------------
def exit_above_sma5(f: Feat, pos: dict) -> bool:
    return _ok(f, "sma5") and f["price"] > f["sma5"]


def exit_trend_break(f: Feat, pos: dict) -> bool:
    return _ok(f, "sma50") and f["price"] < f["sma50"]


def exit_rsi_high(f: Feat, pos: dict) -> bool:
    return _ok(f, "rsi2") and f["rsi2"] > 70


MOMO_W = {"momentum": 0.35, "rel_strength": 0.25, "volume": 0.10, "trend": 0.15, "volatility": 0.05, "market": 0.10}
MR_W = {"momentum": 0.0, "rel_strength": 0.10, "volume": 0.10, "trend": 0.35, "volatility": 0.20, "market": 0.25}
BRK_W = {"momentum": 0.20, "rel_strength": 0.20, "volume": 0.30, "trend": 0.15, "volatility": 0.05, "market": 0.10}

STRATEGIES: list[Strategy] = [
    Strategy("baseline", "Basis", "Standardvægte og -tærskler fra config.py.", min_score=65),
    Strategy("momentum", "Momentum", "Køber stærke aktier med højt 20/60-dages afkast og relativ styrke.",
             entry=lambda f: _ok(f, "ret20", "sma50") and f["ret20"] > 0.05 and f["price"] > f["sma50"],
             entry_text="20-dages afkast > 5 % og kurs over SMA50", weights=MOMO_W, min_score=68),
    Strategy("breakout", "Breakout", "Ny 52-ugers top på høj relativ volumen.",
             signals=frozenset({"new_high52"}),
             entry=lambda f: f.get("rvol", 0) and f["rvol"] >= 1.5,
             entry_text="Ny 52-ugers top og relativ volumen ≥ 1,5", weights=BRK_W, min_score=62,
             atr_trail=2.5),
    Strategy("mean_reversion", "Mean reversion", "Køber kortvarigt oversolgte aktier i langsigtet optrend, sælger ved rebound.",
             signals=frozenset({"oversold", "gap_down", "pullback"}),
             entry=lambda f: _ok(f, "rsi2", "sma200") and f["rsi2"] < 10 and f["price"] > f["sma200"],
             entry_text="RSI(2) < 10 og kurs over SMA200",
             exit=exit_above_sma5, exit_text="Sælg når kursen lukker over SMA5",
             weights=MR_W, min_score=55, atr_stop=2.5, max_hold_days=7),
    Strategy("gap_go", "Gap op", "Gap-and-go: gap op på volumen der holder over åbningskursen.",
             signals=frozenset({"gap_up"}),
             entry=lambda f: f["gap"] >= 0.02 and f["price"] >= f["open"] and (f.get("rvol") or 0) >= 1.5,
             entry_text="Gap ≥ 2 %, kurs over dagens åbning, relativ volumen ≥ 1,5",
             weights=BRK_W, min_score=58, atr_stop=1.5, max_hold_days=5),
    Strategy("gap_fade", "Gap ned-rebound", "Køber gap ned i aktier i optrend, når kursen vender over åbningen.",
             signals=frozenset({"gap_down"}),
             entry=lambda f: _ok(f, "sma200") and f["gap"] <= -0.02 and f["price"] > f["open"] and f["price"] > f["sma200"],
             entry_text="Gap ≤ −2 %, kurs over åbning og over SMA200",
             exit=exit_rsi_high, exit_text="Sælg når RSI(2) > 70",
             weights=MR_W, min_score=55, max_hold_days=5),
    Strategy("dip_uptrend", "Dip-køb i optrend", "Køber tilbagefald til omkring SMA20 når SMA50 > SMA200.",
             signals=frozenset({"pullback", "oversold", "gap_down"}),
             entry=lambda f: _ok(f, "sma20", "sma50", "sma200") and f["sma50"] > f["sma200"]
             and f["price"] > f["sma50"] and f["price"] < f["sma20"] * 1.01,
             entry_text="SMA50 > SMA200, kurs mellem SMA50 og SMA20",
             exit=exit_trend_break, exit_text="Sælg hvis kursen bryder under SMA50",
             weights=MR_W, min_score=58, max_hold_days=15),
    Strategy("picky", "Kræsen", "Kun de allerbedste: høj score, SPY i optrend, få positioner.",
             min_score=80, max_positions=5, position_pct=0.12, require_spy_uptrend=True),
    Strategy("aggressive", "Aggressiv", "Lav tærskel, store positioner, brede stop.",
             min_score=55, position_pct=0.15, max_positions=8, atr_stop=3.0, atr_trail=4.0,
             max_sector_pct=0.5, weights=MOMO_W),
    Strategy("many_small", "Mange små", "Mange små positioner for bred spredning.",
             min_score=60, position_pct=0.03, max_positions=30),
    Strategy("trend_follow", "Trendfølger", "Klassisk trend: SMA20 > SMA50 > SMA200, lange hold.",
             entry=lambda f: _ok(f, "sma20", "sma50", "sma200") and f["sma20"] > f["sma50"] > f["sma200"],
             entry_text="SMA20 > SMA50 > SMA200",
             exit=exit_trend_break, exit_text="Sælg under SMA50",
             min_score=62, atr_trail=4.0, max_hold_days=60),
    Strategy("rs_leaders", "RS-ledere", "Stærkest relative styrke mod både sektor og indeks.",
             entry=lambda f: (f.get("rs_sector20") or 0) > 0.03 and (f.get("rs_spy20") or 0) > 0.03,
             entry_text="Slår sektor og SPY med > 3 % over 20 dage",
             weights={"momentum": 0.15, "rel_strength": 0.45, "volume": 0.10, "trend": 0.15, "volatility": 0.05, "market": 0.10},
             min_score=65),
    Strategy("volume_surge", "Volumenstød", "Usædvanlig volumen (≥ 3× normalt) med positiv kurs.",
             signals=frozenset({"rvol"}),
             entry=lambda f: (f.get("rvol") or 0) >= 3 and (f.get("ret1") or 0) > 0,
             entry_text="Relativ volumen ≥ 3 og dagen er positiv",
             weights=BRK_W, min_score=58, max_hold_days=10),
    Strategy("low_vol", "Lav volatilitet", "Rolige aktier i optrend (ATR < 2 %).",
             entry=lambda f: _ok(f, "atr_pct", "sma50") and f["atr_pct"] < 0.02 and f["price"] > f["sma50"],
             entry_text="ATR < 2 % af kursen og kurs over SMA50",
             weights={"momentum": 0.15, "rel_strength": 0.15, "volume": 0.05, "trend": 0.30, "volatility": 0.20, "market": 0.15},
             min_score=62, atr_stop=2.5, position_pct=0.10),
    Strategy("etf_rotation", "ETF-rotation", "Kun ETF'er med momentum.",
             universe="etf",
             entry=lambda f: _ok(f, "ret60") and f["ret60"] > 0,
             entry_text="ETF med positivt 60-dages afkast", weights=MOMO_W, min_score=55,
             position_pct=0.15, max_positions=6, max_sector_pct=1.0, max_hold_days=40),
    Strategy("market_filter", "Markedsfilter", "Basis, men handler kun når SPY er i optrend.",
             require_spy_uptrend=True, min_score=63),
    Strategy("quick_swing", "Hurtig swing", "Korte hold med gevinstmål på 2×ATR.",
             min_score=62, take_profit_atr=2.0, atr_stop=1.5, max_hold_days=4, weights=MOMO_W),
]

BY_NAME = {s.name: s for s in STRATEGIES}
assert len(BY_NAME) == len(STRATEGIES) >= 15
