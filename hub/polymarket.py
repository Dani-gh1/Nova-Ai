"""Polymarket: Nova AI's egen wallet (POLYMARKET_PRIVATE_KEY i /etc/nova-ai.env) og handel via Polymarkets officielle bibliotek.

* Saldo: USDC på Polygon (den slags USDC, Polymarket bruger) + værdien af åbne positioner fra Polymarkets data-API.
* Logger du ind på polymarket.com med MetaMask, opretter Polymarket en "proxy-wallet", hvor pengene ligger. Dens adresse
  (står under din profil på polymarket.com) sættes i POLYMARKET_FUNDER; ellers bruges MetaMask-adressen selv.
* Køb/salg: markedsordrer (fyld helt eller slet ikke), underskrevet her på serveren.
"""
from __future__ import annotations

import json
import os
import urllib.request

RPC = os.environ.get("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com")
DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"      # Polymarkets betalingsmønt (tjekket mod py_clob_client og kæden)


USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"   # USDC på Polygon, som Binance sender (Polymarket bruger USDC.e, se ovenfor)
POL = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"            # KyberSwaps betegnelse for POL (Polygons gas)
MIN_POL = 0.2                                                 # så meget POL skal der mindst være til en veksling (koster ca. 0,05)
MIN_SWEEP_USD = 1.0
SWAP_SLIPPAGE_BPS = 50                                        # to dollar-mønter: højst 0,5 % slip


class PolygonSigner:
    """Veksler USDC -> USDC.e på Polygon med samme nøgle som Polymarket-walleten (genbruger Base-walletens kode)."""

    def __new__(cls, key: str, opener=None):
        from .base import BaseWallet

        class W(BaseWallet):
            venue = "Polygon"
            rpc_url, kyber, chain_id, slippage_bps = RPC, "https://aggregator-api.kyberswap.com/polygon/api/v1", 137, SWAP_SLIPPAGE_BPS
        w = W(key, opener)
        w._dec.update({USDC_NATIVE.lower(): 6, USDC_E.lower(): 6, POL.lower(): 18})
        return w


class PolymarketWallet:
    venue = "Polymarket"

    def __init__(self, key: str | None = None, funder: str | None = None, opener=None):
        from eth_account import Account
        self.key = (key if key is not None else os.environ.get("POLYMARKET_PRIVATE_KEY", "")).strip()
        if not self.key.startswith("0x"):
            self.key = "0x" + self.key
        self.signer = Account.from_key(self.key).address
        self.funder = (funder if funder is not None else os.environ.get("POLYMARKET_FUNDER", "")).strip() or None
        self.address = self.funder or self.signer
        self.open = opener or (lambda req: urllib.request.urlopen(req, timeout=30))
        self._client = None
        self._tokens: dict[str, str] = {}

    def token(self, sym: str) -> str:
        """Delphis "polymarket:<marked-id>|YES" (eller |NO) -> Polymarkets token for den side. Et rent token gives uændret."""
        if "|" not in sym:
            return sym
        if sym not in self._tokens:
            key, side = sym.rsplit("|", 1)
            m = self._http(f"{GAMMA}/markets/{key.split(':', 1)[1]}")
            toks = json.loads(m["clobTokenIds"])
            if len(toks) != 2:
                raise RuntimeError(f"Polymarket-marked {key} har ikke to udfald")
            self._tokens[sym] = toks[0 if side == "YES" else 1]     # som Delphi: første udfald = JA, andet = NEJ (også i sport)
        return self._tokens[sym]

    def _http(self, url: str, body: dict | None = None):
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "User-Agent": "nova-ai"},
                                     method="POST" if body is not None else "GET")
        with self.open(req) as r:
            return json.loads(r.read().decode())

    # ---------- saldo ----------
    def usdc(self) -> float:
        data = "0x70a08231" + self.address[2:].lower().rjust(64, "0")      # balanceOf(address)
        res = self._http(RPC, {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": USDC_E, "data": data}, "latest"]})
        if "error" in res:
            raise RuntimeError(f"Polygon: {res['error'].get('message')}")
        return int(res["result"], 16) / 1e6

    # ---------- indbetaling fra Binance ----------
    @property
    def deposit_address(self) -> str:
        """Hvor Binance sender til: nøgle-adressen (den, der kan veksle), ikke proxy-adressen."""
        return self.signer

    def signer_wallet(self):
        if getattr(self, "_signer_wallet", None) is None:
            self._signer_wallet = PolygonSigner(self.key)
        return self._signer_wallet

    def pol(self) -> float:
        return self.signer_wallet().balance(POL)

    def sweep(self) -> dict | None:
        """Binance sender USDC (Polygon). Polymarket bruger USDC.e: veksl USDC -> USDC.e, og send det videre til proxy-walleten
        (POLYMARKET_FUNDER), hvis den er sat. Gør ingenting, hvis der intet ligger. Kaster en fejl, hvis der mangler POL."""
        w, out = self.signer_wallet(), {}
        usdc = w.balance(USDC_NATIVE)
        if usdc >= MIN_SWEEP_USD:
            if w.balance(POL) < MIN_POL:
                raise RuntimeError("Polymarket-walleten mangler POL til netværksgebyr (Kassen køber det ved næste overførsel)")
            out["swap"] = w.swap(USDC_NATIVE, USDC_E, usdc)
        if self.funder and self.funder.lower() != self.signer.lower():
            e = w.balance(USDC_E)
            if e >= MIN_SWEEP_USD:
                if w.balance(POL) < MIN_POL:
                    raise RuntimeError("Polymarket-walleten mangler POL til netværksgebyr")
                out["forward"] = {"usd": e, "id": w.transfer(USDC_E, self.funder, e)}
        return out or None

    def positions(self) -> list[dict]:
        res = self._http(f"{DATA}/v2/positions?user={self.address}") or {}        # v1 er lukket (410)
        return res.get("data", []) if isinstance(res, dict) else res

    def value_usd(self) -> dict:
        cash, pos = self.usdc(), self.positions()
        held = sum(float(p.get("currentValue") or p.get("current_value") or 0) for p in pos)
        return {"address": self.address, "usd": round(cash + held, 2), "usdc": round(cash, 2), "positions": len(pos),
                "positions_usd": round(held, 2)}

    # ---------- handel ----------
    def client(self):
        if self._client is None:
            from py_clob_client.client import ClobClient
            c = ClobClient(CLOB, key=self.key, chain_id=137, signature_type=2 if self.funder else 0, funder=self.funder)
            c.set_api_creds(c.create_or_derive_api_creds())
            self._client = c
        return self._client

    def market_order(self, token_id: str, side: str, amount: float) -> dict:
        """side BUY: amount = USDC at bruge. side SELL: amount = antal andele at sælge."""
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        c = self.client()
        order = c.create_market_order(MarketOrderArgs(token_id=token_id, amount=float(amount), side=side))
        res = c.post_order(order, OrderType.FOK)
        if not res or not res.get("success", True) or res.get("errorMsg"):
            raise RuntimeError(f"Polymarket afviste ordren: {res}")
        return res

    # ---------- som marked i Kassen ----------
    def filters(self) -> dict:
        class Any(dict):
            def __contains__(self, k):
                return True

            def __getitem__(self, k):
                return {"step": 0.0, "min": 1.0, "base": k}
        return Any()

    def prices(self, tokens) -> dict[str, float]:
        out = {}
        for t in set(tokens):
            try:
                out[t] = float(self._http(f"{CLOB}/midpoint?token_id={self.token(t)}")["mid"])
            except Exception:
                pass
        return out

    def open_pos(self, token: str, side: str, usd: float, lev: float, px: float) -> dict:
        r = self.market_order(self.token(token), "BUY", usd)
        qty = float(r.get("takingAmount") or 0) or usd / px
        return {"qty": qty, "usd": float(r.get("makingAmount") or usd), "id": r.get("orderID")}

    def close_pos(self, token: str, pos: dict, px: float) -> dict:
        r = self.market_order(self.token(token), "SELL", pos["qty"])
        return {"qty": pos["qty"], "usd": float(r.get("takingAmount") or pos["qty"] * px), "id": r.get("orderID")}
