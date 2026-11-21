"""Thin wrappers over the public on-chain data APIs the desk reads.

DexScreener  - pair age, market cap, liquidity, volume, price change, txn counts
RugCheck     - LP lock/burn, holder distribution, creator history, insider networks
Jupiter      - holder counts and holder change, buy/sell flow, audit, prices, swap quotes
GeckoTerminal- individual trades (wallet-level) for unique buyers and smart wallets
Solana RPC   - mint/freeze authority straight from the mint account
"""
from __future__ import annotations

import time
from datetime import datetime

from .config import USDC_MINT
from .net import JUPITER_HOST, Http, HttpError


def parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        # RugCheck emits nanosecond precision, which fromisoformat rejects on some Pythons.
        head, _, tail = ts.partition(".")
        tz = "+00:00" if tail.endswith("Z") else ""
        return datetime.fromisoformat(head + tz).timestamp()


def chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


class DexScreener:
    BASE = "https://api.dexscreener.com"

    def __init__(self, http: Http):
        self.http = http

    def discovery_mints(self) -> dict[str, str]:
        """Solana mints from DexScreener's public feeds -> feed name."""
        out: dict[str, str] = {}
        for path in ("token-profiles/latest/v1", "token-boosts/latest/v1",
                     "token-boosts/top/v1", "community-takeovers/latest/v1"):
            try:
                rows = self.http.get(f"{self.BASE}/{path}") or []
            except HttpError:
                continue
            for row in rows:
                if row.get("chainId") == "solana" and row.get("tokenAddress"):
                    out.setdefault(row["tokenAddress"], f"dexscreener:{path.split('/')[0]}")
        return out

    def pairs_for_tokens(self, mints: list[str]) -> list[dict]:
        pairs: list[dict] = []
        for group in chunks(mints, 30):
            try:
                rows = self.http.get(f"{self.BASE}/tokens/v1/solana/{','.join(group)}") or []
            except HttpError:
                continue
            pairs.extend(p for p in rows if p.get("chainId") == "solana")
        return pairs


class RugCheck:
    BASE = "https://api.rugcheck.xyz/v1"

    def __init__(self, http: Http, cache_seconds: float = 300.0):
        self.http = http
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, dict]] = {}

    def report(self, mint: str) -> dict | None:
        hit = self._cache.get(mint)
        if hit and time.time() - hit[0] < self.cache_seconds:
            return hit[1]
        try:
            rep = self.http.get(f"{self.BASE}/tokens/{mint}/report")
        except HttpError:
            return None
        if isinstance(rep, dict) and rep.get("mint"):
            self._cache[mint] = (time.time(), rep)
            return rep
        return None


class Jupiter:
    # lite-api.jup.ag without a key (30 requests/min), api.jup.ag with JUPITER_API_KEY set.
    TOKENS = f"https://{JUPITER_HOST}/tokens/v2"
    PRICE = f"https://{JUPITER_HOST}/price/v3"
    QUOTE = f"https://{JUPITER_HOST}/swap/v1/quote"

    def __init__(self, http: Http):
        self.http = http

    def discovery_mints(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in ("toptrending/1h", "toptraded/1h", "toporganicscore/1h", "toptrending/5m",
                     "toptraded/24h", "recent"):
            try:
                rows = self.http.get(f"{self.TOKENS}/{path}", {"limit": 100}) or []
            except HttpError:
                continue
            for row in rows:
                if row.get("id"):
                    out.setdefault(row["id"], f"jupiter:{path}")
        return out

    def tokens(self, mints: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for group in chunks(mints, 100):
            try:
                rows = self.http.get(f"{self.TOKENS}/search", {"query": ",".join(group)}) or []
            except HttpError:
                continue
            for row in rows:
                out[row["id"]] = row
        return out

    def prices(self, mints: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for group in chunks(mints, 50):
            try:
                rows = self.http.get(self.PRICE, {"ids": ",".join(group)}) or {}
            except HttpError:
                continue
            for mint, row in rows.items():
                if row and row.get("usdPrice"):
                    out[mint] = float(row["usdPrice"])
        return out

    def quote(self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int) -> dict | None:
        try:
            q = self.http.get(self.QUOTE, {
                "inputMint": input_mint, "outputMint": output_mint,
                "amount": str(int(amount_raw)), "slippageBps": slippage_bps,
            })
        except HttpError:
            return None
        if not isinstance(q, dict) or q.get("error") or not q.get("outAmount"):
            return None
        return q

    def buy_quote(self, mint: str, usd: float, slippage_bps: int) -> dict | None:
        return self.quote(USDC_MINT, mint, int(usd * 1e6), slippage_bps)

    def sell_quote(self, mint: str, raw_amount: int, slippage_bps: int) -> dict | None:
        return self.quote(mint, USDC_MINT, raw_amount, slippage_bps)


class GeckoTerminal:
    BASE = "https://api.geckoterminal.com/api/v2/networks/solana"

    def __init__(self, http: Http):
        self.http = http

    def _pools(self, path: str, params: dict | None = None) -> list[dict]:
        try:
            data = self.http.get(f"{self.BASE}/{path}", params) or {}
        except HttpError:
            return []
        return data.get("data") or []

    def discovery(self) -> tuple[dict[str, str], dict[str, str]]:
        """(mint -> feed, trending pool address -> base mint)."""
        mints: dict[str, str] = {}
        trending: dict[str, str] = {}
        for path, params in (("trending_pools", {"duration": "1h"}),
                             ("trending_pools", {"duration": "6h"}),
                             ("new_pools", None),
                             ("pools", {"sort": "h24_volume_usd_desc", "page": 1}),
                             ("pools", {"sort": "h24_volume_usd_desc", "page": 2})):
            for pool in self._pools(path, params):
                try:
                    mint = pool["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
                except (KeyError, IndexError, AttributeError):
                    continue
                mints.setdefault(mint, f"geckoterminal:{path}")
                if path == "trending_pools" and (pool.get("attributes") or {}).get("address"):
                    trending.setdefault(pool["attributes"]["address"], mint)
        return mints, trending

    def trades(self, pool: str) -> list[dict] | None:
        """Last <=300 swaps on the pool (past 24h), newest first, normalised."""
        try:
            data = self.http.get(f"{self.BASE}/pools/{pool}/trades") or {}
        except HttpError:
            return None
        out = []
        for row in data.get("data") or []:
            a = row.get("attributes") or {}
            kind = a.get("kind")
            if kind not in ("buy", "sell"):
                continue
            token_amt = a.get("to_token_amount") if kind == "buy" else a.get("from_token_amount")
            out.append({
                "id": row.get("id"),
                "ts": parse_iso(a.get("block_timestamp")) or 0.0,
                "block": a.get("block_number"),
                "wallet": a.get("tx_from_address"),
                "kind": kind,
                "usd": float(a.get("volume_in_usd") or 0.0),
                "tokens": float(token_amt or 0.0),
            })
        return out


class SolanaRPC:
    def __init__(self, http: Http, url: str = "https://api.mainnet-beta.solana.com"):
        self.http = http
        self.url = url

    def mint_info(self, mint: str) -> dict | None:
        try:
            res = self.http.post(self.url, {
                "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                "params": [mint, {"encoding": "jsonParsed"}],
            })
        except HttpError:
            return None
        try:
            info = res["result"]["value"]["data"]["parsed"]["info"]
        except (KeyError, TypeError):
            return None
        return {
            "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            "supply": int(info.get("supply") or 0),
            "decimals": int(info.get("decimals") or 0),
        }


class Sources:
    """Bundle of all clients, shared by the agents."""

    def __init__(self, http: Http | None = None):
        self.http = http or Http()
        self.dex = DexScreener(self.http)
        self.rug = RugCheck(self.http)
        self.jup = Jupiter(self.http)
        self.gecko = GeckoTerminal(self.http)
        self.rpc = SolanaRPC(self.http)
