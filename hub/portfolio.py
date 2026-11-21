"""Smart-porteføljen: den primære strategi. Holder op til SLOTS strategier på én gang, fordelt på flere markeder,
og kun strategier, der vokser stabilt. Resten står kontant.

Regler (alle tal er efter gebyrer, fordi botterne selv regner dem med):
  * Vokser stabilt = i plus over både 6 timer, 24 timer og 3 dage (eller hele historikken, hvis den er kortere),
    mindst MIN_TRADES lukkede handler og MIN_HISTORY_H timers historik, højst MAX_DD % fald over 3 dage,
    botten svarer og scanner, markedet er åbent, og strategien har bevæget sig inden for FLAT_H timer.
  * Point = gennemsnittet af de tre afkast minus halvdelen af det største fald. De bedste købes først.
  * Hver plads får 1/SLOTS af porteføljen, når den købes. Højst MAX_PER_BOT pladser fra samme bot.
  * En plads sælges, når strategien holder op med at vokse (i minus over 24 timer), står stille i FLAT_H timer,
    falder STOP_PCT % fra sin top, eller dens bot ikke har svaret i DEAD_H timer. Er den kun faldet ud af top EXIT_RANK,
    sælges den først, når en bedre venter, og tidligst MIN_HOLD_H timer efter køb (en buffer mod støj).
  * Kun den ene plads skiftes; de andre bliver. Et køb og et salg koster botens realistiske pris pr. side.
"""
from __future__ import annotations

import time
from pathlib import Path

from .smart import DEAD_H, FLAT_H, MAX_POINTS, MIN_HISTORY_H, MIN_TRADES, STALE_S, WARMUP_S, SmartBot, _at

VERSION = 1
SLOTS = 5
MAX_PER_BOT = 2
ENTER_RANK = 5              # skal være blandt de 5 bedste for at blive købt
EXIT_RANK = 8               # sælges først, når den er uden for top 8, og en bedre venter
WINDOWS_H = (6, 24, 72)
MAX_DD = 10.0
STOP_PCT = 5.0
MIN_HOLD_H = 4
COOLDOWN_H = 24

RULES = {"slots": SLOTS, "max_per_bot": MAX_PER_BOT, "enter_rank": ENTER_RANK, "exit_rank": EXIT_RANK, "windows_h": list(WINDOWS_H),
         "max_dd": MAX_DD, "stop_pct": STOP_PCT, "min_hold_h": MIN_HOLD_H, "min_trades": MIN_TRADES, "min_history_h": MIN_HISTORY_H,
         "flat_h": FLAT_H, "dead_h": DEAD_H, "cooldown_h": COOLDOWN_H}


def _ret(cv: list, now: float, hours: float) -> float | None:
    base = _at(cv, now - hours * 3600)
    if base is None:                          # kortere historik end vinduet: brug hele historikken
        base = cv[0][1]
    return (100 + cv[-1][1]) / (100 + base) * 100 - 100 if 100 + base > 0 else None


def _dd(cv: list, now: float, hours: float) -> float:
    start = now - hours * 3600
    base = _at(cv, start)
    eq = ([100 + base] if base is not None else []) + [100 + p[1] for p in cv if p[0] >= start]
    peak, dd = (eq[0] if eq else 100), 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak * 100)
    return dd


def growth(c: dict, now: float) -> dict | None:
    """Afkast over de tre vinduer, største fald og point. None, hvis kurven ikke kan bruges."""
    cv = c.get("curve") or []
    if len(cv) < 2:
        return None
    rs = [_ret(cv, now, h) for h in WINDOWS_H]
    if any(r is None for r in rs):
        return None
    dd = _dd(cv, now, WINDOWS_H[-1])
    return {"r": rs, "dd": dd, "score": sum(rs) / len(rs) - 0.5 * dd}


class SmartPortfolio(SmartBot):
    def _new(self, capital: float = 10000.0, currency: str = "kr.") -> dict:
        t = round(self.now(), 1)
        return {"version": VERSION, "kind": "portfolio", "since": t, "capital": capital, "currency": currency, "cash": 1.0,
                "slots": [], "changes": [], "curve": [[round(t), 0.0]], "cooldown": {}, "updated": t, "costs_paid": 0.0}

    def _load(self) -> dict:
        import json
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("kind") == "portfolio" and d.get("version") == VERSION:
                return d
        except (OSError, ValueError):
            pass
        return self._new()

    def _value(self) -> float:
        return self.state["cash"] + sum(s["value"] for s in self.state["slots"])

    def _pay(self, slot: dict, cost_pct: float) -> None:
        if cost_pct > 0:
            paid = slot["value"] * cost_pct / 100
            slot["value"] -= paid
            self.state["costs_paid"] = self.state.get("costs_paid", 0.0) + paid * self.state["capital"]

    def _qualified(self, now: float) -> dict[str, dict]:
        out = {}
        for cid, (c, s) in self._all().items():
            if now - s["at"] > STALE_S or not s["fresh"] or self.state["cooldown"].get(cid, 0) > now:
                continue
            cv = c.get("curve") or []
            if (c.get("trades") or 0) < MIN_TRADES or len(cv) < 2 or now - cv[0][0] < MIN_HISTORY_H * 3600 or self._flat(c, now):
                continue
            g = growth(c, now)
            if g and all(r > 0 for r in g["r"]) and g["dd"] <= MAX_DD:
                out[cid] = g
        return out

    def step(self) -> None:
        with self.lock:
            now, st, allc = self.now(), self.state, self._all()
            if not allc:
                return
            for sl in st["slots"]:                                   # hver plads følger sin strategi siden sidst
                if sl["id"] not in allc:
                    continue
                c, s = allc[sl["id"]]
                if s["at"] <= sl.get("seen", 0):
                    continue
                v, tr = c["raw_now"], c.get("trades") or 0
                if tr >= sl["trades"] and 100 + sl["v"] > 0:
                    sl["value"] = max(0.0, sl["value"] * (100 + v) / (100 + sl["v"]))
                sl.update(v=v, trades=tr, seen=s["at"])
                sl["peak"] = max(sl.get("peak", sl["value"]), sl["value"])
            q = self._qualified(now)
            if len(self.snap) >= self.expected or now - self.started >= WARMUP_S:
                self._decide(now, q, allc)
            for sl in st["slots"]:
                g = growth(allc[sl["id"]][0], now) if sl["id"] in allc else None
                sl["score"], sl["r"] = (g["score"], g["r"]) if g else (None, None)
            st["qualified"], st["count"], st["bots"] = len(q), len(allc), len({c["bot"] for c, _ in allc.values()})
            st["avg_pct"] = self._average(now, allc)
            pct = round((self._value() - 1) * 100, 4)
            if not st["curve"] or st["curve"][-1][1] != pct or now - st["curve"][-1][0] >= 600:
                st["curve"].append([round(now), pct])
                if len(st["curve"]) > MAX_POINTS:
                    st["curve"] = st["curve"][:1] + st["curve"][1:-1:2] + st["curve"][-1:]
            st["updated"] = round(now, 1)
            self._save()

    def _average(self, now: float, allc: dict) -> float | None:
        """Til sammenligning: gennemsnittet af alle strategier i samme periode (som at sprede pengene på det hele)."""
        rs = []
        for c, _ in allc.values():
            cv = c.get("curve") or []
            base = _at(cv, self.state["since"])
            if base is not None and 100 + base > 0:
                rs.append((100 + cv[-1][1]) / (100 + base) * 100 - 100)
        return round(sum(rs) / len(rs), 3) if rs else None

    def _decide(self, now: float, q: dict, allc: dict) -> None:
        st = self.state
        ranked = sorted(q, key=lambda k: q[k]["score"], reverse=True)
        rank = {k: i + 1 for i, k in enumerate(ranked)}
        held = {sl["id"] for sl in st["slots"]}
        waiting = [k for k in ranked[:ENTER_RANK] if k not in held]
        for sl in list(st["slots"]):                                 # sælg de pladser, der ikke vokser længere
            c = allc.get(sl["id"], (None, None))[0]
            seen = allc[sl["id"]][1]["at"] if sl["id"] in allc else sl.get("seen", 0)
            g = growth(c, now) if c else None
            why = None
            if now - max(seen, sl.get("seen", 0)) > DEAD_H * 3600:
                why = f"{sl['bot_name']} har ikke svaret i {DEAD_H} timer"
            elif sl["value"] < sl.get("peak", sl["value"]) * (1 - STOP_PCT / 100):
                why = f"faldt over {STOP_PCT:g} % fra toppen"
            elif c is not None and self._flat(c, now):
                why = f"har stået stille i {FLAT_H} timer"
            elif g is not None and g["r"][1] <= 0:
                why = "er ikke længere i plus over 24 timer"
            elif now - sl["entered"] >= MIN_HOLD_H * 3600 and rank.get(sl["id"], 999) > EXIT_RANK and waiting:
                why = f"faldt ud af top {EXIT_RANK}, og en bedre venter"
            if why:
                self._sell(now, sl, allc, why, stop=why.startswith("faldt over") or "svaret" in why)
        per_bot: dict[str, int] = {}
        for sl in st["slots"]:
            per_bot[sl["bot"]] = per_bot.get(sl["bot"], 0) + 1
        held = {sl["id"] for sl in st["slots"]}
        for k in ranked[:ENTER_RANK]:                                # køb de bedste, der ikke er købt, så længe der er plads
            if len(st["slots"]) >= SLOTS:
                break
            c, s = allc[k]
            if k in held or per_bot.get(c["bot"], 0) >= MAX_PER_BOT or q[k]["score"] <= float(c.get("entry_cost") or 0) * 2:
                continue                                             # point skal mindst kunne betale ind- og udgang
            self._buy(now, k, allc, q[k])
            per_bot[c["bot"]] = per_bot.get(c["bot"], 0) + 1
            held.add(k)

    def _buy(self, now: float, cid: str, allc: dict, g: dict) -> None:
        st = self.state
        c, s = allc[cid]
        amount = min(st["cash"], self._value() / SLOTS)
        if amount <= 1e-6:
            return
        st["cash"] -= amount
        sl = {"id": cid, "name": c["name"], "bot": c["bot"], "bot_name": c["bot_name"], "v": c["raw_now"], "trades": c.get("trades") or 0,
              "seen": s["at"], "entered": round(now), "value": amount, "cost_in": amount}
        self._pay(sl, float(c.get("entry_cost") or 0))
        sl["peak"] = sl["value"]
        st["slots"].append(sl)
        self._change(now, "KØB", sl, f"vokser: {g['r'][0]:+.1f} % (6 t), {g['r'][1]:+.1f} % (24 t), {g['r'][2]:+.1f} % (3 d)")

    def _sell(self, now: float, sl: dict, allc: dict, why: str, stop: bool = False) -> None:
        st = self.state
        if sl["id"] in allc:
            self._pay(sl, float(allc[sl["id"]][0].get("entry_cost") or 0))
        st["cash"] += sl["value"]
        st["slots"].remove(sl)
        if stop:
            st["cooldown"][sl["id"]] = now + COOLDOWN_H * 3600
        self._change(now, "SÆLG", sl, why)

    def _change(self, now: float, side: str, sl: dict, why: str) -> None:
        res = (sl["value"] / sl["cost_in"] - 1) * 100 if side == "SÆLG" and sl.get("cost_in") else None
        self.state["changes"] = (self.state["changes"] + [{"ts": round(now), "side": side, "name": sl["name"], "bot_name": sl["bot_name"],
                                                           "why": why, "result_pct": res and round(res, 2)}])[-300:]
        self.log(f"Smart-portefølje: {side} {sl['name']} ({sl['bot_name']}): {why}")

    def weights(self) -> list[tuple[dict, float]]:
        """(plads, andel af porteføljen) – bruges af live-kontoen."""
        with self.lock:
            tot = self._value()
            return [(dict(sl), sl["value"] / tot if tot > 0 else 0.0) for sl in self.state["slots"]]

    def view(self) -> dict:
        with self.lock:
            st, now = self.state, self.now()
            tot = self._value()
            curve = st["curve"]
            if len(curve) > 200:
                k = len(curve) / 200
                curve = [curve[int(i * k)] for i in range(200)] + [curve[-1]]
            warm = len(self.snap) < self.expected and now - self.started < WARMUP_S
            return {"return_pct": round((tot - 1) * 100, 2), "capital": st["capital"], "currency": st["currency"],
                    "value": round(st["capital"] * tot, 2), "cash": round(st["capital"] * st["cash"], 2),
                    "invested_pct": round((1 - st["cash"] / tot) * 100, 1) if tot > 0 else 0.0,
                    "costs_paid": round(st.get("costs_paid", 0.0), 2), "since": round(st["since"]), "updated": st.get("updated"),
                    "qualified": st.get("qualified", 0), "count": st.get("count", 0), "bots": st.get("bots", 0),
                    "avg_pct": st.get("avg_pct"), "rules": RULES,
                    "warning": f"Starter op: {len(self.snap)} af {self.expected} botter har svaret – køber, når alle er med" if warm else None,
                    "slots": [{"name": sl["name"], "bot_name": sl["bot_name"], "bot": sl["bot"], "entered": sl["entered"],
                               "weight_pct": round(sl["value"] / tot * 100, 1) if tot > 0 else 0.0,
                               "result_pct": round((sl["value"] / sl["cost_in"] - 1) * 100, 2) if sl.get("cost_in") else 0.0,
                               "score": sl.get("score") and round(sl["score"], 2), "r": sl.get("r") and [round(x, 2) for x in sl["r"]]}
                              for sl in st["slots"]],
                    "changes": st["changes"][-10:][::-1], "trades": sum(1 for c in st["changes"] if c["side"] == "SÆLG"),
                    "curve": [[t, round(v, 3)] for t, v in curve]}
