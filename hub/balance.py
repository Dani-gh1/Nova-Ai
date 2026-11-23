"""Den rigtige saldo, læst direkte hos Binance (ikke regnet ud af Nova AI selv).

* Saldo: alle mønter i spot (fri + låst) regnet om med Binances kurser lige nu, plus futures-kontoen inkl. gevinst/tab på
  åbne positioner. Samme princip som Binances "Estimated Balance" i USD.
* Indsat: indbetalinger minus hævninger fra Binances egen historik (krypto, kortkøb og bankoverførsler), så det ikke skal
  skrives ind i hånden.
* Tjent/tabt = saldo - indsat.
Læses højst hvert minut. Kroner er en omregning (ca.), dollars er Binances tal.
"""
from __future__ import annotations

import threading
import time

STABLE = {"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "USDP", "DAI"}
DKK_PER_EUR = 7.46          # kronen er bundet til euroen
YEAR_S = 365 * 86400


class BinanceBalance:
    def __init__(self, api, futures_api=None, now=time.time, ttl: float = 60):
        self.api, self.fut, self.now, self.ttl = api, futures_api, now, ttl
        self.lock = threading.Lock()
        self._cache: tuple[float, dict] | None = None

    # ---------- kurser ----------
    def _prices(self) -> dict[str, float]:
        return {r["symbol"]: float(r["price"]) for r in self.api._req("GET", "/api/v3/ticker/price")}

    @staticmethod
    def usd(asset: str, amount: float, px: dict[str, float]) -> float:
        if asset in STABLE:
            return amount
        for q in ("USDT", "USDC", "FDUSD"):
            if asset + q in px:
                return amount * px[asset + q]
        if "BTC" + "USDT" in px and asset + "BTC" in px:
            return amount * px[asset + "BTC"] * px["BTCUSDT"]
        if asset == "EUR" and "EURUSDT" in px:
            return amount * px["EURUSDT"]
        if asset == "DKK" and "EURUSDT" in px:
            return amount / DKK_PER_EUR * px["EURUSDT"]
        return 0.0

    # ---------- saldo ----------
    def _spot_usd(self, px: dict) -> float:
        acct = self.api._req("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)
        return sum(self.usd(b["asset"], float(b["free"]) + float(b["locked"]), px) for b in acct.get("balances", []))

    def _futures_usd(self, px: dict) -> float:
        if self.fut is None:                              # futures kun, hvis nøglen må (BINANCE_FUTURES=1)
            return 0.0
        acct = self.fut._req("GET", "/fapi/v2/account", signed=True)
        return sum(self.usd(a["asset"], float(a["marginBalance"]), px) for a in acct.get("assets", []) if float(a.get("marginBalance") or 0))

    # ---------- indsat ----------
    def _deposited_usd(self, px: dict) -> float:
        start = int((self.now() - YEAR_S) * 1000)
        total, step, now_ms = 0.0, 89 * 86400 * 1000, int(self.now() * 1000)
        windows = [(t, min(t + step, now_ms)) for t in range(start, now_ms, step)]    # Binance giver højst 90 dage ad gangen
        for a, z in windows:
            for d in self.api._req("GET", "/sapi/v1/capital/deposit/hisrec", {"startTime": a, "endTime": z}, signed=True) or []:
                if int(d.get("status", 0)) == 1:                  # 1 = gennemført
                    total += self.usd(d["coin"], float(d["amount"]), px)
            for w in self.api._req("GET", "/sapi/v1/capital/withdraw/history", {"startTime": a, "endTime": z}, signed=True) or []:
                if int(w.get("status", 0)) == 6:                  # 6 = gennemført
                    total -= self.usd(w["coin"], float(w["amount"]) + float(w.get("transactionFee") or 0), px)
        for kind, sign in (("0", 1), ("1", -1)):                  # bankoverførsler ind (0) og ud (1)
            res = self.api._req("GET", "/sapi/v1/fiat/orders", {"transactionType": kind, "beginTime": start}, signed=True) or {}
            for o in res.get("data") or []:
                if o.get("status") in ("Successful", "Finished", "Completed"):
                    total += sign * self.usd(o["fiatCurrency"], float(o["amount"]) - float(o.get("totalFee") or 0), px)
        res = self.api._req("GET", "/sapi/v1/fiat/payments", {"transactionType": "0", "beginTime": start}, signed=True) or {}
        for p in res.get("data") or []:                          # kortkøb af krypto
            if p.get("status") in ("Completed", "Successful"):
                total += self.usd(p["fiatCurrency"], float(p["sourceAmount"]), px)
        return total

    def read(self) -> dict:
        """{balance_usd, deposited_usd, earned_usd, balance_dkk, earned_dkk, at}. Kastes en fejl, vises den seneste."""
        with self.lock:
            if self._cache and self.now() - self._cache[0] < self.ttl:
                return self._cache[1]
            px = self._prices()
            bal = self._spot_usd(px) + self._futures_usd(px)
            dep = self._deposited_usd(px)
            eur = px.get("EURUSDT") or 0
            dkk = (lambda v: round(v / eur * DKK_PER_EUR, 2)) if eur else (lambda v: None)
            out = {"balance_usd": round(bal, 2), "deposited_usd": round(dep, 2), "earned_usd": round(bal - dep, 2),
                   "balance_dkk": dkk(bal), "earned_dkk": dkk(bal - dep), "deposited_dkk": dkk(dep), "at": round(self.now())}
            self._cache = (self.now(), out)
            return out
