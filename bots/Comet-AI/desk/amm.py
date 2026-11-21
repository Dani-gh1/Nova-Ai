"""Constant-product (x*y=k) tilnærmelse af prisvirkning ud fra poolens likviditet.

Likviditet i USD regnes som to lige store sider; R = liquidity_usd / 2 er
modvaluta-siden. For koncentreret likviditet (v3/v4/CL) er dette en
konservativ-ish tilnærmelse - det er papirhandel, ikke et orakel.
"""
from __future__ import annotations

import config


def reserve(liquidity_usd: float) -> float:
    return max(liquidity_usd, 0.0) / 2.0


def max_size_for_impact(liquidity_usd: float, max_impact: float) -> float:
    """Største køb (USD) hvor gennemsnitlig prisvirkning <= max_impact."""
    r = reserve(liquidity_usd)
    if r <= 0 or max_impact <= 0:
        return 0.0
    return r * max_impact          # avg-pris = p*(1 + x/R) => x/R <= impact


def buy_fill(price: float, size_usd: float, liquidity_usd: float) -> tuple[float, float]:
    """(gennemsnitlig fyldkurs, prisvirkning) ved køb for size_usd."""
    r = reserve(liquidity_usd)
    impact = size_usd / r if r > 0 else 1.0
    return price * (1 + impact), impact


def sell_proceeds(price: float, tokens: float, liquidity_usd: float) -> tuple[float, float]:
    """(brutto-provenu USD, prisvirkning) ved salg af tokens."""
    r = reserve(liquidity_usd)
    x = tokens * price
    if r <= 0 or x <= 0:
        return 0.0, 1.0
    gross = x * r / (r + x)
    return gross, 1 - r / (r + x)


def dex_fee(dex: str) -> float:
    d = (dex or "").lower()
    for k, v in config.DEX_FEE.items():
        if d.startswith(k):
            return v
    return config.DEX_FEE["default"]
