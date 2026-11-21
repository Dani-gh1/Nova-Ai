"""Offline stand-ins for the data APIs so the whole desk can be tested without network."""
from __future__ import annotations

import time

from desk.messages import Candidate

NOW = time.time()
POOL_ACCT = "PoolVault1111111111111111111111111111111111"


def pair(mint="MINT1", symbol="GOOD", liq=120_000.0, mcap=1_500_000.0, age_h=6.0, vol_h1=250_000.0,
         pc_h1=30.0, pc_m5=4.0, vol_m5=30_000.0, buys=900, sells=500, quote="So11111111111111111111111111111111111111112",
         pair_address=None, price=0.001) -> dict:
    return {
        "chainId": "solana", "dexId": "pumpswap", "pairAddress": pair_address or f"PAIR_{mint}",
        "baseToken": {"address": mint, "symbol": symbol, "name": symbol},
        "quoteToken": {"address": quote, "symbol": "SOL"},
        "priceUsd": str(price),
        "txns": {"h1": {"buys": buys, "sells": sells}, "m5": {"buys": 80, "sells": 40}},
        "volume": {"h1": vol_h1, "m5": vol_m5, "h6": vol_h1 * 4, "h24": vol_h1 * 10},
        "priceChange": {"h1": pc_h1, "m5": pc_m5, "h6": 50.0, "h24": 80.0},
        "liquidity": {"usd": liq},
        "marketCap": mcap, "fdv": mcap,
        "pairCreatedAt": int((NOW - age_h * 3600) * 1000),
    }


def candidate(**kw) -> Candidate:
    from desk.agents.scout import to_candidate
    p = pair(**kw)
    age = (NOW - p["pairCreatedAt"] / 1000) / 3600
    return to_candidate(p, age, p["marketCap"], ("test",))


def rug_report(mint="MINT1", pair_address=None, lp_locked=100.0, top_pcts=(3, 2, 2, 1.5, 1, 1, 1, 1, 1, 1),
               creator_pct=0.0, risks=(), insider_holding=0.0, creator_tokens=(), mint_auth=None,
               freeze_auth=None, supply=1_000_000_000_000_000) -> dict:
    holders = [{"address": POOL_ACCT, "owner": "AMMOWNER", "pct": 40.0, "insider": False}]
    holders += [{"address": f"acct{i}", "owner": f"owner{i}", "pct": float(p), "insider": False}
                for i, p in enumerate(top_pcts)]
    if creator_pct:
        holders.append({"address": "devacct", "owner": "CREATOR", "pct": creator_pct, "insider": False})
    return {
        "mint": mint, "creator": "CREATOR", "creatorBalance": int(supply * creator_pct / 100),
        "token": {"mintAuthority": mint_auth, "freezeAuthority": freeze_auth, "supply": supply, "decimals": 6},
        "topHolders": holders,
        "knownAccounts": {"AMMOWNER": {"name": "Pump Fun AMM", "type": "AMM"}},
        "markets": [{"pubkey": pair_address or f"PAIR_{mint}", "marketType": "pump_fun_amm",
                     "liquidityA": POOL_ACCT, "liquidityB": "QuoteVault",
                     "lp": {"lpLockedPct": lp_locked, "quoteUSD": 60_000, "baseUSD": 60_000}}],
        "risks": [{"name": r, "level": "danger"} for r in risks],
        "insiderNetworks": [{"id": "net", "size": 30, "currentHolding": int(supply * insider_holding / 100),
                             "tokenAmount": int(supply * insider_holding / 100)}] if insider_holding else [],
        "graphInsidersDetected": 30 if insider_holding else 0,
        "creatorTokens": list(creator_tokens),
        "rugged": False,
        "totalHolders": 5000,
    }


def jup_token(mint="MINT1", holder_change_1h=12.0, holder_change_5m=1.5, buy_vol=180_000.0,
              sell_vol=100_000.0, traders=900, decimals=6) -> dict:
    return {
        "id": mint, "decimals": decimals, "holderCount": 5000, "dev": "CREATOR", "organicScore": 70,
        "stats5m": {"holderChange": holder_change_5m},
        "stats1h": {"holderChange": holder_change_1h, "buyVolume": buy_vol, "sellVolume": sell_vol,
                    "numTraders": traders, "numBuys": 900, "numSells": 500, "buyOrganicVolume": 90_000},
        "audit": {"mintAuthorityDisabled": True, "freezeAuthorityDisabled": True, "topHoldersPercentage": 14.5},
    }


def trades(n_real=120, n_dust=0, n_sells=40, minutes=10.0, now=None, smart=(), prefix="w") -> list[dict]:
    now = now or time.time()
    out = []
    total = n_real + n_dust + n_sells + len(smart)
    step = minutes * 60 / max(total, 1)
    i = 0
    for k in range(n_real):
        out.append({"id": f"{prefix}r{k}", "ts": now - i * step, "block": 1, "wallet": f"{prefix}real{k}",
                    "kind": "buy", "usd": 150.0, "tokens": 150_000.0}); i += 1
    for k in range(n_dust):
        out.append({"id": f"{prefix}d{k}", "ts": now - i * step, "block": 1, "wallet": f"{prefix}dust{k}",
                    "kind": "buy", "usd": 0.4, "tokens": 400.0}); i += 1
    for k in range(n_sells):
        out.append({"id": f"{prefix}s{k}", "ts": now - i * step, "block": 1, "wallet": f"{prefix}seller{k}",
                    "kind": "sell", "usd": 120.0, "tokens": 120_000.0}); i += 1
    for w in smart:
        out.append({"id": f"{prefix}sm{w}", "ts": now - i * step, "block": 1, "wallet": w,
                    "kind": "buy", "usd": 500.0, "tokens": 500_000.0}); i += 1
    return out


class FakeHttp:
    stats: dict = {}


class FakeDex:
    def __init__(self, pairs, live_prices=None):
        self.pairs = pairs
        self.live_prices = live_prices      # optional mint -> price dict that the pairs follow

    def discovery_mints(self):
        return {p["baseToken"]["address"]: "dexscreener:test" for p in self.pairs}

    def pairs_for_tokens(self, mints):
        out = []
        for p in self.pairs:
            mint = p["baseToken"]["address"]
            if mint not in mints:
                continue
            if self.live_prices is not None:
                if mint not in self.live_prices:
                    continue
                p = {**p, "priceUsd": str(self.live_prices[mint])}
            out.append(p)
        return out


class FakeRug:
    def __init__(self, reports):
        self.reports = reports

    def report(self, mint):
        return self.reports.get(mint)


class FakeJup:
    """Constant-product-ish quotes around `prices` with a fixed impact."""

    def __init__(self, tokens, prices, impact_pct=0.5):
        self.tokens_ = tokens
        self.price = dict(prices)
        self.impact_pct = impact_pct
        self.impact_fn = None          # optional: usd -> impact %, overrides impact_pct on buys
        self.fail_sells = False

    def discovery_mints(self):
        return {}

    def tokens(self, mints):
        return {m: self.tokens_[m] for m in mints if m in self.tokens_}

    def prices(self, mints):
        return {m: self.price[m] for m in mints if m in self.price}

    def buy_quote(self, mint, usd, slippage_bps):
        dec = self.tokens_[mint]["decimals"]
        impact = self.impact_fn(usd) if self.impact_fn else self.impact_pct
        eff = self.price[mint] * (1 + impact / 100)
        return {"inAmount": str(int(usd * 1e6)), "outAmount": str(int(usd / eff * 10 ** dec)),
                "priceImpactPct": str(impact / 100)}

    def sell_quote(self, mint, raw, slippage_bps):
        if self.fail_sells:
            return None
        dec = self.tokens_[mint]["decimals"]
        eff = self.price[mint] * (1 - self.impact_pct / 100)
        return {"inAmount": str(raw), "outAmount": str(int(raw / 10 ** dec * eff * 1e6)),
                "priceImpactPct": str(self.impact_pct / 100)}


class FakeGecko:
    def __init__(self, trades_by_pool):
        self.trades_by_pool = trades_by_pool

    def discovery(self):
        return {}, {}

    def trades(self, pool):
        return self.trades_by_pool.get(pool, [])


class FakeRPC:
    def __init__(self, infos):
        self.infos = infos

    def mint_info(self, mint):
        return self.infos.get(mint)


class FakeSources:
    def __init__(self, pairs, rugs, jup_tokens, prices, trades_by_pool, rpc_infos=None, impact_pct=0.5):
        self.http = FakeHttp()
        self.rug = FakeRug(rugs)
        self.jup = FakeJup(jup_tokens, prices, impact_pct)
        self.dex = FakeDex(pairs, live_prices=self.jup.price)   # one market: both feeds show the same price
        self.gecko = FakeGecko(trades_by_pool)
        self.rpc = FakeRPC(rpc_infos or {})
