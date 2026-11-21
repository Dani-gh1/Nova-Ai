"""Live-kontoen: rigtige penge hos en rigtig børs, styret af Live-Smart.

Live-Smart er en Smart-bot (samme regler som Smart strategi), der kun vælger blandt strategier, vi faktisk kan handle for
rigtigt. Live-kontoen ejer de samme mønter som den strategi, Live-Smart følger (lige store beløb), og ikke andet.

Tilstande (HUB_LIVE i /etc/hub.env):
  tom/0   slået fra
  dry     simulering: rigtige priser, ingen ordrer sendes (standard, når du prøver det af)
  1       RIGTIGE ORDRER (kræver BINANCE_API_KEY og BINANCE_API_SECRET)

Sikkerhed:
  * Bruger højst HUB_LIVE_BUDGET_USD (standard 145 USD ≈ 1.000 kr.) plus/minus det, kontoen selv har tjent/tabt.
  * Rører kun de mønter, den selv har købt (aldrig andet på din konto). Kun spot: ingen gearing, ingen short.
  * API-nøglen skal oprettes UDEN ret til at hæve penge, og gerne låst til serverens IP-adresse.
  * Stop med det samme: opret filen logs/live-stop (eller sæt HUB_LIVE=0). Så sælges intet og købes intet.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_POSITIONS = 8            # højst så mange mønter ad gangen (små beløb: hver ordre skal over børsens minimum)
MIN_ORDER_USD = 6.0          # Binance kræver mindst 5 USDT pr. ordre
FEE = 0.001                  # Binance spot: 0,10 % pr. side (bruges i simuleringen)
USD_DKK = 6.9                # kun til visning i kroner
LIVE_ADAPTERS = {"orion": "binance"}       # bot-adapter -> børs, der kan handle dens strategier for rigtigt


def state_file() -> Path:
    return Path(os.environ.get("HUB_LIVE_FILE") or ROOT / "logs" / "live.json")


def stop_file() -> Path:
    return Path(os.environ.get("HUB_LIVE_STOP") or ROOT / "logs" / "live-stop")


def live_bots(bots: list[dict]) -> set[str]:
    return {b["key"] for b in bots if LIVE_ADAPTERS.get(b.get("adapter")) and not b.get("disabled")}


class BinanceSpot:
    """Binance spot via deres REST-API (kun standardbiblioteket). Offentlige kald kræver ingen nøgle."""
    venue = "Binance"

    def __init__(self, key: str = "", secret: str = "", base: str = "https://api.binance.com", opener=None):
        self.key, self.secret, self.base = key, secret, base
        self.open = opener or (lambda req: urllib.request.urlopen(req, timeout=20))
        self._filters: dict | None = None

    def _req(self, method: str, path: str, params: dict | None = None, signed: bool = False):
        params = dict(params or {})
        headers = {"User-Agent": "papirdesk-hub"}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 10000
            q = urllib.parse.urlencode(params)
            params_q = q + "&signature=" + hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = self.key
        else:
            params_q = urllib.parse.urlencode(params)
        url = self.base + path + ("?" + params_q if params_q and method == "GET" else "")
        data = params_q.encode() if method != "GET" else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.open(req) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Binance {e.code}: {e.read().decode(errors='replace')[:200]}") from None

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
                step = float((f.get("LOT_SIZE") or {}).get("stepSize", 0) or 0)
                mn = float((f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}).get("minNotional", 5) or 5)
                out[s["symbol"]] = {"step": step, "min": mn, "base": s["baseAsset"]}
            self._filters = out
        return self._filters

    def free(self) -> dict[str, float]:
        acct = self._req("GET", "/api/v3/account", signed=True)
        return {b["asset"]: float(b["free"]) for b in acct.get("balances", []) if float(b["free"]) > 0}

    def buy(self, symbol: str, usd: float) -> dict:
        r = self._req("POST", "/api/v3/order", {"symbol": symbol, "side": "BUY", "type": "MARKET", "quoteOrderQty": f"{usd:.2f}"}, signed=True)
        base = self.filters()[symbol]["base"]
        fee_base = sum(float(f["commission"]) for f in r.get("fills", []) if f.get("commissionAsset") == base)
        return {"qty": float(r["executedQty"]) - fee_base, "usd": float(r["cummulativeQuoteQty"]), "id": r.get("orderId")}

    def sell(self, symbol: str, qty: float) -> dict:
        step = self.filters()[symbol]["step"] or 1e-8
        q = math.floor(qty / step + 1e-9) * step
        dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        r = self._req("POST", "/api/v3/order", {"symbol": symbol, "side": "SELL", "type": "MARKET", "quantity": f"{q:.{dec}f}"}, signed=True)
        fee_q = sum(float(f["commission"]) for f in r.get("fills", []) if f.get("commissionAsset") == "USDT")
        return {"qty": float(r["executedQty"]), "usd": float(r["cummulativeQuoteQty"]) - fee_q, "id": r.get("orderId")}


class LiveAccount:
    """Holder live-kontoen i takt med den strategi, Live-Smart følger. Gemmer alt i logs/live.json."""

    def __init__(self, broker: BinanceSpot | None = None, mode: str | None = None, budget: float | None = None,
                 path: Path | None = None, now=time.time, log=print):
        self.mode = (os.environ.get("HUB_LIVE", "") if mode is None else mode).strip().lower()
        if self.mode in ("0", "off", "nej"):
            self.mode = ""
        self.budget = float(os.environ.get("HUB_LIVE_BUDGET_USD", "145") if budget is None else budget)
        self.broker = broker or BinanceSpot(os.environ.get("BINANCE_API_KEY", ""), os.environ.get("BINANCE_API_SECRET", ""))
        self.path, self.now, self.log = path or state_file(), now, log
        self.lock = threading.Lock()
        self.problem = ""
        self.state = self._load()

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("mode") == self.mode and "cash" in d:
                if d.get("budget") != self.budget:          # budgettet er ændret (fx flere penge lagt ind): forskellen bliver kontant
                    d["cash"] += self.budget - d.get("budget", 0.0)
                    d["budget"] = self.budget
                return d
        except (OSError, ValueError):
            pass
        return {"mode": self.mode, "since": round(self.now()), "budget": self.budget, "cash": self.budget, "fees": 0.0,
                "holdings": {}, "orders": [], "following": None, "value": self.budget, "prices": {}}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            self.log(f"Live: kunne ikke gemme: {e}")

    def ready(self) -> str:
        """Tom tekst, hvis kontoen må handle; ellers hvorfor ikke."""
        if not self.mode:
            return "Slået fra (HUB_LIVE er ikke sat)"
        if stop_file().exists():
            return "Stoppet (filen logs/live-stop findes)"
        if self.mode == "1" and not (self.broker.key and self.broker.secret):
            return "Mangler BINANCE_API_KEY og BINANCE_API_SECRET i /etc/hub.env"
        return ""

    def _order(self, side: str, symbol: str, amount: float, price: float) -> dict | None:
        """Én ordre. Simulering: rigtig pris og 0,10 % gebyr. Rigtig: sendes til børsen."""
        if self.mode == "dry":
            if side == "BUY":
                return {"qty": amount * (1 - FEE) / price, "usd": amount, "id": "sim"}
            return {"qty": amount, "usd": amount * price * (1 - FEE), "id": "sim"}
        return self.broker.buy(symbol, amount) if side == "BUY" else self.broker.sell(symbol, amount)

    def sync(self, targets: list[str], following: str | None) -> None:
        """Gør beholdningen lig med `targets` (de mønter, den fulgte strategi ejer nu), lige store beløb."""
        with self.lock:
            st = self.state
            why = self.ready()
            st["following"], self.problem = following, why
            if why and self.mode:
                self._mark()
                return
            if not self.mode:
                return
            try:
                flt = self.broker.filters()
                targets = [s for s in dict.fromkeys(targets) if s in flt][:MAX_POSITIONS]
                px = self.broker.prices(list(st["holdings"]) + targets)
                st["prices"] = px
                real_free = self.broker.free() if self.mode == "1" else None
                for sym in [s for s in st["holdings"] if s not in targets]:          # sælg det, strategien ikke ejer mere
                    qty = st["holdings"][sym]
                    if real_free is not None:
                        qty = min(qty, real_free.get(flt[sym]["base"], 0.0))
                    if qty * px.get(sym, 0) < flt.get(sym, {}).get("min", 5):
                        st["holdings"].pop(sym)                                     # støv under børsens minimum: glem det
                        continue
                    r = self._order("SELL", sym, qty, px[sym])
                    st["cash"] += r["usd"]
                    st["fees"] += qty * px[sym] - r["usd"]
                    st["holdings"].pop(sym)
                    self._note("SÆLG", sym, r, px[sym])
                missing = [s for s in targets if s not in st["holdings"]]
                if missing:
                    value = st["cash"] + sum(q * px.get(s, 0) for s, q in st["holdings"].items())
                    each = min(value / len(targets), st["cash"] / len(missing))
                    if real_free is not None:
                        each = min(each, real_free.get("USDT", 0.0) / len(missing))
                    for sym in missing:
                        if each < max(MIN_ORDER_USD, flt[sym]["min"]) or sym not in px:
                            continue
                        r = self._order("BUY", sym, each, px[sym])
                        st["cash"] -= r["usd"]
                        st["fees"] += r["usd"] - r["qty"] * px[sym]
                        st["holdings"][sym] = st["holdings"].get(sym, 0.0) + r["qty"]
                        self._note("KØB", sym, r, px[sym])
                self._mark()
            except Exception as e:                        # en fejl hos børsen må aldrig vælte hubben; prøv igen næste runde
                self.problem = f"Fejl hos {self.broker.venue}: {e}"[:200]
                self.log("Live: " + self.problem)
            finally:
                self._save()

    def _mark(self) -> None:
        st = self.state
        st["value"] = round(st["cash"] + sum(q * st["prices"].get(s, 0) for s, q in st["holdings"].items()), 4)
        st["updated"] = round(self.now())

    def _note(self, side: str, sym: str, r: dict, price: float) -> None:
        o = {"ts": round(self.now()), "side": side, "symbol": sym, "qty": r["qty"], "usd": round(r["usd"], 4), "price": price,
             "id": r.get("id"), "sim": self.mode == "dry"}
        self.state["orders"] = (self.state["orders"] + [o])[-200:]
        self.log(f"Live{' (simulering)' if self.mode == 'dry' else ''}: {side} {sym} for {r['usd']:.2f} USD")

    def view(self) -> dict:
        with self.lock:
            st = self.state
            px = st.get("prices", {})
            ret = (st["value"] / st["budget"] - 1) * 100 if st["budget"] else 0.0
            return {"mode": self.mode, "venue": self.broker.venue, "problem": self.problem or self.ready(), "budget_usd": st["budget"],
                    "value_usd": round(st["value"], 2), "value_dkk": round(st["value"] * USD_DKK), "budget_dkk": round(st["budget"] * USD_DKK),
                    "return_pct": round(ret, 2), "cash_usd": round(st["cash"], 2), "fees_usd": round(st["fees"], 2),
                    "following": st.get("following"), "since": st["since"], "updated": st.get("updated"),
                    "holdings": [{"symbol": s, "qty": q, "usd": round(q * px.get(s, 0), 2)} for s, q in sorted(st["holdings"].items())],
                    "orders": st["orders"][-10:][::-1]}


def targets_for(held: dict | None, data: dict) -> list[str]:
    """De mønter (long), den fulgte strategi ejer lige nu, ud fra botens åbne handler."""
    if not held:
        return []
    key = held["id"].split(":", 1)[1]
    names = {held["name"], key}
    return [t["symbol"] for t in data.get("trades", {}).get("open", [])
            if t.get("strategy") in names and (t.get("side") or "long").lower() in ("long", "buy", "køb")]
