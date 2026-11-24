"""Base: Nova AI's egen wallet (BASE_PRIVATE_KEY i /etc/nova-ai.env) og handel med nye tokens via KyberSwap.

* Saldo: ETH + USDC (+ de tokens, Nova AI selv har købt), regnet om i USD.
* Køb/salg: KyberSwap finder den bedste pris på tværs af Bases børser; handlen underskrives her og sendes til Base.
* Betaling sker i USDC. Der skal ligge lidt ETH på Base til netværksgebyrer.
Kun Comets positioner på Base handles; dem på Ethereum og BNB Chain springes over.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

RPC = os.environ.get("BASE_RPC", "https://mainnet.base.org")
KYBER = "https://aggregator-api.kyberswap.com/base/api/v1"
CHAIN_ID = 8453
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"          # USDC på Base (tjekket mod kæden: symbol USDC, 6 decimaler)
ETH = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"           # KyberSwaps betegnelse for ETH
SLIPPAGE_BPS = 300                                          # højst 3 % slip på et nyt token


def _word(hexaddr: str) -> str:
    return hexaddr[2:].lower().rjust(64, "0")


class BaseWallet:
    venue = "Base"
    rpc_url, kyber, chain_id, slippage_bps = RPC, KYBER, CHAIN_ID, SLIPPAGE_BPS      # underklasser kan bruge et andet netværk (Polygon)

    def __init__(self, key: str | None = None, opener=None):
        from eth_account import Account
        self.Account = Account
        k = (key if key is not None else os.environ.get("BASE_PRIVATE_KEY", "")).strip()
        self.key = k if k.startswith("0x") else "0x" + k
        self.address = Account.from_key(self.key).address
        self.open = opener or (lambda req: urllib.request.urlopen(req, timeout=30))
        self._dec: dict[str, int] = {USDC.lower(): 6, ETH.lower(): 18}

    # ---------- net ----------
    def _http(self, url: str, body: dict | None = None):
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "User-Agent": "nova-ai", "x-client-id": "nova-ai"},
                                     method="POST" if body is not None else "GET")
        with self.open(req) as r:
            return json.loads(r.read().decode())

    def rpc(self, method: str, params: list):
        res = self._http(self.rpc_url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if res.get("error"):
            raise RuntimeError(f"Base {method}: {res['error'].get('message')}")
        return res["result"]

    # ---------- saldo ----------
    def decimals(self, token: str) -> int:
        t = token.lower()
        if t not in self._dec:
            self._dec[t] = int(self.rpc("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]), 16)
        return self._dec[t]

    def balance(self, token: str) -> float:
        if token.lower() == ETH.lower():
            raw = int(self.rpc("eth_getBalance", [self.address, "latest"]), 16)
        else:
            raw = int(self.rpc("eth_call", [{"to": token, "data": "0x70a08231" + _word(self.address)}, "latest"]), 16)
        return raw / 10 ** self.decimals(token)

    def quote_usd(self, token: str, amount: float) -> float:
        """Hvad `amount` af et token er værd i USDC lige nu (KyberSwaps pris)."""
        if amount <= 0:
            return 0.0
        if token.lower() == USDC.lower():
            return amount
        raw = int(amount * 10 ** self.decimals(token))
        d = self._http(f"{KYBER}/routes?tokenIn={token}&tokenOut={USDC}&amountIn={raw}").get("data") or {}
        return int((d.get("routeSummary") or {}).get("amountOut") or 0) / 1e6

    def value_usd(self, tokens=()) -> dict:
        eth, usdc = self.balance(ETH), self.balance(USDC)
        held = {t: self.balance(t) for t in tokens}
        parts = {"ETH": round(self.quote_usd(ETH, eth), 2), "USDC": round(usdc, 2)}
        parts.update({t: round(self.quote_usd(t, q), 2) for t, q in held.items() if q > 0})
        return {"address": self.address, "usd": round(sum(parts.values()), 2), "eth": eth, "usdc": usdc, "parts": parts}

    # ---------- handel ----------
    def _send(self, to: str, data: str, value: int = 0) -> str:
        tx = {"from": self.address, "to": to, "data": data, "value": hex(value)}
        gas = int(int(self.rpc("eth_estimateGas", [tx]), 16) * 1.3)
        base_fee = int(self.rpc("eth_getBlockByNumber", ["latest", False])["baseFeePerGas"], 16)
        tip = int(self.rpc("eth_maxPriorityFeePerGas", []), 16)
        signed = self.Account.sign_transaction({
            "chainId": self.chain_id, "nonce": int(self.rpc("eth_getTransactionCount", [self.address, "pending"]), 16),
            "to": to, "value": value, "data": data, "gas": gas, "maxPriorityFeePerGas": tip, "maxFeePerGas": base_fee * 2 + tip,
            "type": 2}, self.key)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = self.rpc("eth_sendRawTransaction", ["0x" + raw.hex().removeprefix("0x")])
        for _ in range(60):                                   # vent på bekræftelse (højst ca. 2 min.)
            rec = self.rpc("eth_getTransactionReceipt", [h])
            if rec:
                if int(rec["status"], 16) != 1:
                    raise RuntimeError(f"Base-handlen fejlede ({h})")
                return h
            time.sleep(2)
        raise RuntimeError(f"Base-handlen blev ikke bekræftet i tide ({h})")

    def _approve(self, token: str, spender: str, raw: int) -> None:
        allowed = int(self.rpc("eth_call", [{"to": token, "data": "0xdd62ed3e" + _word(self.address) + _word(spender)}, "latest"]), 16)
        if allowed < raw:                                    # giv routeren lov til præcis det beløb, der skal handles
            self._send(token, "0x095ea7b3" + _word(spender) + hex(raw)[2:].rjust(64, "0"))

    def transfer(self, token: str, to: str, amount: float) -> str:
        """Send `amount` af et ERC-20-token til en anden adresse (bruges til at flytte USDC.e til Polymarkets proxy-wallet)."""
        raw = int(amount * 10 ** self.decimals(token))
        return self._send(token, "0xa9059cbb" + _word(to) + hex(raw)[2:].rjust(64, "0"))

    def swap(self, token_in: str, token_out: str, amount: float) -> dict:
        raw = int(amount * 10 ** self.decimals(token_in))
        r = self._http(f"{self.kyber}/routes?tokenIn={token_in}&tokenOut={token_out}&amountIn={raw}").get("data")
        if not r:
            raise RuntimeError("KyberSwap: ingen pris")
        b = self._http(f"{self.kyber}/route/build", {"routeSummary": r["routeSummary"], "sender": self.address, "recipient": self.address,
                                               "slippageTolerance": self.slippage_bps}).get("data")
        if not b:
            raise RuntimeError("KyberSwap: kunne ikke bygge handlen")
        if token_in.lower() != ETH.lower():
            self._approve(token_in, b["routerAddress"], raw)
        h = self._send(b["routerAddress"], b["data"], raw if token_in.lower() == ETH.lower() else 0)
        return {"in": amount, "out": int(b["amountOut"]) / 10 ** self.decimals(token_out), "id": h}

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
                out[t] = self.quote_usd(t, 1.0)
            except Exception:
                pass
        return out

    def open_pos(self, token: str, side: str, usd: float, lev: float, px: float) -> dict:
        r = self.swap(USDC, token, usd)
        return {"qty": r["out"], "usd": r["in"], "id": r["id"]}

    def close_pos(self, token: str, pos: dict, px: float) -> dict:
        qty = min(pos["qty"], self.balance(token))
        r = self.swap(token, USDC, qty)
        return {"qty": r["in"], "usd": r["out"], "id": r["id"]}
