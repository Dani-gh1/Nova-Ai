import unittest

from tests.helpers import config, goplus_raw
from desk import sources


class GoPlusParseTest(unittest.TestCase):
    def test_safe_token(self):
        r = sources.parse_goplus("base", "0xpair", goplus_raw())
        self.assertTrue(r.ok and r.owner_renounced)
        self.assertAlmostEqual(r.lp_locked_or_burned, 0.98)
        self.assertAlmostEqual(r.top10_share, 0.08)       # pair-adressen tæller ikke
        self.assertAlmostEqual(r.max_single_holder, 0.05)
        self.assertEqual(r.flags, [])

    def test_honeypot_and_flags(self):
        r = sources.parse_goplus("base", "0xpair", goplus_raw(is_honeypot="1", hidden_owner="1"))
        self.assertTrue(r.is_honeypot)
        self.assertIn("hidden_owner", r.flags)
        self.assertFalse(r.owner_renounced)

    def test_owner_and_unknown_tax_and_lp(self):
        r = sources.parse_goplus("base", "0xpair", goplus_raw(owner_address="0x1234", buy_tax="", lp_holders=[]))
        self.assertFalse(r.owner_renounced)
        self.assertIsNone(r.buy_tax)
        self.assertEqual(r.lp_locked_or_burned, -1.0)

    def test_none(self):
        self.assertFalse(sources.parse_goplus("base", "0xpair", None).ok)


class MarketParseTest(unittest.TestCase):
    def test_gecko_pool(self):
        item = {"attributes": {"address": "0xABC", "name": "FOO / WETH 0.3%", "pool_created_at": "2026-11-18T10:00:00Z",
                               "base_token_price_usd": "0.5", "reserve_in_usd": "50000", "market_cap_usd": None,
                               "fdv_usd": "900000", "price_change_percentage": {"m5": "1", "h1": "12", "h6": "30"},
                               "transactions": {"h1": {"buys": 50, "sells": 20, "buyers": 40, "sellers": 15}},
                               "volume_usd": {"h1": "10000", "h24": "90000"}},
                "relationships": {"base_token": {"data": {"id": "base_0xTOK"}}, "dex": {"data": {"id": "aerodrome-base"}}}}
        c = sources.parse_gecko_pool("base", item)
        self.assertEqual((c.pair_address, c.token_address, c.symbol, c.quote_symbol), ("0xabc", "0xtok", "FOO", "WETH"))
        self.assertEqual(c.market_cap_usd, 900000)        # fdv som reserve
        self.assertEqual(c.buyers_h1, 40)
        self.assertTrue(sources.dex_allowed("base", c.dex))
        self.assertFalse(sources.dex_allowed("bsc", "four-meme"))

    def test_dexscreener_pair(self):
        p = {"pairAddress": "0xP", "dexId": "pancakeswap", "baseToken": {"address": "0xT", "symbol": "B", "name": "Bee"},
             "quoteToken": {"symbol": "WBNB"}, "priceUsd": "0.01", "liquidity": {"usd": 70000}, "marketCap": 300000,
             "pairCreatedAt": 1795000000000, "txns": {"h1": {"buys": 10, "sells": 5}}, "volume": {"h1": 1000, "h24": 9000},
             "priceChange": {"h1": 5}}
        c = sources.parse_ds_pair("bsc", p)
        self.assertEqual(c.created_at, 1795000000)
        self.assertEqual(c.liquidity_usd, 70000)
        merged = sources.merge(sources.parse_ds_pair("bsc", p), None)
        self.assertEqual(merged.symbol, "B")


if __name__ == "__main__":
    unittest.main()
