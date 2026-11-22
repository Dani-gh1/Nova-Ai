import io
import json
import unittest

from hub.live import positions_for
from hub.polymarket import PolymarketWallet

KEY = "0x" + "11" * 32                       # en test-nøgle, aldrig en rigtig


class PolymarketTests(unittest.TestCase):
    def wallet(self, answers):
        def opener(req):
            for part, body in answers.items():
                if part in req.full_url:
                    return io.BytesIO(json.dumps(body).encode())
            raise AssertionError(req.full_url)
        return PolymarketWallet(KEY, funder="", opener=opener)

    def test_yes_is_the_first_outcome_and_no_the_second_like_delphi(self):
        w = self.wallet({"/markets/7245975": {"outcomes": '["Knicks", "Pacers"]', "clobTokenIds": '["111", "222"]'}})
        self.assertEqual(w.token("polymarket:7245975|YES"), "111")
        self.assertEqual(w.token("polymarket:7245975|NO"), "222")       # sport: ingen "Yes"/"No", men samme rækkefølge
        self.assertEqual(w.token("333"), "333")                           # et rent token gives uændret

    def test_balance_reads_usdc_and_positions(self):
        w = self.wallet({"publicnode": {"result": hex(12_500_000)}, "/v2/positions": {"data": [{"currentValue": 7.5}]}})
        v = w.value_usd()
        self.assertEqual((v["usdc"], v["positions_usd"], v["usd"]), (12.5, 7.5, 20.0))

    def test_only_polymarket_positions_are_followed(self):
        data = {"trades": {"open": [
            {"strategy": "Balanceret", "symbol": "Knicks?", "side": "NO", "market": "polymarket:7245975|NO", "platform": "polymarket"},
            {"strategy": "Balanceret", "symbol": "Fed?", "side": "YES", "market": "kalshi:FED-25|YES", "platform": "kalshi"}]}}
        self.assertEqual(positions_for({"id": "delphi:balanced", "name": "Balanceret"}, data), [("polymarket:7245975|NO", "long", 1.0)])


if __name__ == "__main__":
    unittest.main()
