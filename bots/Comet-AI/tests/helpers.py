import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DESK_DATA_DIR", tempfile.mkdtemp(prefix="desk-test-"))

import config  # noqa: E402
from desk.models import Candidate, SecurityReport  # noqa: E402


def cand(**kw) -> Candidate:
    now = kw.pop("now", time.time())
    d = dict(chain="base", pair_address="0xpair", token_address="0xtoken", symbol="TST", name="Test",
             dex="uniswap-v2-base", quote_symbol="WETH", created_at=now - 5 * 3600, price_usd=0.001,
             liquidity_usd=80_000, market_cap_usd=400_000, volume_h1=30_000, volume_h24=200_000,
             price_change_m5=2, price_change_h1=15, price_change_h6=40, buys_h1=200, sells_h1=100,
             buyers_h1=90, sellers_h1=40, seen_at=now)
    d.update(kw)
    return Candidate(**d)


def safe_sec(**kw) -> SecurityReport:
    d = dict(ok=True, is_honeypot=False, buy_tax=0.0, sell_tax=0.0, owner_renounced=True,
             lp_locked_or_burned=1.0, top10_share=0.15, max_single_holder=0.03, holder_count=400,
             creator="0xdev", flags=[])
    d.update(kw)
    return SecurityReport(**d)


def goplus_raw(**kw) -> dict:
    d = {"is_honeypot": "0", "buy_tax": "0", "sell_tax": "0", "owner_address": "", "creator_address": "0xdev",
         "holder_count": "400",
         "lp_holders": [{"address": "0x000000000000000000000000000000000000dead", "percent": "0.98", "is_locked": 0}],
         "holders": [{"address": "0xpair", "percent": "0.4", "is_contract": 1, "tag": ""},
                     {"address": "0xaaa", "percent": "0.05", "is_contract": 0, "tag": ""},
                     {"address": "0xbbb", "percent": "0.03", "is_contract": 0, "tag": ""}]}
    d.update(kw)
    return d
