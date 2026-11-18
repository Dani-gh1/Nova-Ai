"""Indlæser strategier fra config.STRATEGIES og udfylder standardværdier."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

PROFILES = ("balanced", "momentum", "trend", "breakout", "meanrev", "lowvol", "highvol", "hold")


@dataclass(frozen=True)
class Strategy:
    name: str
    label: str
    description: str
    profile: str
    min_score: float
    position_pct: float
    max_positions: int
    stop_pct: float
    trail_activate_pct: float
    trail_pct: float
    max_hold_hours: float
    quarantine_minutes: float
    btc_filter: bool = True
    min_rel_vol: float = 0.0
    max_volatility_pct: float = 0.0     # 0 = ingen grænse
    top_n: int = 0                      # 0 = alle
    symbols: Optional[tuple] = None     # None = alle

    def allows(self, symbol: str, rank: int) -> bool:
        if self.symbols is not None and symbol not in self.symbols:
            return False
        if self.top_n and rank > self.top_n:
            return False
        return True


def load_strategies(cfg) -> list[Strategy]:
    out, seen = [], set()
    for raw in cfg.STRATEGIES:
        d = dict(raw)
        name = d["name"]
        if name in seen:
            raise ValueError(f"strateginavn bruges to gange: {name}")
        seen.add(name)
        profile = d.get("profile", "balanced")
        if profile not in PROFILES:
            raise ValueError(f"ukendt profil {profile!r} i {name}")
        syms = d.get("symbols")
        out.append(Strategy(
            name=name,
            label=d.get("label", name),
            description=d.get("description", ""),
            profile=profile,
            min_score=float(d.get("min_score", 70)),
            position_pct=float(d.get("position_pct", cfg.POSITION_PCT)),
            max_positions=int(d.get("max_positions", cfg.MAX_POSITIONS)),
            stop_pct=float(d.get("stop_pct", cfg.STOP_PCT)),
            trail_activate_pct=float(d.get("trail_activate_pct", cfg.TRAIL_ACTIVATE_PCT)),
            trail_pct=float(d.get("trail_pct", cfg.TRAIL_PCT)),
            max_hold_hours=float(d.get("max_hold_hours", cfg.MAX_HOLD_HOURS)),
            quarantine_minutes=float(d.get("quarantine_minutes", cfg.QUARANTINE_MINUTES)),
            btc_filter=bool(d.get("btc_filter", True)),
            min_rel_vol=float(d.get("min_rel_vol", 0.0)),
            max_volatility_pct=float(d.get("max_volatility_pct", 0.0)),
            top_n=int(d.get("top_n", 0)),
            symbols=tuple(syms) if syms else None,
        ))
    return out
