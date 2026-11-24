"""Smart strategi: følger altid den strategi, der er steget mest i dag (runden starter ved midnat dansk tid, hvor alle
strategier starter på 0 %), og skifter i samme øjeblik, en anden overhaler den.

Eksempel med to strategier, der begge starter på 0 %:
  * Strategi 1 stiger til +2 %  -> Smart følger den og står på +2 %.
  * Strategi 2 stiger til +1,5 % -> ingen betydning; strategi 1 fører stadig. Smart står på +2 %.
  * Strategi 1 falder mod +1 %  -> Smart falder med, og i det øjeblik strategi 1 kommer under strategi 2 (ved +1,5 %),
    skifter Smart til strategi 2. Smart står derfor på +1,5 % og følger nu strategi 2.

Som en rigtig konto: Smart vokser og falder med den strategi, den følger (renters rente), og skifter koster intet på papir.
Hver strategis stigning måles fra rundens start (midnat), eller fra første gang Smart så den, hvis det var senere.
Kører hele tiden i hubben og gemmer sin konto i logs/smart-leader.json; historikken regnes aldrig om.
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .smart import MAX_POINTS, WARMUP_S, SmartBot

VERSION = 1
EPS = 1e-9
TZ = ZoneInfo("Europe/Copenhagen")       # en ny dag starter ved midnat dansk tid


class SmartLeader(SmartBot):
    def _new(self) -> dict:
        t = round(self.now(), 1)
        return {"version": VERSION, "kind": "leader", "since": t, "capital": 10000.0, "currency": "kr.", "acct": 1.0,
                "held": None, "bases": {}, "switches": [], "curve": [[round(t), 0.0]], "updated": t,
                "day": self._day(t), "day_acct": 1.0, "days": {}}

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("kind") == "leader" and d.get("version") == VERSION:
                return d
        except (OSError, ValueError):
            pass
        return self._new()

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
        st["day"], st["day_acct"], st["bases"], st["held"] = day, st["acct"], {}, None

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

    def _round_start(self, now: float) -> float:
        """Rundens start: midnat dansk tid."""
        return datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def _gains(self, allc: dict, now: float) -> dict[str, float]:
        """Hver strategis stigning i procent siden rundens start (midnat). Er Smart først begyndt at se strategien senere, måles fra da."""
        start = self._round_start(now)
        bases, out = self.state["bases"], {}
        for cid, (c, _s) in allc.items():
            v, tr = c["raw_now"], c.get("trades") or 0
            b = bases.get(cid)
            if b is None or tr < b["trades"]:                  # ny strategi, eller den er startet forfra: mål fra nu
                b = bases[cid] = {"v": v, "trades": tr, "t": now}
            b["trades"] = max(b["trades"], tr)
            base = b["v"]
            if b.get("t", 0) < start:                          # gemt fra før rundens start (fx efter en opdatering): mål fra midnat
                base = self._value_at(c.get("curve"), start, b["v"])
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
                if gains[lead] > EPS and (h is None or (lead != h["id"] and (held_gain is None or gains[lead] > held_gain + EPS))):
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
        entry = st["acct"]
        if h is None:                      # første valg: alle stod lige fra start, så Smart har været med i den førende hele vejen
            st["acct"] *= 1 + gains[to] / 100
        result = (st["acct"] / h["entry_acct"] - 1) * 100 if h and h.get("entry_acct") else None
        st["held"] = {"id": to, "name": c["name"], "bot": c["bot"], "bot_name": c["bot_name"], "v": c["raw_now"],
                      "trades": c.get("trades") or 0, "seen": s["at"], "entered": round(now), "entry_acct": entry}
        why = f"overhalede {h['name']} ({gains[to]:+.2f} % mod {gains.get(h['id'], 0.0):+.2f} %)" if h else f"steg først ({gains[to]:+.2f} %)"
        st["switches"] = (st["switches"] + [{"ts": round(now), "from": h and h["name"], "from_bot": h and h["bot_name"], "from_key": h and h["bot"], "to": c["name"],
                                             "to_bot": c["bot_name"], "to_key": c["bot"], "why": why, "result_pct": None if result is None else round(result, 2), "smart_pct": round((st["acct"] - 1) * 100, 4)}])[-500:]
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
                    "recent": st["switches"][-8:][::-1],
                    "warning": f"Starter op: {len(self.snap)} af {self.expected} botter har svaret – vælger, når alle er med" if warm else None,
                    "curve": [[t, round(v, 3)] for t, v in curve]}
