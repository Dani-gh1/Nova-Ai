"""Skyggehandler: hvert scoret signal får en hypotetisk 1x-handel på BEGGE sider med
faste regler (stop, mål, horisont). Bruges til at måle, om høj score faktisk giver
bedre udfald - uafhængigt af hvilke strategier der handlede."""
from __future__ import annotations

from . import config
from .models import Signal

HOUR_MS = 3_600_000


class ShadowBook:
    def __init__(self, logger):
        self.logger = logger
        self.open: dict[tuple[str, str], tuple] = {}
        self.reload()

    def reload(self) -> None:
        self.open = {(r[1], r[2]): r for r in self.logger.open_shadows()}

    def add(self, sig: Signal) -> int:
        n = 0
        for side, score in (("long", sig.long_score), ("short", sig.short_score)):
            if score < config.SHADOW_MIN_SCORE or (sig.symbol, side) in self.open:
                continue
            d = 1 if side == "long" else -1
            entry = sig.price
            stop = entry * (1 - d * config.SHADOW_STOP_PCT)
            tp = entry * (1 + d * config.SHADOW_TP_PCT)
            exp = sig.ts + int(config.SHADOW_HORIZON_HOURS * HOUR_MS)
            sid = self.logger.shadow_open(sig.ts, sig.symbol, side, score, entry, stop, tp, exp)
            self.open[(sig.symbol, side)] = (sid, sig.symbol, side, score, entry, stop, tp, exp)
            n += 1
        return n

    def update(self, bars: dict[str, tuple[float, float, float, float]], now_ms: int) -> int:
        closed = 0
        for key, (sid, sym, side, _score, entry, stop, tp, exp) in list(self.open.items()):
            bar = bars.get(sym)
            if not bar:
                continue
            o, h, low, c = bar
            res = None
            if side == "long":
                if low <= stop:
                    res = (min(o, stop), "stop")
                elif h >= tp:
                    res = (tp, "mål")
            else:
                if h >= stop:
                    res = (max(o, stop), "stop")
                elif low <= tp:
                    res = (tp, "mål")
            if res is None and now_ms >= exp:
                res = (c, "horisont")
            if res:
                px, why = res
                d = 1 if side == "long" else -1
                r = d * (px / entry - 1) * 100 - 2 * config.TAKER_FEE * 100
                self.logger.shadow_close(sid, px, now_ms, r, why)
                del self.open[key]
                closed += 1
        return closed
