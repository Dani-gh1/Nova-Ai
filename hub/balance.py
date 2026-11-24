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
        self._kl: dict[tuple, float] = {}          # (mønt, minut) -> pris dengang
        self._events: list[dict] = []              # indbetalinger og hævninger til appens historik

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

    @staticmethod
    def _ms(v, default: float) -> float:
        """Tidspunkt i millisekunder fra Binances felter (tal eller 'YYYY-MM-DD HH:MM:SS' i UTC)."""
        if isinstance(v, (int, float)):
            return float(v)
        try:
            from datetime import datetime, timezone
            return datetime.strptime(str(v)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000
        except ValueError:
            return default

    def _usd_at(self, asset: str, amount: float, ts_ms: float, px: dict) -> float:
        """Hvad pengene var værd i USD, DA de kom ind eller ud (Binances egen 1-minutskurs). Så en krypto, der senere stiger eller
        falder, ikke ændrer det indsatte beløb, og kursgevinsten tæller som gevinst. Uden kurs bruges dagens."""
        if asset in STABLE:
            return amount
        key = (asset, int(ts_ms // 60000))
        if key not in self._kl:
            v = None
            for q in ("USDT", "USDC"):
                try:
                    rows = self.api._req("GET", "/api/v3/klines", {"symbol": asset + q, "interval": "1m", "startTime": int(ts_ms), "limit": 1})
                    if rows:
                        v = float(rows[0][4])
                        break
                except Exception:
                    pass
            self._kl[key] = v if v else (self.usd(asset, 1.0, px) or 0.0)
        return amount * self._kl[key]

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
        ev = self._events = []
        note = lambda ts, kind, what, usd: ev.append({"ts": round(ts / 1000), "kind": kind, "what": what, "usd": round(usd, 2)})
        total, step, now_ms = 0.0, 89 * 86400 * 1000, int(self.now() * 1000)
        windows = [(t, min(t + step, now_ms)) for t in range(start, now_ms, step)]    # Binance giver højst 90 dage ad gangen
        for a, z in windows:
            for d in self.api._req("GET", "/sapi/v1/capital/deposit/hisrec", {"startTime": a, "endTime": z}, signed=True) or []:
                if int(d.get("status", 0)) in (1, 6):             # 1 = gennemført, 6 = krediteret (kan endnu ikke hæves)
                    ts = self._ms(d.get("insertTime"), now_ms)
                    v = self._usd_at(d["coin"], float(d["amount"]), ts, px)
                    total += v
                    note(ts, "ind", f"{float(d['amount']):g} {d['coin']}", v)
            for w in self.api._req("GET", "/sapi/v1/capital/withdraw/history", {"startTime": a, "endTime": z}, signed=True) or []:
                if int(w.get("status", 0)) == 6:                  # 6 = gennemført
                    ts = self._ms(w.get("applyTime"), now_ms)
                    v = self._usd_at(w["coin"], float(w["amount"]) + float(w.get("transactionFee") or 0), ts, px)
                    total -= v
                    note(ts, "ud", f"{float(w['amount']):g} {w['coin']}", v)
        for kind, sign in (("0", 1), ("1", -1)):                  # bankoverførsler ind (0) og ud (1)
            res = self.api._req("GET", "/sapi/v1/fiat/orders", {"transactionType": kind, "beginTime": start}, signed=True) or {}
            for o in res.get("data") or []:
                if o.get("status") in ("Successful", "Finished", "Completed"):
                    ts = self._ms(o.get("createTime"), now_ms)
                    v = self._usd_at(o["fiatCurrency"], float(o["amount"]) - float(o.get("totalFee") or 0), ts, px)
                    total += sign * v
                    note(ts, "ind" if sign > 0 else "ud", f"{float(o['amount']):g} {o['fiatCurrency']} (bank)", v)
        res = self.api._req("GET", "/sapi/v1/fiat/payments", {"transactionType": "0", "beginTime": start}, signed=True) or {}
        for p in res.get("data") or []:                          # kortkøb af krypto
            if p.get("status") in ("Completed", "Successful"):
                ts = self._ms(p.get("createTime"), now_ms)
                v = self._usd_at(p["fiatCurrency"], float(p["sourceAmount"]), ts, px)
                total += v
                note(ts, "ind", f"{float(p['sourceAmount']):g} {p['fiatCurrency']} (kort)", v)
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
                   "balance_dkk": dkk(bal), "earned_dkk": dkk(bal - dep), "deposited_dkk": dkk(dep), "at": round(self.now()),
                   "usd_dkk": round(DKK_PER_EUR / eur, 4) if eur else None,
                   "events": sorted(self._events, key=lambda e: -e["ts"])[:50]}
            self._cache = (self.now(), out)
            return out
