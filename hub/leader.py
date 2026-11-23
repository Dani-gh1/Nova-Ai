"""Smart strategi: følger altid den strategi, der er steget mest, siden Smart startede, og skifter, når en anden fører med
mere, end skiftet koster i gebyr og prisforskel (se SIDE_COST).

Eksempel med to strategier, der begge starter på 0 %:
  * Strategi 1 stiger til +2 %  -> Smart følger den og står på +2 %.
  * Strategi 2 stiger til +1,5 % -> ingen betydning; strategi 1 fører stadig. Smart står på +2 %.
  * Strategi 1 falder mod +1 %  -> Smart falder med, og i det øjeblik strategi 1 kommer under strategi 2 (ved +1,5 %),
    skifter Smart til strategi 2. Smart står derfor på +1,5 % og følger nu strategi 2.

Som en rigtig konto: Smart vokser og falder med den strategi, den følger (renters rente), og skifter koster intet på papir.
Hver strategis stigning måles fra den stand, den havde, første gang Smart så den (for de fleste: da Smart startede).
Kører hele tiden i hubben og gemmer sin konto i logs/smart-leader.json; historikken regnes aldrig om.
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .smart import MAX_POINTS, WARMUP_S, SmartBot

VERSION = 2                              # 2: omkostninger ved skift trækkes fra (version 1 regnes om ved indlæsning)
EPS = 1e-9
WINDOW_S = 24 * 3600                     # Nova Bot vælger den strategi, der er steget mest de seneste 24 timer (rullende)

# Omkostning pr. side ved et skift (andel af kontoen): gebyr + prisforskel (spread/slip/prisvirkning), forsigtigt skønnet.
# Botternes egne tal har allerede deres gebyrer med. Det her er det, der kommer oveni, når Nova Bot sælger alt i én
# strategi og køber ind i en anden. Et skift koster derfor salg i den gamle + køb i den nye.
SIDE_COST = {
    "atlas": .0015, "atlasglobal": .002, "bondai": .001, "metalai": .0015, "energiai": .0015, "temaai": .0015,
    "smallcapai": .003, "udbytteai": .0015, "danskai": .002, "regnskabai": .0015,       # aktier: kurtage + slip + halv spread
    "meridian": .0003, "valutaai": .0003,                                                 # forex: halv spread
    "orion": .0015, "orionmid": .0025, "stakingai": .0015, "defiai": .0025, "aitokenai": .0025,   # Binance spot: 0,10 % + slip
    "quasar": .003,                                     # futures: 0,05 % af den gearede størrelse (op til 5x) + slip
    "delphi": .015,                                     # prognosemarkeder: køb til udbud, salg til bud (bred spread)
    "comet": .013,                                      # nye tokens: 0,3 % DEX-gebyr + gas + prisvirkning i små puljer
    "nova": .015, "novaclassic": .012,                  # memecoins: puljegebyr + prisvirkning + slip + netværksgebyr
}
DEFAULT_SIDE_COST = .005                 # ukendt marked: hellere for meget end for lidt


def side_cost(bot_key: str | None, table: dict | None = None) -> float:
    t = SIDE_COST if table is None else table
    return t.get(bot_key, DEFAULT_SIDE_COST if table is None else 0.0)
TZ = ZoneInfo("Europe/Copenhagen")       # en ny dag starter ved midnat dansk tid


class SmartLeader(SmartBot):
    costs: dict | None = None              # None: SIDE_COST. Tests kan give {} (ingen omkostninger)

    def _cost(self, bot_key) -> float:
        return side_cost(bot_key, self.costs)

    def _new(self) -> dict:
        t = round(self.now(), 1)
        return {"version": VERSION, "kind": "leader", "since": t, "capital": 10000.0, "currency": "kr.", "acct": 1.0,
                "held": None, "bases": {}, "switches": [], "curve": [[round(t), 0.0]], "updated": t,
                "day": self._day(t), "day_acct": 1.0, "days": {}}

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("kind") == "leader" and d.get("version") == 1:
                d = self._migrate(d)
            if isinstance(d, dict) and d.get("kind") == "leader" and d.get("version") == VERSION:
                return d
        except (OSError, ValueError):
            pass
        return self._new()

    def _migrate(self, d: dict) -> dict:
        """Version 1 talte skift som gratis. Regn hele historikken om med omkostningerne: kurve, dage, konto og hvert skift."""
        sw = sorted(d.get("switches") or [], key=lambda x: x["ts"])
        steps = []                                           # (tid, faktor) for hvert skift
        for x in sw:
            f = (1 - self._cost(x.get("from_key")) if x.get("from") else 1.0) * (1 - self._cost(x.get("to_key")))
            x["cost_pct"] = round((1 - f) * 100, 4)
            steps.append((x["ts"], f))

        def factor(t):                                       # samlet omkostning til og med tid t
            out = 1.0
            for ts, f in steps:
                if ts <= t:
                    out *= f
            return out
        for p in d.get("curve") or []:
            p[1] = round(((1 + p[1] / 100) * factor(p[0]) - 1) * 100, 4)
        for x in sw:
            if x.get("smart_pct") is not None:
                x["smart_pct"] = round(((1 + x["smart_pct"] / 100) * factor(x["ts"]) - 1) * 100, 4)
        for day, v in list((d.get("days") or {}).items()):  # dagens skift trækkes fra dagens resultat
            f = 1.0
            for x, (ts, g) in zip(sw, steps):
                if self._day(ts) == day:
                    f *= g
            d["days"][day] = round(((1 + v / 100) * f - 1) * 100, 4)
        today = 1.0
        for x, (ts, g) in zip(sw, steps):
            if self._day(ts) == d.get("day"):
                today *= g
        total = factor(float("inf"))
        d["acct"] = d.get("acct", 1.0) * total
        d["day_acct"] = d.get("day_acct", 1.0) * total / today
        if d.get("held") and d["held"].get("entry_acct"):
            # skiftet ind i den nuværende: salget af den gamle tæller før indgangen, købet af den nye efter
            d["held"]["entry_acct"] *= factor(d["held"].get("entered", 0)) / (1 - self._cost(d["held"].get("bot")))
        d["costs_pct"] = round((1 - total) * 100, 4)
        d["version"] = VERSION
        self.log(f"Nova Bot: historikken er regnet om med omkostninger ved skift ({len(sw)} skift, i alt −{d['costs_pct']:.2f} % af kontoen)")
        return d

    @staticmethod
    def _day(t: float) -> str:
        return datetime.fromtimestamp(t, TZ).strftime("%Y-%m-%d")

    def _new_day(self, now: float) -> None:
        """Ved midnat: gem dagens resultat, og lad alle strategier starte fra 0 igen (Smart vælger forfra)."""
        st, day = self.state, self._day(now)
        if st.get("day") == day:
            return
        if st.get("day"):
            st.setdefault("days", {})[st["day"]] = round((st["acct"] / st.get("day_acct", 1.0) - 1) * 100, 4)
            self.log(f"Nova Bot: {st['day']} sluttede på {st['days'][st['day']]:+.2f} %")
        st["day"], st["day_acct"] = day, st["acct"]          # kun dagsresultatet nulstilles; valget går på de seneste 24 timer

    @staticmethod
    def _value_at(curve, t: float, default: float) -> float:
        """Kurvens værdi på tidspunktet t (sidste punkt til og med t, ellers det første punkt)."""
        pts = sorted((p for p in curve or [] if isinstance(p, (list, tuple)) and len(p) >= 2 and isinstance(p[0], (int, float))), key=lambda p: p[0])
        if not pts:
            return default
        v = pts[0][1]
        for p in pts:
            if p[0] > t:
                break
            v = p[1]
        return v

    def _gains(self, allc: dict, now: float) -> dict[str, float]:
        """Hver strategis stigning i procent de seneste 24 timer. Er Smart først begyndt at se strategien senere, måles fra da."""
        bases, out = self.state["bases"], {}
        for cid, (c, _s) in allc.items():
            v, tr = c["raw_now"], c.get("trades") or 0
            b = bases.get(cid)
            if b is None or tr < b["trades"]:                  # ny strategi, eller den er startet forfra: mål fra nu
                b = bases[cid] = {"v": v, "trades": tr, "t": now}
            b["trades"] = max(b["trades"], tr)
            base = b["v"]
            if b.get("t", 0) < now - WINDOW_S:                 # set for over 24 timer siden: mål fra værdien for præcis 24 timer siden
                base = self._value_at(c.get("curve"), now - WINDOW_S, b["v"])
            if 100 + base > 0:
                out[cid] = ((100 + v) / (100 + base) - 1) * 100
        return out

    def step(self) -> None:
        with self.lock:
            now, st, allc = self.now(), self.state, self._all()
            if not allc:
                return
            h = st["held"]
            if h and h["id"] in allc:                                       # følg strategiens bevægelse siden sidst
                c, s = allc[h["id"]]
                if s["at"] > h.get("seen", 0):
                    v = c["raw_now"]
                    if (c.get("trades") or 0) >= h["trades"] and 100 + h["v"] > 0:
                        st["acct"] = max(0.0, st["acct"] * (100 + v) / (100 + h["v"]))
                    h.update(v=v, trades=c.get("trades") or 0, seen=s["at"])
            self._new_day(now)
            h = st["held"]
            gains = self._gains(allc, now)
            if gains and (len(self.snap) >= self.expected or now - self.started >= WARMUP_S):
                lead = max(gains, key=gains.get)
                held_gain = gains.get(h["id"]) if h else None
                # Skift kun, når føringen er større end det, skiftet koster (salg af den gamle + køb af den nye)
                c_lead = allc[lead][0]["bot"]
                need = (1 - (1 - self._cost(h["bot"]) if h else 1.0) * (1 - self._cost(c_lead))) * 100
                if gains[lead] > need + EPS and (h is None or (lead != h["id"] and (held_gain is None or gains[lead] > held_gain + need + EPS))):
                    self._switch(now, lead, allc, gains)
            st["gains"] = {k: round(v, 4) for k, v in gains.items()}
            st["count"], st["bots"] = len(allc), len({c["bot"] for c, _ in allc.values()})
            pct = round((st["acct"] - 1) * 100, 4)
            if not st["curve"] or st["curve"][-1][1] != pct or now - st["curve"][-1][0] >= 600:
                st["curve"].append([round(now), pct])
                if len(st["curve"]) > MAX_POINTS:
                    st["curve"] = st["curve"][:1] + st["curve"][1:-1:2] + st["curve"][-1:]
            st["updated"] = round(now, 1)
            self._save()

    def _switch(self, now: float, to: str, allc: dict, gains: dict) -> None:
        st, h = self.state, self.state["held"]
        c, s = allc[to]
        entry = st["acct"]                 # før købet: købsomkostningen hører til den nye strategis resultat
        if h is None:                      # første valg: alle stod lige fra start, så Smart har været med i den førende hele vejen
            st["acct"] *= 1 + gains[to] / 100
        before = st["acct"]
        if h is not None:                  # sælg alt i den gamle strategi: gebyr + prisforskel
            st["acct"] *= 1 - self._cost(h["bot"])
            entry = st["acct"]
        result = (st["acct"] / h["entry_acct"] - 1) * 100 if h and h.get("entry_acct") else None    # inkl. køb og salg
        st["acct"] *= 1 - self._cost(c["bot"])
        cost = (1 - st["acct"] / before) * 100
        st["costs_pct"] = round((1 - (1 - st.get("costs_pct", 0.0) / 100) * (1 - cost / 100)) * 100, 4)
        st["held"] = {"id": to, "name": c["name"], "bot": c["bot"], "bot_name": c["bot_name"], "v": c["raw_now"],
                      "trades": c.get("trades") or 0, "seen": s["at"], "entered": round(now), "entry_acct": entry}
        why = f"overhalede {h['name']} ({gains[to]:+.2f} % mod {gains.get(h['id'], 0.0):+.2f} %)" if h else f"steg først ({gains[to]:+.2f} %)"
        st["switches"] = (st["switches"] + [{"ts": round(now), "from": h and h["name"], "from_bot": h and h["bot_name"], "from_key": h and h["bot"], "to": c["name"],
                                             "to_bot": c["bot_name"], "to_key": c["bot"], "why": why, "result_pct": None if result is None else round(result, 2), "cost_pct": round(cost, 4), "smart_pct": round((st["acct"] - 1) * 100, 4)}])[-500:]
        self.log(f"Nova Bot: {(h and h['name']) or 'start'} → {c['name']} ({c['bot_name']}): {why}")
        cb = getattr(self, "on_switch", None)
        if cb:
            res = "" if result is None else f" --{result:+.2f}%-->"
            cb(f"{(h and h['name']) or 'Start'}{res or ' →'} {c['name']} ({c['bot_name']})")

    def weights(self) -> list[tuple[dict, float]]:
        """(strategi, andel af kontoen) – bruges af Kassen: hele kontoen følger den ene strategi."""
        with self.lock:
            h = self.state["held"]
            return [(dict(h), 1.0)] if h else []

    def view(self) -> dict:
        with self.lock:
            st, now, h = self.state, self.now(), self.state["held"]
            names = {cid: (c["name"], c["bot_name"]) for cid, (c, _s) in self._all().items()}
            gains = st.get("gains", {})
            top = sorted(gains, key=gains.get, reverse=True)[:5]
            curve = st["curve"]
            if len(curve) > 200:
                k = len(curve) / 200
                curve = [curve[int(i * k)] for i in range(200)] + [curve[-1]]
            warm = len(self.snap) < self.expected and now - self.started < WARMUP_S
            ret = (st["acct"] - 1) * 100
            days = dict(st.get("days", {}))
            today = (st["acct"] / st.get("day_acct", 1.0) - 1) * 100
            days[st.get("day") or self._day(now)] = round(today, 4)
            return {"return_pct": round(ret, 2), "today_pct": round(today, 2), "days": days, "capital": st["capital"], "currency": st["currency"],
                    "value": round(st["capital"] * st["acct"], 2), "since": round(st["since"]), "updated": st.get("updated"),
                    "following": h and h["name"], "following_bot": h and h["bot_name"], "held_since": h and h["entered"],
                    "following_gain": round(gains[h["id"]], 2) if h and h["id"] in gains else None,
                    "leaders": [{"name": names.get(k, (k, ""))[0], "bot_name": names.get(k, ("", ""))[1], "gain": round(gains[k], 2),
                                 "held": bool(h and k == h["id"])} for k in top],
                    "count": st.get("count", 0), "bots": st.get("bots", 0), "switches": len(st["switches"]),
                    "costs_pct": round(st.get("costs_pct", 0.0), 2),     # omkostninger ved skift i alt (allerede trukket fra)
                    "recent": st["switches"][-8:][::-1],
                    "warning": f"Starter op: {len(self.snap)} af {self.expected} botter har svaret – vælger, når alle er med" if warm else None,
                    "curve": [[t, round(v, 3)] for t, v in curve]}
