"""Heuristisk matching af samme begivenhed på Polymarket og Kalshi."""
from __future__ import annotations

import re

from . import config

STOP = set("""a an the of on in at by to for and or will be is are does do did before after than
who what which win wins won market yes no vs v over under above below between end by price
2024 2025 2026 2027 2028 this that from with any next""".split())


def tokens(text: str) -> tuple[frozenset, frozenset]:
    words = re.findall(r"[a-z0-9.,$%]+", (text or "").lower())
    nums, toks = set(), set()
    for w in words:
        w = w.strip(".,")
        if not w:
            continue
        if re.fullmatch(r"[$]?\d[\d,.]*[%k]?", w):
            n = w.replace("$", "").replace(",", "")
            if n not in ("2024", "2025", "2026", "2027", "2028"):
                nums.add(n)
        elif w not in STOP and len(w) > 1:
            toks.add(w)
    return frozenset(toks), frozenset(nums)


def similarity(a: str, b: str) -> float:
    ta, na = tokens(a)
    tb, nb = tokens(b)
    if (na or nb) and na != nb:
        return 0.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def match_markets(markets) -> dict[str, dict]:
    """Returnerer {market_key: {"key", "price", "similarity"}} i begge retninger (grådig 1:1)."""
    poly = [m for m in markets if m.platform == "polymarket"]
    kal = [m for m in markets if m.platform == "kalshi"]
    pairs = []
    max_dt = config.MATCH_MAX_CLOSE_DIFF_DAYS * 86400
    for p in poly:
        tp = f"{p.event_title} {p.title}"
        for k in kal:
            if abs(p.close_ts - k.close_ts) > max_dt:
                continue
            s = similarity(tp, f"{k.event_title} {k.title}")
            if s >= config.MATCH_MIN_SIMILARITY:
                pairs.append((s, p, k))
    pairs.sort(key=lambda x: -x[0])
    out: dict[str, dict] = {}
    for s, p, k in pairs:
        if p.key in out or k.key in out:
            continue
        out[p.key] = {"key": k.key, "price": k.yes_price, "similarity": round(s, 3), "title": k.title}
        out[k.key] = {"key": p.key, "price": p.yes_price, "similarity": round(s, 3), "title": p.title}
    return out
