"""Scout: finder kandidater i hele universet.

Signaler: stor relativ volumen ('rvol'), gap op/ned ('gap_up'/'gap_down'),
ny 52-ugers top ('new_high52') og – hvis slået til – 'pullback' og 'oversold'
som dip-køb/mean reversion-regelsættene bruger.
Output: list[Candidate] (sorteret efter prioritet).
"""
from __future__ import annotations

from ..indicators import compute_features
from ..models import Candidate, Snapshot


class Scout:
    name = "Scout"

    def __init__(self, cfg, logger=None):
        self.cfg = cfg
        self.log = logger

    def signals_for(self, f: dict) -> list[str]:
        c = self.cfg
        sig = []
        if f.get("rvol") is not None and f["rvol"] >= c.SCOUT_MIN_RVOL:
            sig.append("rvol")
        if f.get("gap", 0) >= c.SCOUT_GAP_PCT:
            sig.append("gap_up")
        if f.get("gap", 0) <= -c.SCOUT_GAP_PCT:
            sig.append("gap_down")
        if f.get("new_high52"):
            sig.append("new_high52")
        if c.SCOUT_EXTRA_SIGNALS:
            if (f.get("sma50") and f.get("sma20") and f.get("ret5") is not None
                    and f["price"] > f["sma50"] and f["price"] < f["sma20"] and f["ret5"] <= c.SCOUT_PULLBACK_RET5):
                sig.append("pullback")
            if (f.get("rsi2") is not None and f.get("sma200") and f["rsi2"] < c.SCOUT_OVERSOLD_RSI2
                    and f["price"] > f["sma200"]):
                sig.append("oversold")
        return sig

    def run(self, snapshots: dict[str, Snapshot], features: dict[str, dict] | None = None) -> list[Candidate]:
        out = []
        for sym, snap in snapshots.items():
            f = features.get(sym) if features else None
            if f is None:
                f = compute_features(snap, self.cfg)
                if features is not None:
                    features[sym] = f
            sig = self.signals_for(f)
            if not sig:
                continue
            prio = (f.get("rvol") or 0) + abs(f.get("gap") or 0) * 50 + (1.0 if f.get("new_high52") else 0) + len(sig) * 0.5
            out.append(Candidate(sym, tuple(sig), round(prio, 3), f, snap))
        out.sort(key=lambda c: c.priority, reverse=True)
        out = out[: self.cfg.SCOUT_MAX_CANDIDATES]
        if self.log:
            self.log.event(self.name, f"{len(out)} kandidater af {len(snapshots)} symboler",
                           data={"top": [(c.symbol, c.signals) for c in out[:15]]})
            for c in out:
                self.log.handoff(self.name, "Filter", c.symbol, {"signals": c.signals, "priority": c.priority})
        return out
