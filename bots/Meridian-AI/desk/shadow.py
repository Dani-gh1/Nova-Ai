"""Skyggehandler: hvert scoret signal (over SHADOW_MIN_SCORE) følges som en fiktiv handel
med standard-stop og -mål, så man kan måle om høje scorer faktisk giver bedre udfald."""
from __future__ import annotations

from datetime import datetime, timedelta

from . import config

_M15 = timedelta(minutes=15)


class ShadowBook:
    def __init__(self, open_items: list | None = None, last: dict | None = None):
        self.open: list = open_items or []
        self.last: dict = last or {}  # "SYM:dir" -> ISO-tid for seneste skygge

    def add(self, sig, quote, now: datetime) -> dict | None:
        direction, score = sig.best
        if score < config.SHADOW_MIN_SCORE or quote is None:
            return None
        key = f"{sig.instrument}:{direction}"
        last = self.last.get(key)
        if last and datetime.fromisoformat(last) > now - timedelta(minutes=config.SHADOW_DEDUP_MIN):
            return None
        s = 1 if direction == "long" else -1
        entry = quote.ask if s > 0 else quote.bid
        stop_d = config.SHADOW_STOP_ATR * sig.atr_h1
        sh = {"id": f"sh-{sig.instrument}-{direction}-{now:%Y%m%d%H%M}", "instrument": sig.instrument,
              "direction": direction, "score": score, "opened_at": now.isoformat(timespec="seconds"),
              "entry": entry, "stop": entry - s * stop_d, "tp": entry + s * stop_d * config.SHADOW_TP_R,
              "risk": stop_d, "checked": now.isoformat(timespec="seconds")}
        self.open.append(sh)
        self.last[key] = sh["opened_at"]
        return sh

    def update(self, md, quotes: dict, now: datetime) -> list:
        done = []
        for sh in list(self.open):
            q = quotes.get(sh["instrument"])
            if q is None:
                continue
            hs = q.spread / 2
            s = 1 if sh["direction"] == "long" else -1
            outcome, r = None, 0.0
            last = datetime.fromisoformat(sh["checked"])
            for c in md.candles(sh["instrument"], "M15")[-40:]:
                ct = c.time + _M15
                if ct <= last or ct > now:
                    continue
                lo, hi = c.low - s * hs, c.high - s * hs
                if (s > 0 and lo <= sh["stop"]) or (s < 0 and hi >= sh["stop"]):
                    outcome, r = "stop", -1.0
                    break
                if (s > 0 and hi >= sh["tp"]) or (s < 0 and lo <= sh["tp"]):
                    outcome, r = "mål", config.SHADOW_TP_R
                    break
            sh["checked"] = now.isoformat(timespec="seconds")
            age_h = (now - datetime.fromisoformat(sh["opened_at"])).total_seconds() / 3600
            if outcome is None and age_h >= config.SHADOW_MAX_HOURS:
                px = q.bid if s > 0 else q.ask
                outcome, r = "tid", round(s * (px - sh["entry"]) / sh["risk"], 3)
            if outcome:
                sh.update(closed_at=now.isoformat(timespec="seconds"), outcome=outcome, r=r)
                self.open.remove(sh)
                done.append(sh)
        return done
