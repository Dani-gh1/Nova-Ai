"""Risk: sætter stop og trailing stop ud fra ATR, håndhæver maks. eksponering
pr. sektor og overvåger åbne positioner (stop, trailing, gevinstmål, maks. holdetid,
strategiens egne exit-regler).
Output: Order (eller afvisning) til Executor.
"""
from __future__ import annotations

from datetime import date

from ..models import Order, Scored


class Risk:
    name = "Risk"

    def __init__(self, cfg, clock=None, logger=None):
        self.cfg = cfg
        self.clock = clock
        self.log = logger

    # --- nye positioner -------------------------------------------------------
    def approve(self, s: Scored, strat, account: dict, equity: float, prices: dict[str, float]):
        cfg = self.cfg
        f = s.features
        a = f.get("atr")
        if not a or a <= 0:
            return None, "ATR mangler"
        stop = s.price - strat.p("atr_stop", cfg) * a
        if stop <= 0 or (s.price - stop) / s.price > cfg.MAX_STOP_PCT:
            return None, f"stop for langt væk ({(s.price - stop) / s.price * 100:.1f} %)"
        # Sektoreksponering inkl. den nye position
        sector = s.sector or "Ukendt"
        cur = sum(p["qty"] * prices.get(sym, p["entry"]) for sym, p in account["positions"].items()
                  if p.get("sector") == sector)
        new = equity * strat.p("position_pct", cfg)
        limit = strat.p("max_sector_pct", cfg)
        if equity > 0 and (cur + new) / equity > limit + 1e-9:
            return None, f"sektorloft {sector} ({(cur + new) / equity * 100:.0f} % > {limit * 100:.0f} %)"
        order = Order(strat.name, s.symbol, "buy", s.price, f"{strat.label}: {','.join(s.signals)}",
                      sector, round(stop, 4), round(a, 4), strat.p("atr_trail", cfg),
                      strat.score(s.components, cfg), f.get("spread_pct"))
        if self.log:
            self.log.handoff(self.name, "Executor", s.symbol, {"strategy": strat.name, "stop": order.stop, "atr": a})
        return order, "ok"

    # --- åbne positioner --------------------------------------------------------
    def update_trailing(self, pos: dict, high_price: float) -> None:
        if high_price > pos.get("high", pos["entry"]):
            pos["high"] = high_price
        trail = pos["high"] - pos["trail_mult"] * pos["atr"]
        pos["trail"] = max(pos.get("trail") or 0.0, trail)

    def active_stop(self, pos: dict) -> float:
        return max(pos["stop"], pos.get("trail") or 0.0)

    def hold_days(self, pos: dict, today: date) -> int:
        opened = date.fromisoformat(pos["opened"][:10])
        if self.clock:
            return self.clock.trading_days_between(opened, today)
        return (today - opened).days

    def review(self, strat, sym: str, pos: dict, f: dict | None, today: date,
               price: float) -> str | None:
        """Returnerer en exit-grund eller None. Opdaterer trailing stop."""
        cfg = self.cfg
        self.update_trailing(pos, price)
        stop = self.active_stop(pos)
        if price <= stop:
            return "trailing stop" if (pos.get("trail") or 0) >= pos["stop"] else "stop"
        tp = strat.take_profit_atr
        if tp and price >= pos["entry"] + tp * pos["atr"]:
            return "gevinstmål"
        if self.hold_days(pos, today) >= strat.p("max_hold_days", cfg):
            return "maks. holdetid"
        if strat.exit and f is not None:
            try:
                if strat.exit(f, pos):
                    return strat.exit_text or "exit-regel"
            except (TypeError, KeyError):
                pass
        return None

    def sell_order(self, strat, sym: str, pos: dict, price: float, reason: str) -> Order:
        if self.log:
            self.log.handoff(self.name, "Executor", sym, {"strategy": strat.name, "exit": reason})
        return Order(strat.name, sym, "sell", price, reason, pos.get("sector", ""))
