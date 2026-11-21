"""Gratis offentlige datakilder: DexScreener, GeckoTerminal og GoPlus.

Kun læsning. Parse-funktionerne er rene (ingen netværk) så de kan testes.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import config
from desk import http
from desk.models import Candidate, SecurityReport

log = logging.getLogger("desk.sources")

DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
GP = "https://api.gopluslabs.io/api/v1"


def _f(x, default=0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _iso(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def dex_allowed(chain_key: str, dex_id: str) -> bool:
    dex_id = (dex_id or "").lower()
    return any(dex_id.startswith(p) for p in config.CHAINS[chain_key].allowed_dexes)


# ------------------------------------------------------------ GeckoTerminal
def parse_gecko_pool(chain_key: str, item: dict, now: float | None = None) -> Candidate | None:
    a = item.get("attributes", {})
    rel = item.get("relationships", {})
    try:
        token = rel["base_token"]["data"]["id"].split("_", 1)[1]
        dex = rel.get("dex", {}).get("data", {}).get("id", "")
        created = _iso(a["pool_created_at"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    name = a.get("name", "")
    parts = name.split(" / ")
    symbol = parts[0].strip() if parts else ""
    quote = parts[1].split(" ")[0].strip() if len(parts) > 1 else ""
    tx = (a.get("transactions") or {}).get("h1") or {}
    pc = a.get("price_change_percentage") or {}
    vol = a.get("volume_usd") or {}
    mcap = _f(a.get("market_cap_usd")) or _f(a.get("fdv_usd"))
    return Candidate(
        chain=chain_key, pair_address=a.get("address", "").lower(), token_address=token.lower(),
        symbol=symbol, name=symbol, dex=dex, quote_symbol=quote, created_at=created,
        price_usd=_f(a.get("base_token_price_usd")), liquidity_usd=_f(a.get("reserve_in_usd")),
        market_cap_usd=mcap, volume_h1=_f(vol.get("h1")), volume_h24=_f(vol.get("h24")),
        price_change_m5=_f(pc.get("m5")), price_change_h1=_f(pc.get("h1")), price_change_h6=_f(pc.get("h6")),
        buys_h1=int(_f(tx.get("buys"))), sells_h1=int(_f(tx.get("sells"))),
        buyers_h1=int(_f(tx.get("buyers"))), sellers_h1=int(_f(tx.get("sellers"))),
        source="geckoterminal", seen_at=now or time.time())


def gecko_new_pools(chain_key: str, pages: int) -> list[Candidate]:
    net = config.CHAINS[chain_key].gecko_id
    out = []
    for kind, n in (("new_pools?", pages), ("trending_pools?duration=24h&", config.SCOUT_GECKO_TRENDING_PAGES),
                    ("pools?sort=h24_volume_usd_desc&", config.SCOUT_GECKO_TOP_PAGES)):
        for page in range(1, n + 1):
            try:
                d = http.get_json(f"{GT}/networks/{net}/{kind}page={page}")
            except http.HttpError as e:
                log.warning("GeckoTerminal %s %s: %s", chain_key, kind, e)
                break
            for item in d.get("data", []):
                c = parse_gecko_pool(chain_key, item)
                if c:
                    out.append(c)
    return out


def gecko_pools_multi(chain_key: str, pools: list[str]) -> dict[str, dict]:
    """Kurs og likviditet for op til 30 puljer (reserve til DexScreener)."""
    net = config.CHAINS[chain_key].gecko_id
    out = {}
    for i in range(0, len(pools), 30):
        chunk = ",".join(pools[i:i + 30])
        try:
            d = http.get_json(f"{GT}/networks/{net}/pools/multi/{chunk}", retries=1)
        except http.HttpError:
            continue
        for item in d.get("data", []):
            a = item.get("attributes", {})
            out[a.get("address", "").lower()] = {
                "price": _f(a.get("base_token_price_usd")), "liquidity": _f(a.get("reserve_in_usd"))}
    return out


def gecko_ohlcv(chain_key: str, pool: str, timeframe="minute", aggregate=1, limit=1000,
                before_ts: int | None = None) -> list[list[float]]:
    """[[ts, o, h, l, c, v], ...] stigende i tid."""
    net = config.CHAINS[chain_key].gecko_id
    url = f"{GT}/networks/{net}/pools/{pool}/ohlcv/{timeframe}?aggregate={aggregate}&limit={limit}&currency=usd"
    if before_ts:
        url += f"&before_timestamp={before_ts}"
    d = http.get_json(url)
    rows = d.get("data", {}).get("attributes", {}).get("ohlcv_list", []) or []
    return sorted(([float(v) for v in r] for r in rows), key=lambda r: r[0])


# ------------------------------------------------------------ DexScreener
def parse_ds_pair(chain_key: str, p: dict, now: float | None = None) -> Candidate | None:
    try:
        created = _f(p.get("pairCreatedAt")) / 1000.0
        base = p["baseToken"]
    except (KeyError, TypeError):
        return None
    tx = (p.get("txns") or {}).get("h1") or {}
    vol = p.get("volume") or {}
    pc = p.get("priceChange") or {}
    return Candidate(
        chain=chain_key, pair_address=str(p.get("pairAddress", "")).lower(),
        token_address=str(base.get("address", "")).lower(), symbol=base.get("symbol", ""),
        name=base.get("name", ""), dex=p.get("dexId", ""),
        quote_symbol=(p.get("quoteToken") or {}).get("symbol", ""), created_at=created,
        price_usd=_f(p.get("priceUsd")), liquidity_usd=_f((p.get("liquidity") or {}).get("usd")),
        market_cap_usd=_f(p.get("marketCap")) or _f(p.get("fdv")),
        volume_h1=_f(vol.get("h1")), volume_h24=_f(vol.get("h24")),
        price_change_m5=_f(pc.get("m5")), price_change_h1=_f(pc.get("h1")), price_change_h6=_f(pc.get("h6")),
        buys_h1=int(_f(tx.get("buys"))), sells_h1=int(_f(tx.get("sells"))),
        source="dexscreener", seen_at=now or time.time())


def ds_tokens(chain_key: str, tokens: list[str]) -> dict[str, list[dict]]:
    """token -> liste af rå DexScreener-par."""
    cid = config.CHAINS[chain_key].dexscreener_id
    out: dict[str, list[dict]] = {}
    for i in range(0, len(tokens), 30):
        try:
            d = http.get_json(f"{DS}/tokens/v1/{cid}/{','.join(tokens[i:i + 30])}")
        except http.HttpError as e:
            log.warning("DexScreener tokens %s: %s", chain_key, e)
            continue
        for p in d if isinstance(d, list) else []:
            out.setdefault(str(p.get("baseToken", {}).get("address", "")).lower(), []).append(p)
    return out


def ds_latest_profiles(chain_key: str) -> list[str]:
    """Token-adresser fra DexScreeners seneste profiler/boosts på kæden."""
    cid = config.CHAINS[chain_key].dexscreener_id
    toks = []
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        try:
            d = http.get_json(DS + path, retries=1)
        except http.HttpError:
            continue
        for t in d if isinstance(d, list) else []:
            if t.get("chainId") == cid and t.get("tokenAddress"):
                toks.append(t["tokenAddress"].lower())
    return list(dict.fromkeys(toks))


def ds_pair_prices(chain_key: str, pairs: list[str]) -> dict[str, dict]:
    cid = config.CHAINS[chain_key].dexscreener_id
    out = {}
    for i in range(0, len(pairs), 30):
        try:
            d = http.get_json(f"{DS}/latest/dex/pairs/{cid}/{','.join(pairs[i:i + 30])}", retries=1, timeout=8)
        except http.HttpError:
            continue
        for p in (d.get("pairs") or []) if isinstance(d, dict) else []:
            out[str(p.get("pairAddress", "")).lower()] = {
                "price": _f(p.get("priceUsd")), "liquidity": _f((p.get("liquidity") or {}).get("usd"))}
    return out


def merge(gt: Candidate, ds: Candidate | None) -> Candidate:
    """DexScreener har ofte bedre likviditet/markedsværdi; GeckoTerminal har unikke købere."""
    if not ds:
        return gt
    for f in ("liquidity_usd", "market_cap_usd", "volume_h1", "volume_h24", "price_usd"):
        if getattr(ds, f):
            setattr(gt, f, getattr(ds, f))
    if ds.name:
        gt.name = ds.name
    gt.source = "geckoterminal+dexscreener"
    return gt


# ------------------------------------------------------------ GoPlus
def _flag(d: dict, k: str) -> bool:
    return str(d.get(k, "0")).strip() == "1"


def parse_goplus(chain_key: str, pair_address: str, d: dict | None) -> SecurityReport:
    if not d:
        return SecurityReport(ok=False, raw_error="ingen data fra GoPlus")
    dead = config.DEAD_ADDRESSES
    owner = str(d.get("owner_address", "") or "").lower()
    renounced = owner == "" or owner in dead
    if _flag(d, "can_take_back_ownership") or _flag(d, "hidden_owner"):
        renounced = False

    lp = d.get("lp_holders") or []
    lp_safe = 0.0
    for h in lp:
        addr = str(h.get("address", "")).lower()
        tag = str(h.get("tag", "")).lower()
        if str(h.get("is_locked", 0)) == "1" or addr in dead or "dead" in tag or "burn" in tag or "lock" in tag:
            lp_safe += _f(h.get("percent"))
    lp_safe = min(1.0, lp_safe) if lp else -1.0     # -1 = ukendt

    excluded = set(dead) | {pair_address.lower()} | {a.lower() for a in config.EXCLUDED_HOLDERS.get(chain_key, ())}
    excluded |= {str(h.get("address", "")).lower() for h in lp}
    shares = []
    for h in d.get("holders") or []:
        addr = str(h.get("address", "")).lower()
        if addr in excluded or str(h.get("is_locked", 0)) == "1":
            continue
        if str(h.get("is_contract", 0)) == "1" and h.get("tag"):
            continue        # navngivne kontrakter (DEX, bro, locker)
        shares.append(_f(h.get("percent")))
    shares.sort(reverse=True)

    def tax(k):
        v = d.get(k)
        return None if v in (None, "") else _f(v)

    return SecurityReport(
        ok=True, is_honeypot=_flag(d, "is_honeypot"), buy_tax=tax("buy_tax"), sell_tax=tax("sell_tax"),
        owner_renounced=renounced, lp_locked_or_burned=lp_safe, top10_share=sum(shares[:10]),
        max_single_holder=shares[0] if shares else 0.0, holder_count=int(_f(d.get("holder_count"))),
        creator=str(d.get("creator_address", "") or "").lower(),
        flags=[k for k in config.FILTER_KILL_FLAGS if _flag(d, k)])


def creator_flagged(r: dict) -> bool:
    if any(str(r.get(k, "0")) == "1" for k in config.FILTER_CREATOR_BAD_KEYS):
        return True
    return _f(r.get("number_of_malicious_contracts_created")) > config.FILTER_CREATOR_MAX_MALICIOUS_CONTRACTS


def goplus_token(chain_key: str, token: str) -> dict | None:
    cid = config.CHAINS[chain_key].goplus_id
    try:
        d = http.get_json(f"{GP}/token_security/{cid}?contract_addresses={token}")
    except http.HttpError as e:
        log.warning("GoPlus %s: %s", token, e)
        return None
    if d.get("code") != 1:
        return None
    res = d.get("result") or {}
    return res.get(token.lower()) or (next(iter(res.values())) if res else None)


def goplus_creator_flagged(chain_key: str, address: str) -> bool:
    if not address:
        return False
    cid = config.CHAINS[chain_key].goplus_id
    try:
        d = http.get_json(f"{GP}/address_security/{address}?chain_id={cid}", retries=1)
    except http.HttpError:
        return False
    return creator_flagged(d.get("result") or {})
