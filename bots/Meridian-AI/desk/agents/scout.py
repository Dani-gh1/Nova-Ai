"""Scout: vælger instrumenter i aktive sessioner med nok udsving."""
from __future__ import annotations

from datetime import datetime

from .. import config, indicators as ind, market
from ..models import ScoutPick, ScoutReport


class Scout:
    name = "scout"

    def run(self, now: datetime, md) -> ScoutReport:
        st = market.status(now)
        picks, skipped = [], []
        if not st.open:
            return ScoutReport(now, st, (), tuple((s, "marked lukket") for s in config.INSTRUMENTS))
        for sym in config.INSTRUMENTS:
            if not market.is_instrument_open(sym, now):
                skipped.append((sym, "instrument holder pause"))
                continue
            h1 = md.candles(sym, "H1")
            if len(h1) < config.EMA_SLOW + 10:
                skipped.append((sym, "for lidt historik"))
                continue
            ratio = ind.atr_ratio(h1, config.ATR_LEN, config.ATR_MEDIAN_LOOKBACK)
            own = tuple(s for s in st.sessions if s in config.INSTRUMENT_SESSIONS.get(sym, ()))
            need = config.SCOUT_MIN_ATR_RATIO if own else config.SCOUT_OFFSESSION_ATR_RATIO
            if ratio < need:
                why = "uden for egen session" if not own else "for lidt udsving"
                skipped.append((sym, f"{why} (ATR-ratio {ratio:.2f} < {need:.2f})"))
                continue
            reason = (f"session {'+'.join(own)}" if own else "stort udsving uden for egen session")
            picks.append(ScoutPick(sym, round(ratio, 3), own, f"{reason}, ATR-ratio {ratio:.2f}"))
        picks.sort(key=lambda p: (0 if p.sessions else 1, -p.atr_ratio))
        keep = picks[:config.SCOUT_MAX_CANDIDATES]
        skipped += [(p.instrument, "uden for top-liste") for p in picks[config.SCOUT_MAX_CANDIDATES:]]
        return ScoutReport(now, st, tuple(keep), tuple(skipped))
