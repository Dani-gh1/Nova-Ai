"""Filter: kill anything unsafe or illiquid. Every rule is evaluated and every failed rule is
reported, so each Kill carries the complete list of reasons and the measured values.

Tier 1 (market data, from the Scout's DexScreener snapshot - free):
  liquidity < $40k, 1h volume < $50k, already +80% in 1h
Tier 2 (contract + holder safety, only for tier-1 survivors):
  mint / freeze authority not revoked      Solana RPC getAccountInfo (fallback RugCheck/Jupiter)
  LP not burned or locked on traded pair   RugCheck markets[].lp.lpLockedPct
  top 10 holders > 25% (pools excluded)    RugCheck topHolders minus AMM/locker accounts
  dev > 3% of supply                       RugCheck creatorBalance / creator in top holders
  dev past rugs                            RugCheck rug-history risk, rugged flag, dead launches
  bundled launch > 10%                     RugCheck insider networks + insider-flagged holders
  dangerous Token-2022 extension           RugCheck token_extensions / transferFee: a permanent
                                           delegate (can take tokens from any wallet), a transfer
                                           hook, transfer fees, pausable or non-transferable
"""
from __future__ import annotations

import time

from ..config import FilterConfig
from ..messages import Candidate, FilterPass, Kill
from ..sources import Sources, parse_iso

STRUCTURAL = {"mint_authority", "freeze_authority", "lp_unlocked", "dev_rugs", "bundled", "token_extension"}


def market_checks(c: Candidate, cfg: FilterConfig) -> tuple[list[str], dict]:
    reasons: list[str] = []
    liq = c.liquidity_usd
    vol1h = c.volume.get("h1", 0.0)
    pc1h = c.price_change.get("h1", 0.0)
    metrics = {"liquidity_usd": round(liq, 2), "volume_1h_usd": round(vol1h, 2),
               "price_change_1h_pct": pc1h, "mcap_usd": round(c.mcap_usd, 2),
               "pair_age_h": round(c.pair_age_hours, 2)}
    if liq < cfg.min_liquidity_usd:
        reasons.append(f"low_liquidity: ${liq:,.0f} < ${cfg.min_liquidity_usd:,.0f}")
    if vol1h < cfg.min_volume_1h_usd:
        reasons.append(f"low_volume_1h: ${vol1h:,.0f} < ${cfg.min_volume_1h_usd:,.0f}")
    if pc1h > cfg.max_price_change_1h_pct:
        reasons.append(f"already_pumped: +{pc1h:.1f}% in 1h > +{cfg.max_price_change_1h_pct:.0f}%")
    return reasons, metrics


def _excluded_accounts(rug: dict) -> set[str]:
    excluded = {addr for addr, meta in (rug.get("knownAccounts") or {}).items()
                if (meta or {}).get("type") in ("AMM", "LOCKER")}
    for m in rug.get("markets") or []:
        for key in ("pubkey", "liquidityA", "liquidityB"):
            if m.get(key):
                excluded.add(m[key])
    return excluded


def traded_market(rug: dict, pair_address: str) -> tuple[dict | None, bool]:
    """RugCheck market for the pair we'd trade. (market, exact_match)."""
    markets = rug.get("markets") or []
    for m in markets:
        if m.get("pubkey") == pair_address:
            return m, True
    if not markets:
        return None, False

    def depth(m: dict) -> float:
        lp = m.get("lp") or {}
        return float(lp.get("quoteUSD") or 0) + float(lp.get("baseUSD") or 0)
    return max(markets, key=depth), False


def dangerous_extensions(rug: dict) -> list[str]:
    """Token-2022 extensions that let someone take, block or tax a holder's tokens. Plain
    pump.fun tokens use Token-2022 too, but only with metadata extensions, which pass."""
    ext = rug.get("token_extensions") or {}
    out = []
    if (ext.get("permanentDelegate") or {}).get("delegate"):
        out.append("permanent delegate (can move tokens out of any wallet)")
    if (ext.get("transferHook") or {}).get("programId"):
        out.append("transfer hook (a program can block sales)")
    if ext.get("nonTransferable"):
        out.append("non-transferable")
    if ext.get("pausableConfig"):
        out.append("pausable transfers")
    if ext.get("defaultAccountState") and "frozen" in str(ext["defaultAccountState"]).lower():
        out.append("new accounts start frozen")
    fee_cfg = ext.get("transferFeeConfig") or {}
    fee_pct = float((rug.get("transferFee") or {}).get("pct") or 0)
    bps = [float((fee_cfg.get(k) or {}).get("transferFeeBasisPoints") or 0)
           for k in ("olderTransferFee", "newerTransferFee")]
    if fee_pct > 0 or any(b > 0 for b in bps):
        out.append(f"transfer fee ({max([fee_pct] + [b / 100 for b in bps]):g}%)")
    elif fee_cfg.get("transferFeeConfigAuthority"):
        out.append("transfer fee can be switched on")
    return out


def safety_checks(c: Candidate, rug: dict | None, mint_info: dict | None, jup: dict | None,
                  cfg: FilterConfig, now: float | None = None) -> tuple[list[str], list[str], dict]:
    """Returns (reasons, reason_codes, metrics)."""
    now = now or time.time()
    reasons: list[str] = []
    codes: list[str] = []
    metrics: dict = {}
    audit = (jup or {}).get("audit") or {}

    def fail(code: str, text: str) -> None:
        codes.append(code)
        reasons.append(f"{code}: {text}")

    # --- authorities (on-chain first) ------------------------------------
    if mint_info is not None:
        mint_auth, freeze_auth, src = mint_info["mint_authority"], mint_info["freeze_authority"], "rpc"
    elif rug is not None and rug.get("token"):
        mint_auth, freeze_auth, src = rug["token"].get("mintAuthority"), rug["token"].get("freezeAuthority"), "rugcheck"
    elif audit:
        mint_auth = None if audit.get("mintAuthorityDisabled") else "enabled"
        freeze_auth = None if audit.get("freezeAuthorityDisabled") else "enabled"
        src = "jupiter"
    else:
        mint_auth = freeze_auth = src = None
    metrics["authority_source"] = src
    if src is None:
        fail("data_unavailable", "could not read mint/freeze authority")
    else:
        metrics["mint_authority"] = mint_auth
        metrics["freeze_authority"] = freeze_auth
        if mint_auth:
            fail("mint_authority", f"not revoked ({mint_auth[:8]}…)")
        if freeze_auth:
            fail("freeze_authority", f"not revoked ({freeze_auth[:8]}…)")

    if rug is None:
        fail("data_unavailable", "RugCheck report unavailable (LP/holders/dev/bundles unverified)")
        return reasons, codes, metrics

    # --- Token-2022 extensions that can trap or take a holder's tokens ----
    bad_ext = dangerous_extensions(rug)
    metrics["token_extensions"] = bad_ext
    if bad_ext:
        fail("token_extension", ", ".join(bad_ext))

    supply = float((rug.get("token") or {}).get("supply") or 0) or (mint_info or {}).get("supply") or 0
    holders = rug.get("topHolders") or []

    # --- LP burned / locked on the traded pair ---------------------------
    market, exact = traded_market(rug, c.pair_address)
    if market is None:
        fail("lp_unlocked", "no market found to verify LP lock")
    else:
        lp_pct = float((market.get("lp") or {}).get("lpLockedPct") or 0.0)
        metrics.update(lp_locked_pct=round(lp_pct, 2), lp_market_type=market.get("marketType"),
                       lp_market_exact=exact)
        if lp_pct < cfg.min_lp_locked_pct:
            fail("lp_unlocked", f"{lp_pct:.1f}% burned/locked on {market.get('marketType')} "
                                f"< {cfg.min_lp_locked_pct:.0f}%")

    # --- top 10 holders (pools and lockers excluded) ---------------------
    excluded = _excluded_accounts(rug)
    real = [h for h in holders if h.get("owner") not in excluded and h.get("address") not in excluded]
    if real:
        top10 = sum(float(h.get("pct") or 0) for h in real[:10])
        metrics["top10_source"] = "rugcheck"
    elif audit.get("topHoldersPercentage") is not None:
        top10 = float(audit["topHoldersPercentage"])
        metrics["top10_source"] = "jupiter"
    else:
        top10 = None
    metrics["top10_pct"] = None if top10 is None else round(top10, 2)
    if top10 is None:
        fail("data_unavailable", "no holder distribution")
    elif top10 > cfg.max_top10_pct:
        fail("top10_concentration", f"{top10:.1f}% > {cfg.max_top10_pct:.0f}%")

    # --- dev holdings -----------------------------------------------------
    creator = rug.get("creator")
    dev_wallets = {w for w in (creator, (jup or {}).get("dev")) if w}
    dev_pct = (float(rug.get("creatorBalance") or 0) / supply * 100.0) if supply else 0.0
    for h in holders:
        if h.get("owner") in dev_wallets:
            dev_pct = max(dev_pct, float(h.get("pct") or 0))
    metrics["dev_pct"] = round(dev_pct, 3)
    metrics["creator"] = creator
    if dev_pct > cfg.max_dev_pct:
        fail("dev_holding", f"dev holds {dev_pct:.1f}% > {cfg.max_dev_pct:.0f}%")

    # --- dev past rugs ----------------------------------------------------
    rug_risks = [r.get("name", "") for r in rug.get("risks") or [] if "rug" in (r.get("name") or "").lower()]
    dead = []
    for t in rug.get("creatorTokens") or []:
        if t.get("mint") == c.mint:
            continue
        created = parse_iso(t.get("createdAt"))
        if created and now - created > 6 * 3600 and float(t.get("marketCap") or 0) < 10_000:
            dead.append(t.get("mint"))
    metrics["dev_rug_flags"] = rug_risks
    metrics["dev_dead_launches"] = len(dead)
    metrics["rugged_flag"] = bool(rug.get("rugged"))
    if rug_risks:
        fail("dev_rugs", "; ".join(rug_risks))
    if rug.get("rugged"):
        fail("dev_rugs", "token itself flagged rugged")
    if len(dead) >= cfg.max_dev_dead_launches:
        fail("dev_rugs", f"creator has {len(dead)} dead prior launches")

    # --- bundled launch / linked-wallet cluster --------------------------
    nets = rug.get("insiderNetworks") or []
    cluster = sum(float(n.get("currentHolding") or 0) for n in nets)
    cluster_pct = min(100.0, cluster / supply * 100.0) if supply else 0.0
    insider_pct = sum(float(h.get("pct") or 0) for h in holders if h.get("insider"))
    bundle_pct = max(cluster_pct, insider_pct)
    metrics.update(bundle_pct=round(bundle_pct, 2), insider_networks=len(nets),
                   insider_wallets=int(rug.get("graphInsidersDetected") or 0))
    if bundle_pct > cfg.max_bundle_pct:
        fail("bundled", f"{bundle_pct:.1f}% of supply held by bundled/linked wallets "
                        f"({len(nets)} networks) > {cfg.max_bundle_pct:.0f}%")

    metrics["holders"] = rug.get("totalHolders")
    return reasons, codes, metrics


class Filter:
    name = "filter"

    def __init__(self, cfg: FilterConfig, sources: Sources):
        self.cfg = cfg
        self.src = sources

    def _kill(self, c: Candidate, reasons: list[str], metrics: dict, structural: bool) -> Kill:
        minutes = self.cfg.recheck_structural_minutes if structural else self.cfg.recheck_dynamic_minutes
        return Kill(stage="filter", mint=c.mint, symbol=c.symbol, reasons=tuple(reasons),
                    metrics=metrics, recheck_after_s=minutes * 60.0, candidate_id=c.id)

    def evaluate(self, candidates: list[Candidate]) -> list[FilterPass | Kill]:
        results: list[FilterPass | Kill] = []
        tier2: list[tuple[Candidate, dict]] = []
        for c in candidates:
            reasons, metrics = market_checks(c, self.cfg)
            if reasons:
                metrics["tier2"] = "skipped (failed market checks)"
                results.append(self._kill(c, reasons, metrics, structural=False))
            else:
                tier2.append((c, metrics))
        if not tier2:
            return results

        jup = self.src.jup.tokens([c.mint for c, _ in tier2])
        for c, metrics in tier2:
            rug = self.src.rug.report(c.mint)
            mint_info = self.src.rpc.mint_info(c.mint)
            reasons, codes, safety = safety_checks(c, rug, mint_info, jup.get(c.mint), self.cfg)
            metrics.update(safety)
            if reasons:
                results.append(self._kill(c, reasons, metrics, structural=bool(STRUCTURAL & set(codes))))
            else:
                results.append(FilterPass(candidate=c, checks=metrics, jupiter=jup.get(c.mint)))
        return results
