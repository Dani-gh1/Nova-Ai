"""Rigtige penge: Kassen og markederne (børserne), styret af Smart strategi.

Kassen er stedet, pengene lægges ind. Smart strategi (samme regel, men kun strategier, der kan handles
for rigtigt) bestemmer, hvor stor en andel hver strategi skal have. Kassen fordeler pengene til de markeder, hvor
strategierne handles, flytter penge automatisk mellem kryptokontiene, og hvert marked ejer de samme positioner som
strategierne (lige store beløb pr. position).

Tilstande (HUB_LIVE i /etc/hub.env):
  tom/0   slået fra
  dry     simulering: rigtige kurser, ingen ordrer sendes. Kassen starter med HUB_LIVE_BUDGET_USD (standard 145 ≈ 1.000 kr.)
  1       RIGTIGE ORDRER. Penge lægges ind hos børsen og registreres i Kassen med:  python3 -m hub kasse indskud <kr>

Markeder i dag: Binance spot (Orion-botterne) og Binance futures (Quasar, gearing højst MAX_LEVERAGE). Flere kommer
(Saxo, Polymarket, Solana, Base) og sættes til i HUB_LIVE_VENUES.

Sikkerhed:
  * Bruger kun de penge, der er registreret i Kassen, og rører kun positioner, den selv har åbnet.
  * API-nøglen behøver ikke ret til at hæve penge: flytning mellem spot og futures er en intern overførsel hos Binance.
  * Den stopper aldrig af sig selv. Penge tages ud med  python3 -m hub kasse udbetal <kr>  (så sælges der først).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_POSITIONS = 8            # højst så mange positioner pr. marked (små beløb: hver ordre skal over børsens minimum)
MIN_ORDER_USD = 6.0          # Binance kræver mindst 5 USDT pr. ordre (futures: notional)
MAX_LEVERAGE = 2.0           # loft over gearing med rigtige penge, uanset hvad strategien bruger
MIN_TRANSFER_USD = 5.0
USD_DKK = 6.9                # kun til visning og indskud i kroner
HOME = "binance-spot"        # her lander indskud, og her står kontanterne
# Automatisk flytning fra Binance til wallets (netværksnavne og minimum fra Binance selv). Polymarket er IKKE med:
# Binance sender USDC på Polygon, men Polymarket bruger USDC.e: walleten veksler det selv (PolymarketWallet.sweep), og
# Kassen sender lidt POL til netværksgebyret. Binances navn for Polygon er MATIC.
WALLET_TRANSFER = {"solana": {"network": "SOL", "gas": ("SOL", 0.02, 10.0)},      # (mønt, fyld op under, køb for USD)
                   "base": {"network": "BASE", "gas": ("ETH", 0.0006, 10.0)},
                   "polymarket": {"network": "MATIC", "gas": ("POL", 0.5, 6.0)}}
TRANSFER_MIN_USD = 10.0
TRANSFER_WAIT_S = 2 * 3600

VENUES = {
    "binance-spot": {"name": "Binance spot", "adapters": {"orion"}, "group": "binance", "kind": "spot", "fee": 0.001},
    "binance-futures": {"name": "Binance futures", "adapters": {"quasar"}, "group": "binance", "kind": "futures", "fee": 0.0005},
    "solana": {"name": "Solana", "adapters": {"nova"}, "group": "solana", "kind": "spot", "fee": 0.005},
    "polymarket": {"name": "Polymarket", "adapters": {"delphi"}, "group": "polygon", "kind": "spot", "fee": 0.0},
    "base": {"name": "Base", "adapters": {"comet"}, "group": "base", "kind": "spot", "fee": 0.003},
}
NEEDS = {"binance-spot": ("BINANCE_API_KEY", "BINANCE_API_SECRET"),
         "binance-futures": ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_FUTURES"),   # futures kun, hvis nøglen må (BINANCE_FUTURES=1)
         "solana": ("SOLANA_PRIVATE_KEY",), "polymarket": ("POLYMARKET_PRIVATE_KEY",), "base": ("BASE_PRIVATE_KEY",)}


def logs_dir() -> Path:
    return Path(os.environ.get("HUB_LIVE_DIR") or ROOT / "logs")


def deposits_file() -> Path:
    return logs_dir() / "kasse-indskud.jsonl"


def enabled_venues() -> list[str]:
    """Markeder, der kan handles for rigtigt: dem, der er nøgler til (eller HUB_LIVE_VENUES, hvis den er sat)."""
    raw = os.environ.get("HUB_LIVE_VENUES")
    if raw:
        return [v.strip() for v in raw.split(",") if v.strip() in VENUES]
    return [v for v in VENUES if all(os.environ.get(k) for k in NEEDS[v])]


def venue_of(bot: dict, venues: list[str] | None = None) -> str | None:
    for v in venues if venues is not None else enabled_venues():
        if bot.get("adapter") in VENUES[v]["adapters"]:
            return v
    return None


def live_bots(bots: list[dict], venues: list[str] | None = None) -> set[str]:
    return {b["key"] for b in bots if not b.get("disabled") and venue_of(b, venues)}


def positions_for(held: dict | None, data: dict, kind: str = "spot") -> list[tuple[str, str, float]]:
    """De positioner, den fulgte strategi har åbne lige nu: (symbol, long/short, gearing). Spot kan kun long."""
    if not held:
        return []
    names = {held["name"], held["id"].split(":", 1)[1]}
    out = []
    for t in data.get("trades", {}).get("open", []):
        if t.get("strategy") not in names or t.get("platform") == "kalshi":   # Kalshi er ikke koblet på endnu
            continue
        if t.get("chain") and t["chain"] != "base":                       # nye tokens: kun Base (ikke Ethereum/BNB Chain)
            continue
        txt = (t.get("side") or "long").lower()
        side = "short" if txt.startswith(("short", "sell", "sælg")) else "long"
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*x", txt)
        lev = float(m.group(1).replace(",", ".")) if m else 1.0
        if kind == "spot" and side != "long":
            continue
        if t.get("market"):                                               # Polymarket: køb den side (YES/NO), strategien har
            side = "long"
        out.append((t.get("mint") or t.get("market") or t.get("token") or t["symbol"], side, lev))   # Solana/Base: møntens adresse
    return out


def targets_for(held: dict | None, data: dict) -> list[str]:
    """De mønter (long), strategien ejer."""
    return [s for s, _side, _lev in positions_for(held, data, "spot")]


# ------------------------------------------------------------------ Binance

class BinanceAPI:
    venue = "Binance"
    base = "https://api.binance.com"

    def __init__(self, key: str = "", secret: str = "", opener=None, base: str | None = None):
        self.key, self.secret = key, secret
        self.base = base or self.base
        self.open = opener or (lambda req: urllib.request.urlopen(req, timeout=20))
        self._filters: dict | None = None

    def _req(self, method: str, path: str, params: dict | None = None, signed: bool = False, base: str | None = None):
        params = dict(params or {})
        headers = {"User-Agent": "papirdesk-hub"}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 10000
            q = urllib.parse.urlencode(params)
            q += "&signature=" + hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = self.key
        else:
            q = urllib.parse.urlencode(params)
        url = (base or self.base) + path + ("?" + q if q and method == "GET" else "")
        req = urllib.request.Request(url, data=q.encode() if method != "GET" else None, headers=headers, method=method)
        try:
            with self.open(req) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Binance {e.code}: {e.read().decode(errors='replace')[:200]}") from None

    @staticmethod
    def _round(qty: float, step: float) -> str:
        step = step or 1e-8
        q = math.floor(qty / step + 1e-9) * step
        dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        return f"{q:.{dec}f}"

    def withdraw(self, coin: str, network: str, address: str, amount: float) -> dict:
        """Send mønter fra Binance til en af Nova AI's egne wallets (adressen skal være på Binance-whitelisten)."""
        return self._req("POST", "/sapi/v1/capital/withdraw/apply", {"coin": coin, "network": network, "address": address,
                                                                      "amount": f"{amount:.6f}".rstrip("0").rstrip(".")}, signed=True, base="https://api.binance.com")

    def transfer(self, amount: float, kind: str) -> dict:
        """Intern overførsel af USDT mellem spot og futures (MAIN_UMFUTURE / UMFUTURE_MAIN). Kræver ikke ret til at hæve."""
        return self._req("POST", "/sapi/v1/asset/transfer", {"type": kind, "asset": "USDT", "amount": f"{amount:.2f}"},
                         signed=True, base="https://api.binance.com")


class BinanceSpot(BinanceAPI):
    venue = "Binance spot"

    def prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        rows = self._req("GET", "/api/v3/ticker/price", {"symbols": json.dumps(sorted(set(symbols)), separators=(",", ":"))})
        return {r["symbol"]: float(r["price"]) for r in rows}

    def filters(self) -> dict[str, dict]:
        if self._filters is None:
            info = self._req("GET", "/api/v3/exchangeInfo", {"permissions": "SPOT"})
            out = {}
            for s in info.get("symbols", []):
                if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT":
                    continue
                f = {x["filterType"]: x for x in s.get("filters", [])}
                out[s["symbol"]] = {"step": float((f.get("LOT_SIZE") or {}).get("stepSize", 0) or 0),
                                    "min": float((f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}).get("minNotional", 5) or 5),
                                    "base": s["baseAsset"]}
            self._filters = out
        return self._filters

    def open_pos(self, symbol: str, side: str, usd: float, lev: float, px: float) -> dict:
        r = self._req("POST", "/api/v3/order", {"symbol": symbol, "side": "BUY", "type": "MARKET", "quoteOrderQty": f"{usd:.2f}"}, signed=True)
        base = self.filters()[symbol]["base"]
        fee_base = sum(float(f["commission"]) for f in r.get("fills", []) if f.get("commissionAsset") == base)
        return {"qty": float(r["executedQty"]) - fee_base, "usd": float(r["cummulativeQuoteQty"]), "id": r.get("orderId")}

    def close_pos(self, symbol: str, pos: dict, px: float) -> dict:
        q = self._round(pos["qty"], self.filters()[symbol]["step"])
        r = self._req("POST", "/api/v3/order", {"symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": q}, signed=True)
        fee_q = sum(float(f["commission"]) for f in r.get("fills", []) if f.get("commissionAsset") == "USDT")
        return {"qty": float(r["executedQty"]), "usd": float(r["cummulativeQuoteQty"]) - fee_q, "id": r.get("orderId")}


class BinanceFutures(BinanceAPI):
    venue = "Binance futures"
    base = "https://fapi.binance.com"

    def prices(self, symbols: list[str]) -> dict[str, float]:
        want = set(symbols)
        return {r["symbol"]: float(r["price"]) for r in self._req("GET", "/fapi/v1/ticker/price") if r["symbol"] in want} if want else {}

    def filters(self) -> dict[str, dict]:
        if self._filters is None:
            out = {}
            for s in self._req("GET", "/fapi/v1/exchangeInfo").get("symbols", []):
                if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT" or s.get("contractType") != "PERPETUAL":
                    continue
                f = {x["filterType"]: x for x in s.get("filters", [])}
                lot = f.get("MARKET_LOT_SIZE") or f.get("LOT_SIZE") or {}
                out[s["symbol"]] = {"step": float(lot.get("stepSize", 0) or 0), "min": float((f.get("MIN_NOTIONAL") or {}).get("notional", 5) or 5),
                                    "base": s["baseAsset"]}
            self._filters = out
        return self._filters

    def open_pos(self, symbol: str, side: str, usd: float, lev: float, px: float) -> dict:
        lev_i = max(1, int(lev))
        self._req("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev_i}, signed=True)
        q = self._round(usd * lev_i / px, self.filters()[symbol]["step"])
        r = self._req("POST", "/fapi/v1/order", {"symbol": symbol, "side": "BUY" if side == "long" else "SELL", "type": "MARKET",
                                                 "quantity": q, "newOrderRespType": "RESULT"}, signed=True)
        entry = float(r.get("avgPrice") or px)
        qty = float(r.get("executedQty") or q)
        fee = qty * entry * VENUES["binance-futures"]["fee"]
        return {"qty": qty, "usd": qty * entry / lev_i + fee, "entry": entry, "margin": qty * entry / lev_i, "lev": lev_i, "id": r.get("orderId")}

    def close_pos(self, symbol: str, pos: dict, px: float) -> dict:
        r = self._req("POST", "/fapi/v1/order", {"symbol": symbol, "side": "SELL" if pos["side"] == "long" else "BUY", "type": "MARKET",
                                                 "quantity": self._round(pos["qty"], self.filters()[symbol]["step"]), "reduceOnly": "true",
                                                 "newOrderRespType": "RESULT"}, signed=True)
        exit_px = float(r.get("avgPrice") or px)
        sign = 1 if pos["side"] == "long" else -1
        pnl = (exit_px - pos["entry"]) * pos["qty"] * sign
        fee = pos["qty"] * exit_px * VENUES["binance-futures"]["fee"]
        return {"qty": pos["qty"], "usd": max(0.0, pos["margin"] + pnl - fee), "id": r.get("orderId")}


def make_broker(venue: str):
    if venue == "solana":
        from .solana import SolanaWallet
        return SolanaWallet()
    if venue == "polymarket":
        from .polymarket import PolymarketWallet
        return PolymarketWallet()
    if venue == "base":
        from .base import BaseWallet
        return BaseWallet()
    key, secret = os.environ.get("BINANCE_API_KEY", ""), os.environ.get("BINANCE_API_SECRET", "")
    return {"binance-spot": BinanceSpot, "binance-futures": BinanceFutures}[venue](key, secret)


# ------------------------------------------------------------------ ét marked

class VenueAccount:
    """Ét marked (fx Binance spot): kontanter og egne positioner, gemt i logs/live-<marked>.json."""

    def __init__(self, venue: str, broker=None, mode: str = "dry", path: Path | None = None, now=time.time, log=print):
        self.venue, self.meta, self.mode = venue, VENUES[venue], mode
        self.broker = broker or make_broker(venue)
        self.path, self.now, self.log = path or logs_dir() / f"live-{venue}.json", now, log
        self.problem = ""
        self.state = self._load()

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("mode") == self.mode and "cash" in d:
                return d
        except (OSError, ValueError):
            pass
        return {"mode": self.mode, "cash": 0.0, "fees": 0.0, "holdings": {}, "orders": [], "prices": {}}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            self.log(f"Live: kunne ikke gemme {self.venue}: {e}")

    def mark(self, sym: str, pos: dict, px: float | None) -> float:
        if px is None:
            return pos.get("last_value", 0.0)
        if self.meta["kind"] == "futures":
            sign = 1 if pos["side"] == "long" else -1
            v = max(0.0, pos["margin"] + (px - pos["entry"]) * pos["qty"] * sign)
        else:
            v = pos["qty"] * px
        pos["last_value"] = v
        return v

    def value(self) -> float:
        px = self.state["prices"]
        return self.state["cash"] + sum(self.mark(s, p, px.get(s)) for s, p in self.state["holdings"].items())

    def refresh(self, extra) -> None:
        self.state["prices"] = self.broker.prices(sorted(set(self.state["holdings"]) | set(extra)))

    def _open(self, sym: str, side: str, usd: float, lev: float, px: float) -> dict:
        if self.mode == "dry":
            if self.meta["kind"] == "futures":
                fee = usd * lev * self.meta["fee"]
                return {"qty": usd * lev / px, "usd": usd + fee, "entry": px, "margin": usd, "lev": lev, "id": "sim"}
            return {"qty": usd * (1 - self.meta["fee"]) / px, "usd": usd, "id": "sim"}
        return self.broker.open_pos(sym, side, usd, lev, px)

    def _close(self, sym: str, pos: dict, px: float) -> dict:
        if self.mode == "dry":
            return {"qty": pos["qty"], "usd": max(0.0, self.mark(sym, pos, px) - pos["qty"] * px * self.meta["fee"]), "id": "sim"}
        return self.broker.close_pos(sym, pos, px)

    def close_missing(self, targets: dict) -> None:
        """Luk positioner, strategierne ikke har mere (eller som har skiftet retning)."""
        st = self.state
        for sym in list(st["holdings"]):
            pos = st["holdings"][sym]
            if sym in targets and targets[sym][0] == pos["side"]:
                continue
            px = st["prices"].get(sym)
            if px is None:
                continue
            worth = self.mark(sym, pos, px)
            if worth < 1.0:                                                # støv eller likvideret: glem den
                st["holdings"].pop(sym)
                continue
            r = self._close(sym, pos, px)
            st["cash"] += r["usd"]
            st["fees"] += max(0.0, worth - r["usd"])
            st["holdings"].pop(sym)
            self._note("LUK", sym, pos["side"], r, px)

    def wants(self, targets: dict) -> float:
        """Beløb til de positioner, der skal åbnes."""
        return sum(t[1] for s, t in targets.items() if s not in self.state["holdings"])

    def open_missing(self, targets: dict) -> None:
        st, flt = self.state, self.broker.filters()
        new = sorted((s for s in targets if s not in st["holdings"] and s in flt), key=lambda s: -targets[s][1])
        for sym in new[:max(0, MAX_POSITIONS - len(st["holdings"]))]:
            side, usd, lev = targets[sym]
            lev = min(MAX_LEVERAGE, max(1.0, lev)) if self.meta["kind"] == "futures" else 1.0
            usd = min(usd, st["cash"])
            px = st["prices"].get(sym)
            if px is None or usd * lev < max(MIN_ORDER_USD, flt[sym]["min"]):
                continue
            if self.meta["kind"] == "futures":
                usd /= 1 + lev * self.meta["fee"]                          # så gebyret også kan betales
            r = self._open(sym, side, usd, lev, px)
            st["cash"] -= r["usd"]
            pos = {"qty": r["qty"], "side": side}
            if self.meta["kind"] == "futures":
                pos.update(entry=r["entry"], margin=r["margin"], lev=r["lev"])
                st["fees"] += r["usd"] - r["margin"]
            else:
                st["fees"] += r["usd"] - r["qty"] * px
            st["holdings"][sym] = pos
            self._note("ÅBN", sym, side, r, px)

    def _note(self, what: str, sym: str, side: str, r: dict, px: float) -> None:
        o = {"ts": round(self.now()), "venue": self.meta["name"], "side": what, "dir": side, "symbol": sym, "qty": r["qty"],
             "usd": round(r["usd"], 4), "price": px, "id": r.get("id"), "sim": self.mode == "dry"}
        self.state["orders"] = (self.state["orders"] + [o])[-200:]
        self.log(f"Live{' (simulering)' if self.mode == 'dry' else ''}: {what} {side} {sym} på {self.meta['name']} for {r['usd']:.2f} USD")

    def view(self) -> dict:
        px = self.state["prices"]
        return {"venue": self.venue, "name": self.meta["name"], "value_usd": round(self.value(), 2), "cash_usd": round(self.state["cash"], 2),
                "fees_usd": round(self.state["fees"], 2), "problem": self.problem,
                "holdings": [{"symbol": s, "side": p["side"], "lev": p.get("lev", 1), "usd": round(self.mark(s, p, px.get(s)), 2)}
                             for s, p in sorted(self.state["holdings"].items())]}


# ------------------------------------------------------------------ Kassen

class Kasse:
    """Stedet, pengene lægges ind. Fordeler dem til markederne efter Smart strategi og flytter dem automatisk
    mellem markeder i samme gruppe (Binance spot ↔ futures)."""

    def __init__(self, venues: list[str] | None = None, mode: str | None = None, budget: float | None = None,
                 brokers: dict | None = None, now=time.time, log=print):
        self.mode = (os.environ.get("HUB_LIVE", "") if mode is None else mode).strip().lower()
        if self.mode in ("0", "off", "nej"):
            self.mode = ""
        self.now, self.log = now, log
        self.notify = None                       # sættes af hubben: besked til telefonen
        self.lock = threading.Lock()
        names = venues if venues is not None else enabled_venues()
        self.venues = {v: VenueAccount(v, (brokers or {}).get(v), self.mode or "dry", now=now, log=log) for v in names}
        self.path = logs_dir() / "kasse.json"
        self.state = self._load()
        self.problem = ""
        if self.mode == "dry" and not self.state["deposits"]:       # simulering: et indskud på budgettet
            self._deposit(float(os.environ.get("HUB_LIVE_BUDGET_USD", "145") if budget is None else budget), "simuleret indskud")

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("mode") == self.mode:
                return d
        except (OSError, ValueError):
            pass
        return {"mode": self.mode, "since": round(self.now()), "deposits": [], "transfers": [], "read_lines": 0, "following": None}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            self.log(f"Kasse: kunne ikke gemme: {e}")
        for v in self.venues.values():
            v.save()

    def home(self) -> VenueAccount:
        return self.venues.get(HOME) or next(iter(self.venues.values()))

    def _deposit(self, usd: float, note: str) -> None:
        if usd < 0:                                      # udbetaling: sælg alt, træk beløbet ud; Smart køber igen for resten
            for v in self.venues.values():
                try:
                    v.refresh([])
                    v.close_missing({})
                except Exception as e:
                    self.log(f"Kasse: kunne ikke sælge på {v.meta['name']} før udbetaling: {e}")
            home = self.home()
            for v in self.venues.values():
                if v is not home and v.state["cash"] > 0 and v.meta["group"] == home.meta["group"]:
                    self._move(v, home, v.state["cash"])
            usd = -min(-usd, max(0.0, home.state["cash"]))
        self.home().state["cash"] += usd
        self.state["deposits"].append({"ts": round(self.now()), "usd": round(usd, 2), "dkk": round(usd * USD_DKK), "note": note})
        self.log(f"Kasse: {'indskud' if usd >= 0 else 'udbetaling'} {abs(usd):.2f} USD ({note})")
        if self.notify:
            self.notify(f"Kasse: {'indskud' if usd >= 0 else 'udbetaling'} på {abs(usd) * USD_DKK:,.0f} kr.".replace(",", "."))

    def read_deposits(self) -> None:
        """Nye indskud registreret med  python3 -m hub kasse indskud <kr>  (en linje pr. indskud i logs/kasse-indskud.jsonl)."""
        try:
            lines = deposits_file().read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines[self.state["read_lines"]:]:
            try:
                d = json.loads(line)
                self._deposit(float(d["dkk"]) / USD_DKK, d.get("note") or "indskud")
            except (ValueError, KeyError, TypeError):
                pass
        self.state["read_lines"] = len(lines)

    def sweep_wallets(self) -> None:
        """Wallets, der selv skal veksle indbetalinger (Polymarket: USDC -> USDC.e). Fejl melder kun et problem; pengene bliver liggende."""
        for k, v in self.venues.items():
            sweep = getattr(v.broker, "sweep", None)
            if sweep is None:
                continue
            try:
                r = sweep()
                if r:
                    self.log(f"Kasse: {v.meta['name']}: veksler indbetaling ({', '.join(r)})")
                    if self.notify:
                        self.notify(f"Kasse: {v.meta['name']}: indbetaling klar til handel")
            except Exception as e:                            # må ikke stoppe de andre markeder; log kun, når beskeden er ny
                msg = f"{v.meta['name']}: kunne ikke veksle indbetalingen: {e}"[:200]
                if self.state.get("sweep_error", {}).get(k) != msg:
                    self.state.setdefault("sweep_error", {})[k] = msg
                    self.log("Kasse: " + msg)
            else:
                self.state.get("sweep_error", {}).pop(k, None)

    def sync_cash(self) -> None:
        """Rigtige penge: kontanterne i hvert marked = det, Binance faktisk har (fri USDT/USDC), så indskud og hævninger
        tæller med af sig selv, og Nova AI aldrig handler for penge, der ikke er der."""
        for k, v in self.venues.items():
            try:
                if k == "solana":
                    from .solana import USDC_MINT
                    v.state["cash"] = v.broker.tokens().get(USDC_MINT, 0.0)
                    continue
                if k == "polymarket":
                    v.state["cash"] = v.broker.usdc()
                    continue
                if k == "base":
                    from .base import USDC
                    v.state["cash"] = v.broker.balance(USDC)
                    continue
                if v.meta["kind"] == "futures":
                    bal = v.broker._req("GET", "/fapi/v2/balance", signed=True)
                    v.state["cash"] = sum(float(b["availableBalance"]) for b in bal if b.get("asset") in ("USDT", "USDC"))
                else:
                    acct = v.broker._req("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)
                    v.state["cash"] = sum(float(b["free"]) for b in acct.get("balances", []) if b.get("asset") == "USDT")
            except Exception as e:
                v.problem = f"Kan ikke læse saldoen: {e}"[:160]

    def ready(self) -> str:
        if not self.mode:
            return "Slået fra (HUB_LIVE er ikke sat)"
        if self.mode == "1" and not self.venues:
            return "Mangler nøgler: sæt Binance- og/eller wallet-nøgler i /etc/nova-ai.env"
        return ""

    def total(self) -> float:
        return sum(v.value() for v in self.venues.values())

    def _move(self, src: VenueAccount, dst: VenueAccount, usd: float) -> None:
        if self.mode == "1":
            kind = {("binance-spot", "binance-futures"): "MAIN_UMFUTURE", ("binance-futures", "binance-spot"): "UMFUTURE_MAIN"}[(src.venue, dst.venue)]
            src.broker.transfer(usd, kind)
        src.state["cash"] -= usd
        dst.state["cash"] += usd
        t = {"ts": round(self.now()), "from": src.meta["name"], "to": dst.meta["name"], "usd": round(usd, 2), "sim": self.mode == "dry"}
        self.state["transfers"] = (self.state["transfers"] + [t])[-100:]
        self.log(f"Kasse: flytter {usd:.2f} USD fra {src.meta['name']} til {dst.meta['name']}")

    def _rebalance(self, plan: dict) -> None:
        """Flyt kontanter derhen, hvor nye positioner skal åbnes; ubrugte kontanter samles i hjemmet (Binance spot)."""
        spare = {k: max(0.0, v.state["cash"] - v.wants(plan.get(k, {}))) for k, v in self.venues.items()}
        for k, dst in self.venues.items():
            short = dst.wants(plan.get(k, {})) - dst.state["cash"]
            for sk, src in self.venues.items():
                if short < MIN_TRANSFER_USD:
                    break
                if sk == k or src.meta["group"] != dst.meta["group"]:
                    continue
                amt = min(short, spare[sk])
                if amt >= MIN_TRANSFER_USD:
                    self._move(src, dst, amt)
                    spare[sk] -= amt
                    short -= amt
        home = self.home()
        for k, v in self.venues.items():
            if k == home.venue or v.meta["group"] != home.meta["group"] or v.state["holdings"] or plan.get(k):
                continue
            if v.state["cash"] >= MIN_TRANSFER_USD:
                self._move(v, home, v.state["cash"])
        if self.mode == "1" and home.venue == HOME:
            for k, dst in self.venues.items():                # wallets: send USDC (og lidt gas) fra Binance, når der mangler
                if k in WALLET_TRANSFER:
                    self._fund_wallet(k, dst, dst.wants(plan.get(k, {})) - dst.state["cash"], home, spare)

    def _fund_wallet(self, k: str, dst: "VenueAccount", short: float, home: "VenueAccount", spare: dict) -> None:
        pend = self.state.setdefault("pending", {})
        p = pend.get(k)
        if p:                                              # en overførsel er på vej: vent, til den er fremme
            if dst.state["cash"] >= p["before"] + 0.8 * p["usd"] or self.now() - p["ts"] > TRANSFER_WAIT_S:
                if self.now() - p["ts"] > TRANSFER_WAIT_S and dst.state["cash"] < p["before"] + 0.8 * p["usd"]:
                    self.problem = f"Overførslen til {dst.meta['name']} er ikke kommet frem efter 2 timer"
                pend.pop(k)
            return
        if short < TRANSFER_MIN_USD:
            return
        cfg, b = WALLET_TRANSFER[k], home.broker
        try:
            coin, low, buy_usd = cfg["gas"]                # først lidt SOL/ETH til netværksgebyrer, hvis walleten mangler
            to = getattr(dst.broker, "deposit_address", None) or dst.broker.address     # Polymarket: nøgle-adressen, ikke proxy
            if k == "solana":
                from .solana import SOL_MINT
                gas = dst.broker.tokens().get(SOL_MINT, 0.0)
            elif k == "polymarket":
                gas = dst.broker.pol()
            else:
                from .base import ETH
                gas = dst.broker.balance(ETH)
            if gas < low and home.state["cash"] >= buy_usd + TRANSFER_MIN_USD:
                r = b.open_pos(f"{coin}USDT", "long", buy_usd, 1, 0)
                home.state["cash"] -= r["usd"]
                b.withdraw(coin, cfg["network"], to, r["qty"])
                self.log(f"Kasse: sender {r['qty']:.5f} {coin} til {dst.meta['name']} til netværksgebyrer")
            amt = min(short + 1.0, max(0.0, min(spare.get(home.venue, 0.0), home.state["cash"])))
            if amt < TRANSFER_MIN_USD:
                return
            r = b.open_pos("USDCUSDT", "long", amt, 1, 0)   # USDT -> USDC (wallets bruger USDC)
            home.state["cash"] -= r["usd"]
            b.withdraw("USDC", cfg["network"], to, r["qty"])
            pend[k] = {"usd": r["qty"], "ts": round(self.now()), "before": dst.state["cash"]}
            t = {"ts": round(self.now()), "from": home.meta["name"], "to": dst.meta["name"], "usd": round(r["qty"], 2), "sim": False}
            self.state["transfers"] = (self.state["transfers"] + [t])[-100:]
            self.log(f"Kasse: sender {r['qty']:.2f} USDC fra Binance til {dst.meta['name']} ({dst.broker.address[:6]}…)")
            if self.notify:
                self.notify(f"Kasse: sender {r['qty']:.2f} USDC fra Binance til {dst.meta['name']}")
        except Exception as e:
            self.problem = f"Kunne ikke flytte penge til {dst.meta['name']}: {e}"[:200]
            self.log("Kasse: " + self.problem)

    def run(self, plan: dict, following: str | None) -> None:
        """plan: {marked: {symbol: (long/short, beløb i USD, gearing)}}. Luk, flyt penge, åbn."""
        with self.lock:
            self.state["following"] = following
            self.read_deposits()
            self.problem = self.ready()
            if self.mode == "1" and not self.problem:
                self.sweep_wallets()
                self.sync_cash()
            try:
                for k, v in self.venues.items():
                    v.problem = ""
                    try:
                        v.refresh(plan.get(k, {}))
                    except Exception as e:
                        v.problem = f"Kan ikke hente kurser: {e}"[:160]
                if not self.problem:
                    for k, v in self.venues.items():
                        if not v.problem:
                            v.close_missing(plan.get(k, {}))
                    self._rebalance(plan)
                    for k, v in self.venues.items():
                        if not v.problem:
                            v.open_missing(plan.get(k, {}))
            except Exception as e:                           # en fejl hos børsen må aldrig vælte hubben; prøv igen næste runde
                self.problem = f"Fejl: {e}"[:200]
                self.log("Kasse: " + self.problem)
            finally:
                self.save()

    def view(self) -> dict:
        with self.lock:
            total = self.total()
            dep = sum(d["usd"] for d in self.state["deposits"])
            orders = sorted((o for v in self.venues.values() for o in v.state["orders"]), key=lambda o: -o["ts"])[:12]
            return {"mode": self.mode, "problem": self.problem or self.ready(), "since": self.state["since"],
                    "deposited_usd": round(dep, 2), "deposited_dkk": round(dep * USD_DKK), "value_usd": round(total, 2),
                    "value_dkk": round(total * USD_DKK), "return_pct": round((total / dep - 1) * 100, 2) if dep else 0.0,
                    "fees_usd": round(sum(v.state["fees"] for v in self.venues.values()), 2), "following": self.state.get("following"),
                    "venues": [v.view() for v in self.venues.values()], "deposits": self.state["deposits"][-5:][::-1],
                    "transfers": self.state["transfers"][-8:][::-1], "orders": orders}


def register_deposit(dkk: float, note: str = "") -> None:
    """Fra kommandolinjen: læg et indskud i køen; hubben lægger det i Kassen inden for et minut."""
    f = deposits_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": round(time.time()), "dkk": float(dkk), "note": note or "indskud"}) + "\n")
