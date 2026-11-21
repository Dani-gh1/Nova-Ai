"""Fælles grundlag for Smart strategi (porteføljen i portfolio.py): data fra botterne, gemt konto og baggrundsløkken.

Hver bot sender sine strategier ind (feed). En bot, der ikke svarer, beholder sit seneste svar, så et langsomt svar
aldrig får strategier til at forsvinde. Kontoen gemmes efter hvert skridt og regnes aldrig om bagud.
"""
from __future__ import annotations

import bisect
import json
import threading
import time
from pathlib import Path

TICK_S = 60                 # så tit der tages et skridt
REFRESH_S = 300             # så tit der hentes friske tal fra botterne (skåner en lille server; strategierne ændrer sig kun ved handler)
MIN_HISTORY_H = 12          # så lang historik skal en strategi mindst have
MIN_TRADES = 20             # så mange lukkede handler skal en strategi mindst have
DEAD_H = 2                  # en strategi, hvis bot ikke har svaret så længe, forlades
STALE_S = 30 * 60           # en bot, der ikke har svaret så længe, kan ikke vælges
MAX_POINTS = 3000           # gemte punkter i grafen (tyndes ud, når der er flere)
FLAT_H = 3                  # står en strategi stille så længe (lukket marked, ingen handler), kan den ikke vælges
WARMUP_S = 15 * 60          # efter start: vælg først, når alle botter har svaret (eller der er gået så længe)


def _at(curve: list, t: float):
    i = bisect.bisect_right([p[0] for p in curve], t) - 1
    return curve[i][1] if i >= 0 else None


class SmartBot:
    """Grundklassen: underklassen laver _new, step og view."""

    def __init__(self, path: Path, now=time.time, log=print, expected: int = 0, allow: set | None = None):
        self.path, self.now, self.log = path, now, log
        self.allow = allow                                   # kun disse botter (live: dem, der kan handles for rigtigt)
        self.expected, self.started = expected, now()      # så mange botter venter den på efter start
        self.lock = threading.Lock()
        self.snap: dict[str, dict] = {}        # bot -> {"at": tid, "fresh": scanner, "cands": [...]} (seneste svar)
        self.state = self._load()

    def _new(self) -> dict:
        raise NotImplementedError

    def _load(self) -> dict:
        raise NotImplementedError

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            self.log(f"Smart: kunne ikke gemme kontoen: {e}")

    def feed(self, bot: str, cands: list[dict], fresh: bool = True) -> None:
        """Seneste svar fra én bot."""
        if self.allow is not None and bot not in self.allow:
            return
        with self.lock:
            self.snap[bot] = {"at": self.now(), "fresh": fresh, "cands": cands}

    def _all(self) -> dict[str, tuple[dict, dict]]:
        return {c["id"]: (c, s) for s in self.snap.values() for c in s["cands"]}

    @staticmethod
    def _flat(c: dict, now: float) -> bool:
        """Har strategien stået stille i FLAT_H timer (lukket marked eller ingen handler)?"""
        moved = c.get("moved_at")
        return moved is not None and now - moved >= FLAT_H * 3600

    def step(self) -> None:
        raise NotImplementedError

    def run(self, refresh, stop: threading.Event | None = None) -> None:
        """Baggrundsløkken: hent friske data fra botterne (refresh) hvert REFRESH_S sekund, og tag et skridt hvert TICK_S."""
        stop = stop or threading.Event()
        last = 0.0
        while not stop.is_set():
            try:
                if self.now() - last >= REFRESH_S:
                    last = self.now()
                    refresh()
                self.step()
            except Exception as e:                  # botten må aldrig dø af én dårlig runde
                self.log(f"Smart: fejl i runden: {e!r}")
            stop.wait(TICK_S)
