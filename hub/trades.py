"""Alle handler med købspris, salgspris og ALLE omkostninger, læst direkte fra botternes egne databaser (kun læsning).

Hver handel får en liste over omkostninger: [etiket, USD (eller None), % af positionen (eller None), art], hvor art er
  "registreret"  botten har selv gemt beløbet (eller det kan regnes præcist ud af det, den har gemt)
  "sats"         bottens faste sats, som den bruger på hver handel (fx Binance 0,10 % pr. side)
  "indregnet"    omkostningen ligger allerede i købs-/salgsprisen (fx slip fra ordrebogen, køb til udbudspris)
Papirhandler: botternes egne tal. Rigtige handler: Kassens ordrer med Binances faktiske gebyr og prisforskel.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

ORION = {"orion": "Orion-AI/desk.sqlite3", "orionmid": "Orion-AI/desk-mid.sqlite3", "defiai": "Orion-AI/desk-defiai.sqlite3",
         "aitokenai": "Orion-AI/desk-aitokenai.sqlite3", "stakingai": "Orion-AI/desk-stakingai.sqlite3"}
ATLAS = {"atlas": "Atlas/data/desk.sqlite", "atlasglobal": "Atlas/data-global/desk.sqlite",
         **{k: f"Atlas/data-{k}/desk.sqlite" for k in ("bondai", "metalai", "energiai", "temaai", "smallcapai", "udbytteai", "danskai", "regnskabai")}}
MERIDIAN = {"meridian": "Meridian-AI/var/desk.sqlite3", "valutaai": "Meridian-AI/var-valuta/desk.sqlite3"}
COINS = {"nova": "Coins/runs/desk.db", "novaclassic": "Coins/runs-classic/desk.db"}
ORION_FEE_PCT = 0.10            # Orion-botternes gebyr pr. side (config.FEE_PCT)
DELPHI_BANKROLL = 1000.0        # Delphi gemmer beløb i % af en nominel konto på 1.000 USD (config.EXEC_NOMINAL_BANKROLL_USD)


def _ts(v) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _rows(path: Path, sql: str) -> list[sqlite3.Row]:
    if not path.exists():
        return []
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def _cost(label, usd=None, pct=None, kind="registreret") -> list:
    return [label, None if usd is None else round(usd, 6), None if pct is None else round(pct, 4), kind]


def _trade(bot, strategy, symbol, side, opened, closed, entry, exit_, size_usd, pnl_pct, pnl_usd, costs, note="") -> dict:
    usd = [c[1] for c in costs if c[1] is not None]
    pct = [c[2] for c in costs if c[2] is not None]
    cost_usd = round(sum(usd), 4) if usd else None
    cost_pct = round(sum(pct), 4) if pct else (round(cost_usd / size_usd * 100, 4) if cost_usd is not None and size_usd else None)
    return {"bot": bot, "strategy": str(strategy), "symbol": symbol, "side": side, "opened": _ts(opened), "closed": _ts(closed),
            "entry": entry, "exit": exit_, "size_usd": None if size_usd is None else round(size_usd, 2),
            "pnl_pct": None if pnl_pct is None else round(pnl_pct, 4), "pnl_usd": None if pnl_usd is None else round(pnl_usd, 4),
            "costs": costs, "cost_usd": cost_usd, "cost_pct": cost_pct, "note": note}


# ---------------------------------------------------------------- pr. bot-type

def orion(key: str, path: Path) -> list[dict]:
    out = []
    for r in _rows(path, "SELECT * FROM trades WHERE shadow=0 AND epoch=(SELECT MAX(epoch) FROM trades)"):
        closed = r["ts_close"]
        out.append(_trade(key, r["strategy"], r["symbol"], "long", r["ts_open"], closed, r["entry_price"], r["exit_price"], None,
                          r["pnl_pct"], None,
                          [_cost("Binance-gebyr ved køb (0,10 %)", pct=ORION_FEE_PCT, kind="sats")]
                          + ([_cost("Binance-gebyr ved salg (0,10 %)", pct=ORION_FEE_PCT, kind="sats")] if closed else [])
                          + [_cost("Slip fra ordrebogen", kind="indregnet")]))
    return out


def comet(key: str, path: Path) -> list[dict]:
    out = []
    for r in _rows(path, "SELECT * FROM positions WHERE shadow=0 AND epoch=(SELECT MAX(epoch) FROM positions)"):
        size, tokens = r["size_usd"] or 0.0, r["tokens"] or 0.0
        costs = [_cost("Gas ved køb", usd=max(0.0, (r["entry_cost_usd"] or size) - size))]
        if tokens and r["entry_price"]:
            costs.append(_cost(f"DEX-gebyr og prisvirkning ved køb ({(r['entry_impact'] or 0) * 100:.2f} % prisvirkning)".replace(".", ","),
                               usd=max(0.0, size - tokens * r["entry_price"])))
        if r["status"] == "closed" and r["proceeds_usd"] is not None and r["exit_price"] and tokens:
            sale = max(0.0, tokens * r["exit_price"] - r["proceeds_usd"])
            big = sale > 0.10 * (r["entry_cost_usd"] or size or 1)         # over 10 %: puljen var (næsten) tom, fx rug pull
            costs.append(_cost("Salg langt under kursen (tom pulje eller rug pull)" if big else "DEX-gebyr, gas og prisvirkning ved salg", usd=sale))
        for c in costs:
            c[2] = round(c[1] / (r["entry_cost_usd"] or size) * 100, 4) if c[1] is not None and (r["entry_cost_usd"] or size) else None
        out.append(_trade(key, r["strategy_key"], f"{r['symbol']} ({r['chain']})", "long", r["opened_at"],
                          r["closed_at"] if r["status"] == "closed" else None, r["entry_price"], r["exit_price"], r["entry_cost_usd"] or size,
                          r["pnl_pct"], r["pnl_usd"], costs))
    return out


def delphi(key: str, path: Path) -> list[dict]:
    out = []
    for r in _rows(path, "SELECT * FROM trades WHERE epoch=(SELECT MAX(epoch) FROM trades)"):
        cost = r["cost"] or 0.0                                   # % af den nominelle konto
        usd = lambda p: p * DELPHI_BANKROLL / 100
        costs = [_cost("Platformens gebyr ved køb", usd=usd(r["fee"] or 0.0)), _cost("Slip ved køb", usd=usd(r["slippage"] or 0.0)),
                 _cost("Køb til udbudspris (spread)", kind="indregnet")]
        if r["status"] != "open" and r["proceeds"] is not None and r["exit_price"] is not None and r["contracts"]:
            costs.append(_cost("Gebyr og spread ved salg", usd=max(0.0, r["contracts"] * r["exit_price"] - usd(r["proceeds"]))))
        size = usd(cost)
        for c in costs:
            c[2] = round(c[1] / size * 100, 4) if c[1] is not None and size else None
        out.append(_trade(key, r["strategy"], f"{r['title']} · {r['side']}", r["side"], r["opened_ts"],
                          r["closed_ts"] if r["status"] != "open" else None, r["entry_price"], r["exit_price"], size,
                          (r["pnl"] / cost * 100) if r["pnl"] is not None and cost else None,
                          usd(r["pnl"]) if r["pnl"] is not None else None, costs))
    return out


def atlas(key: str, path: Path) -> list[dict]:
    """Aktier: køb og salg er hver sin række. De parres pr. strategi og aktie."""
    out, open_ = [], {}
    for r in _rows(path, "SELECT * FROM trades WHERE epoch=(SELECT MAX(epoch) FROM trades) ORDER BY id"):
        k = (r["strategy"], r["symbol"])
        fee, slip = r["commission"] or 0.0, (r["slippage"] or 0.0) * (r["qty"] or 0.0)
        if r["side"] == "buy":
            open_[k] = {"r": r, "costs": [_cost("Kurtage ved køb", usd=fee), _cost("Slip og halv spread ved køb", usd=slip)]}
            continue
        b = open_.pop(k, None)
        if b is None:
            continue
        size = (b["r"]["qty"] or 0) * (b["r"]["price"] or 0)
        costs = b["costs"] + [_cost("Kurtage ved salg", usd=fee), _cost("Slip og halv spread ved salg", usd=slip)]
        for c in costs:
            c[2] = round(c[1] / size * 100, 4) if size else None
        out.append(_trade(key, r["strategy"], r["symbol"], "long", b["r"]["ts"], r["ts"], b["r"]["price"], r["price"], size, r["pnl_pct"],
                          size * r["pnl_pct"] / 100 if r["pnl_pct"] is not None else None, costs))
    for b in open_.values():
        r = b["r"]
        size = (r["qty"] or 0) * (r["price"] or 0)
        for c in b["costs"]:
            c[2] = round(c[1] / size * 100, 4) if size else None
        out.append(_trade(key, r["strategy"], r["symbol"], "long", r["ts"], None, r["price"], None, size, None, None, b["costs"]))
    return out


def meridian(key: str, path: Path) -> list[dict]:
    out = []
    for r in _rows(path, "SELECT * FROM trades WHERE epoch=(SELECT MAX(epoch) FROM trades)"):
        costs = [_cost("Spread (køb og salg)", pct=r["spread_cost_pct"] or 0.0)]
        if r["swap_pct"]:
            costs.append(_cost("Renter for at holde natten over (swap)", pct=r["swap_pct"]))
        out.append(_trade(key, r["strategy"], r["instrument"], r["direction"], r["opened_at"], r["closed_at"], r["entry"], r["exit"], None,
                          r["pnl_pct"], None, costs))
    return out


def coins(key: str, path: Path) -> list[dict]:
    out = []
    rows = [(r, r["strategy"]) for r in _rows(path, "SELECT * FROM strategy_positions")] + [(r, "Hovedkonto") for r in _rows(path, "SELECT * FROM positions")]
    for r, strat in rows:
        keys = r.keys()
        cost, closed = r["cost_usd"] or 0.0, r["status"] == "closed"
        costs = []
        if r["qty"] and r["entry_price"]:                        # betalt minus værdien til købsprisen = netværksgebyret
            costs.append(_cost("Netværksgebyr ved køb", usd=max(0.0, cost - r["qty"] * r["entry_price"])))
        if "entry_impact_pct" in keys and r["entry_impact_pct"] is not None:
            costs.append(_cost("Prisvirkning og puljegebyr ved køb (i købsprisen)", usd=cost * r["entry_impact_pct"] / 100, kind="indregnet"))
        if closed and r["proceeds_usd"] is not None and r["exit_price"] and r["qty"]:
            costs.append(_cost("Puljegebyr, prisvirkning og netværksgebyr ved salg", usd=max(0.0, r["qty"] * r["exit_price"] - r["proceeds_usd"])))
        for c in costs:
            c[2] = round(c[1] / cost * 100, 4) if c[1] is not None and cost else None
        out.append(_trade(key, strat, r["symbol"], "long", r["opened_ts"], r["closed_ts"] if closed else None, r["entry_price"], r["exit_price"],
                          cost, (r["pnl_usd"] / cost * 100) if r["pnl_usd"] is not None and cost else None, r["pnl_usd"], costs))
    return out


SOURCES = {**{k: (orion, p) for k, p in ORION.items()}, **{k: (atlas, p) for k, p in ATLAS.items()},
           **{k: (meridian, p) for k, p in MERIDIAN.items()}, **{k: (coins, p) for k, p in COINS.items()},
           "comet": (comet, "Comet-AI/data/desk.sqlite3"), "delphi": (delphi, "Delphi-AI/data/desk.sqlite3")}


def real_trades(kasse) -> list[dict]:
    """Rigtige handler fra Kassen: Binances faktiske gebyr, og prisforskellen mod den pris, Kassen så lige før ordren."""
    out = []
    if kasse is None:
        return out
    for v in kasse.venues.values():
        for o in v.state.get("orders") or []:
            if o.get("sim"):
                continue
            fee, cost = o.get("fee_usd"), o.get("cost_usd")
            costs = [_cost("Binance-gebyr", usd=fee)] if fee is not None else []
            if cost is not None:
                costs.append(_cost("Prisforskel (slip)", usd=cost - (fee or 0.0) if fee is not None else cost))
            for c in costs:
                c[2] = round(c[1] / o["usd"] * 100, 4) if c[1] is not None and o.get("usd") else None
            eff = o["usd"] / o["qty"] if o.get("qty") else None
            t = _trade("rigtig", o.get("venue", ""), o["symbol"], "Køb" if o["side"] == "ÅBN" else "Salg", o["ts"], o["ts"],
                       eff if o["side"] == "ÅBN" else None, eff if o["side"] != "ÅBN" else None, o.get("usd"), None, None, costs)
            t.update(real=True, market_price=o.get("price"), qty=o.get("qty"), order_id=o.get("id"))
            out.append(t)
    return out


class TradeBook:
    """Læser alle handler højst hvert 60. sekund (databaserne er små, men der er mange)."""

    def __init__(self, bots_dir: Path, ttl: float = 60.0, now=time.time):
        self.dir, self.ttl, self.now = bots_dir, ttl, now
        self.lock = threading.Lock()
        self._cache: tuple[float, list] | None = None

    def all(self) -> list[dict]:
        with self.lock:
            if self._cache and self.now() - self._cache[0] < self.ttl:
                return self._cache[1]
            out = []
            for key, (fn, rel) in SOURCES.items():
                try:
                    out += fn(key, self.dir / rel)
                except (sqlite3.Error, OSError, KeyError, TypeError, ValueError, IndexError):
                    continue                                   # én bots database må aldrig vælte resten
            out.sort(key=lambda t: -((t["closed"] or t["opened"] or 0)))
            self._cache = (self.now(), out)
            return out

    def query(self, bot: str = "", strategy: str = "", status: str = "", limit: int = 100, offset: int = 0, names: dict | None = None) -> dict:
        rows = self.all()
        if bot:
            rows = [t for t in rows if t["bot"] == bot]
        if strategy:
            rows = [t for t in rows if t["strategy"] == strategy or (names or {}).get((t["bot"], t["strategy"])) == strategy]
        if status == "open":
            rows = [t for t in rows if t["closed"] is None]
        elif status == "closed":
            rows = [t for t in rows if t["closed"] is not None]
        page = rows[offset:offset + limit]
        if names:
            page = [{**t, "strategy_name": names.get((t["bot"], t["strategy"]), t["strategy"])} for t in page]
        costs = [t["cost_pct"] for t in rows if t["cost_pct"] is not None and t["closed"]]
        return {"total": len(rows), "offset": offset, "trades": page,
                "avg_cost_pct": round(sum(costs) / len(costs), 3) if costs else None}
