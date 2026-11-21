"""Smart-botten: en konto, der hele tiden følger den bedste strategi i alle botterne.

Den kører i baggrunden i hubben (hvert minut), uanset om nogen kigger på siden, og gemmer sin konto i
logs/smart-bot.json. Historikken regnes aldrig om: hvert skridt bygger på det forrige, præcis som en rigtig konto.

Reglerne (valgt til rigtige penge – hellere lidt for forsigtig end at jagte held):
  * Kun strategier, der har bevist noget: mindst MIN_TRADES lukkede handler og MIN_HISTORY_H timers historik,
    og hvis bot svarer og scanner lige nu.
  * Point = afkast efter omkostninger de seneste LOOKBACK_H timer minus halvdelen af det største fald i samme periode.
    En strategi, der stiger stille og roligt, slår en, der er steget lige så meget med store udsving.
  * Skifter kun, når en anden har mindst SWITCH_MARGIN point mere, og tidligst MIN_HOLD_H timer efter sidste skift.
    Så betaler den ikke skiftegebyrer for at hoppe frem og tilbage på støj.
  * Nødudgang: falder kontoen STOP_PCT % fra toppen, siden den gik ind i strategien, eller svarer strategiens bot ikke
    i DEAD_H timer, forlades den med det samme (og den må ikke vælges igen i COOLDOWN_H timer).
  * Har ingen strategi positive point, står pengene kontant og venter.
  * Omkostninger: hver handel i den fulgte strategi koster det samme som i strategien selv (botens Rigtige penge-tal),
    og et skift koster at lukke den gamle strategis åbne positioner og åbne den nyes.
"""
from __future__ import annotations

import bisect
import json
import os
import threading
import time
from pathlib import Path

VERSION = 3
TICK_S = 60                 # så tit botten tager et skridt
REFRESH_S = 300             # så tit den henter friske tal fra botterne (skåner en lille server; strategierne ændrer sig kun ved handler)
LOOKBACK_H = 48             # point regnes over så mange timer (eller det, der findes, hvis historikken er kortere)
MIN_HISTORY_H = 12          # så lang historik skal en strategi mindst have
MIN_TRADES = 20             # så mange lukkede handler skal en strategi mindst have
SWITCH_MARGIN = 3.0         # så mange point mere skal en anden have, før der skiftes
MIN_HOLD_H = 12             # tidligst så mange timer efter sidste skift (undtagen nødudgang)
STOP_PCT = 8.0              # nødudgang: fald fra toppen siden indgang
DEAD_H = 2                  # nødudgang: strategiens bot har ikke svaret så længe
STALE_S = 30 * 60           # en bot, der ikke har svaret så længe, kan ikke vælges
COOLDOWN_H = 24             # en strategi, der er forladt via nødudgang, må ikke vælges så længe
MAX_POINTS = 3000           # gemte punkter i grafen (tyndes ud, når der er flere)
WARMUP_S = 15 * 60          # efter start: vælg først, når alle botter har svaret (eller der er gået så længe)

RULES = {"lookback_h": LOOKBACK_H, "min_history_h": MIN_HISTORY_H, "min_trades": MIN_TRADES, "switch_margin": SWITCH_MARGIN,
         "min_hold_h": MIN_HOLD_H, "warmup_min": WARMUP_S // 60, "stop_pct": STOP_PCT, "dead_h": DEAD_H, "cooldown_h": COOLDOWN_H}


def state_file() -> Path:
    return Path(os.environ.get("HUB_SMART_FILE") or Path(__file__).resolve().parent.parent / "logs" / "smart-bot.json")


def _at(curve: list, t: float):
    i = bisect.bisect_right([p[0] for p in curve], t) - 1
    return curve[i][1] if i >= 0 else None


def score(c: dict, now: float, lookback_h: float = LOOKBACK_H) -> float | None:
    """Point: afkast efter omkostninger over perioden minus halvdelen af det største fald i perioden (i procent)."""
    cv = c.get("curve") or []
    if len(cv) < 2:
        return None
    start = now - lookback_h * 3600
    base = _at(cv, start)
    pts = [p for p in cv if p[0] >= start]
    if base is None:                       # kortere historik end perioden: brug hele historikken
        base, pts = cv[0][1], cv
    if 100 + base <= 0:
        return None
    eq = [100 + base] + [100 + p[1] for p in pts]
    peak, dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak * 100)
    ret = (eq[-1] / eq[0] - 1) * 100
    return ret - 0.5 * dd


class SmartBot:
    def __init__(self, path: Path | None = None, now=time.time, log=print, expected: int = 0):
        self.path, self.now, self.log = path or state_file(), now, log
        self.expected, self.started = expected, now()     # så mange botter venter den på efter start
        self.lock = threading.Lock()
        self.snap: dict[str, dict] = {}        # bot -> {"at": tid, "fresh": scanner, "cands": [...]} (seneste svar)
        self.state = self._load()

    # ---------- gemt konto ----------
    def _new(self, capital: float = 10000.0, currency: str = "kr.") -> dict:
        t = round(self.now(), 1)
        return {"version": VERSION, "since": t, "capital": capital, "currency": currency, "acct": 1.0, "held": None,
                "switches": [], "curve": [[round(t), 0.0]], "cooldown": {}, "updated": t, "costs_paid": 0.0}

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("version") == VERSION and isinstance(d.get("acct"), (int, float)):
                return d
        except (OSError, ValueError):
            pass
        return self._new()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            self.log(f"Smart-bot: kunne ikke gemme kontoen: {e}")

    # ---------- data fra botterne ----------
    def feed(self, bot: str, cands: list[dict], fresh: bool = True) -> None:
        """Seneste svar fra én bot. Bot, der ikke svarer, beholder deres sidste svar (med det gamle tidspunkt)."""
        with self.lock:
            self.snap[bot] = {"at": self.now(), "fresh": fresh, "cands": cands}

    def _all(self) -> dict[str, tuple[dict, dict]]:
        return {c["id"]: (c, s) for s in self.snap.values() for c in s["cands"]}

    def _eligible(self, now: float) -> dict[str, float]:
        st, out = self.state, {}
        for cid, (c, s) in self._all().items():
            if now - s["at"] > STALE_S or not s["fresh"] or st["cooldown"].get(cid, 0) > now:
                continue
            cv = c.get("curve") or []
            if (c.get("trades") or 0) < MIN_TRADES or len(cv) < 2 or now - cv[0][0] < MIN_HISTORY_H * 3600:
                continue
            sc = score(c, now)
            if sc is not None:
                out[cid] = sc
        return out

    # ---------- ét skridt ----------
    def step(self) -> None:
        with self.lock:
            now = self.now()
            st, allc = self.state, self._all()
            if not allc:
                return
            caps = [(c.get("capital"), c.get("currency")) for c, _ in allc.values() if c.get("capital")]
            if caps and st["held"] is None and not st["switches"]:        # endnu ingen handler: brug de mest almindelige tal
                st["capital"], st["currency"] = max(set(caps), key=caps.count)
            h = st["held"]
            if h and h["id"] in allc:                                       # følg strategiens bevægelse siden sidst
                c, s = allc[h["id"]]
                v, tr, op = c["raw_now"], c.get("trades") or 0, c.get("open") or 0
                if s["at"] > h.get("seen", 0):
                    if tr < h["trades"]:                                    # strategien er startet forfra: synk uden gevinst/tab
                        self.log(f"Smart-bot: {h['name']} ({h['bot_name']}) er startet forfra – følger videre fra nu")
                    else:
                        if 100 + h["v"] > 0:
                            st["acct"] = max(0.0, st["acct"] * (100 + v) / (100 + h["v"]))
                        units = max(0.0, (tr - h["trades"]) + (op - h["open"]) / 2)      # lukket = ud, åbnet = ind
                        self._pay(units * c.get("per", 0.0))
                    h.update(v=v, trades=tr, open=op, seen=s["at"])
                    h["peak"] = max(h.get("peak", st["acct"]), st["acct"])
            scores = self._eligible(now)
            if len(self.snap) >= self.expected or now - self.started >= WARMUP_S:
                self._decide(now, scores, allc)        # først når alle botter har svaret: aldrig vælg blandt et halvt felt
            h = st["held"]
            if h:
                h["score"] = scores[h["id"]] if h["id"] in scores else (score(allc[h["id"]][0], now) if h["id"] in allc else None)
            st["eligible"], st["count"], st["bots"] = len(scores), len(allc), len({c["bot"] for c, _ in allc.values()})
            pct = round((st["acct"] - 1) * 100, 4)
            if not st["curve"] or st["curve"][-1][1] != pct or now - st["curve"][-1][0] >= 600:
                st["curve"].append([round(now), pct])
                if len(st["curve"]) > MAX_POINTS:
                    st["curve"] = st["curve"][:1] + st["curve"][1:-1:2] + st["curve"][-1:]
            st["updated"] = round(now, 1)
            self._save()

    def _pay(self, cost_pct: float) -> None:
        if cost_pct > 0:
            st = self.state
            before = st["acct"]
            st["acct"] = max(0.0, st["acct"] * (1 - cost_pct / 100))
            st["costs_paid"] = st.get("costs_paid", 0.0) + (before - st["acct"]) * st["capital"]

    def _decide(self, now: float, scores: dict, allc: dict) -> None:
        st, h = self.state, self.state["held"]
        best = max(scores, key=scores.get) if scores else None
        best_sc = scores.get(best) if best else None
        if h is None:
            if best is not None and best_sc > 0:
                self._switch(now, best, allc, "bedste strategi" if not st["switches"] else "ny strategi med positive point")
            return
        seen = allc.get(h["id"], (None, {"at": h.get("seen", 0)}))[1]["at"]
        if now - max(seen, h.get("seen", 0)) > DEAD_H * 3600:
            return self._leave(now, scores, allc, f"{h['bot_name']} har ikke svaret i {DEAD_H} timer")
        if st["acct"] < h.get("peak", st["acct"]) * (1 - STOP_PCT / 100):
            return self._leave(now, scores, allc, f"kontoen faldt over {STOP_PCT:g} % fra toppen")
        if now - h["entered"] < MIN_HOLD_H * 3600:
            return
        cur = scores.get(h["id"])
        if cur is None:
            cur = score(allc[h["id"]][0], now) if h["id"] in allc else None
        if best is not None and best != h["id"] and best_sc > 0 and (cur is None or best_sc >= cur + SWITCH_MARGIN):
            self._switch(now, best, allc, f"{best_sc:+.1f} point mod {cur:+.1f}" if cur is not None else "bedre strategi")
        elif (cur is None or cur <= 0) and (best_sc is None or best_sc <= 0):
            self._switch(now, None, allc, "ingen strategi med positive point – venter kontant")

    def _leave(self, now: float, scores: dict, allc: dict, why: str) -> None:
        h = self.state["held"]
        self.state["cooldown"][h["id"]] = now + COOLDOWN_H * 3600
        scores = {k: v for k, v in scores.items() if k != h["id"] and v > 0}
        self._switch(now, max(scores, key=scores.get) if scores else None, allc, "nødudgang: " + why)

    def _switch(self, now: float, to: str | None, allc: dict, why: str) -> None:
        st, h = self.state, self.state["held"]
        if h and h["id"] in allc:                                  # luk den gamle strategis åbne positioner
            self._pay((h.get("open") or 0) * allc[h["id"]][0].get("per", 0.0) / 2)
        new = None
        if to is not None:
            c, s = allc[to]
            self._pay((c.get("open") or 0) * c.get("per", 0.0) / 2)       # åbn den nyes positioner
            new = {"id": to, "name": c["name"], "bot": c["bot"], "bot_name": c["bot_name"], "v": c["raw_now"],
                   "trades": c.get("trades") or 0, "open": c.get("open") or 0, "seen": s["at"], "entered": round(now),
                   "peak": st["acct"]}
        st["switches"].append({"ts": round(now), "from": h and h["name"], "from_bot": h and h["bot_name"],
                               "to": new and new["name"], "to_bot": new and new["bot_name"], "why": why,
                               "value": round(st["capital"] * st["acct"], 2)})
        st["held"] = new
        self.log(f"Smart-bot: {(h and h['name']) or 'kontant'} → {(new and new['name'] + ' (' + new['bot_name'] + ')') or 'kontant'}: {why}")

    # ---------- til dashboardet ----------
    def view(self, tax: float = 0.0) -> dict:
        with self.lock:
            st, h, now = self.state, self.state["held"], self.now()
            keep = lambda v: v * (1 - tax / 100) if v > 0 else v
            ret = keep((st["acct"] - 1) * 100)
            curve = st["curve"]
            if len(curve) > 200:
                k = len(curve) / 200
                curve = [curve[int(i * k)] for i in range(200)] + [curve[-1]]
            stale = None
            if h:
                seen = self.snap.get(h["bot"], {}).get("at") or h.get("seen")
                if seen and now - seen > STALE_S:
                    stale = f"{h['bot_name']} svarer ikke lige nu – positionen holdes"
            warm = len(self.snap) < self.expected and now - self.started < WARMUP_S
            if warm and not stale:
                stale = f"Starter op: {len(self.snap)} af {self.expected} botter har svaret – vælger, når alle er med"
            return {"return_pct": round(ret, 2), "capital": st["capital"], "currency": st["currency"],
                    "value": round(st["capital"] * (1 + ret / 100), 2), "costs_paid": round(st.get("costs_paid", 0.0), 2),
                    "following": h and h["name"], "following_bot": h and h["bot_name"], "following_key": h and h["bot"],
                    "score": round(h["score"], 2) if h and h.get("score") is not None else None,
                    "held_since": h and h["entered"],
                    "next_switch": h and max(h["entered"] + MIN_HOLD_H * 3600, round(now)),
                    "count": st.get("count", 0), "eligible": st.get("eligible", 0), "bots": st.get("bots", 0),
                    "switches": len(st["switches"]), "recent": st["switches"][-8:][::-1], "since": round(st["since"]),
                    "updated": st.get("updated"), "warning": stale, "rules": RULES,
                    "curve": [[t, round(keep(v), 3)] for t, v in curve]}

    def run(self, refresh, stop: threading.Event | None = None) -> None:
        """Baggrundsløkken: hent friske data fra botterne (refresh) og tag et skridt, hvert TICK_S sekund."""
        stop = stop or threading.Event()
        last = 0.0
        while not stop.is_set():
            try:
                if self.now() - last >= REFRESH_S:
                    last = self.now()
                    refresh()
                self.step()
            except Exception as e:                  # botten må aldrig dø af én dårlig runde
                self.log(f"Smart-bot: fejl i runden: {e!r}")
            stop.wait(TICK_S)
