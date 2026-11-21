"""Tests for the fixes from the risk review: exits, marks, Token-2022 checks, rate limits, pruning."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from desk.agents.executor import Executor
from desk.agents.filter import dangerous_extensions, safety_checks
from desk.agents.risk import Risk
from desk.config import AnalystConfig, DeskConfig, ExecutorConfig, FilterConfig, RiskConfig, load_config
from desk.desk import Desk
from desk.messages import ExitSignal
from desk.net import HOST_RPM, RISK_RPM, _retry_after
from desk.smart_wallets import SmartWalletBook
from desk.store import Store

from tests.fakes import candidate, pair, rug_report
from tests.test_executor_desk import passing_plan, sources

PUMP_EXT = {"nonTransferable": False, "transferFeeConfig": None, "defaultAccountState": None,
            "permanentDelegate": None, "metadataPointer": {"authority": None, "metadataAddress": "M"},
            "tokenMetadata": {"authority": None, "name": "X"}, "transferHook": None, "pausableConfig": None}


class TokenExtensionTests(unittest.TestCase):
    def test_plain_pump_token_passes(self):
        self.assertEqual(dangerous_extensions({"token_extensions": PUMP_EXT,
                                               "transferFee": {"pct": 0, "authority": "1111"}}), [])

    def test_dangerous_extensions_are_named(self):
        ext = {**PUMP_EXT, "permanentDelegate": {"delegate": "EVIL"},
               "transferHook": {"authority": "A", "programId": "PROG"}}
        found = dangerous_extensions({"token_extensions": ext})
        self.assertEqual(len(found), 2)
        fee = {**PUMP_EXT, "transferFeeConfig": {"transferFeeConfigAuthority": "A",
                                                 "newerTransferFee": {"transferFeeBasisPoints": 500}}}
        self.assertTrue(dangerous_extensions({"token_extensions": fee})[0].startswith("transfer fee (5%)"))

    def test_filter_kills_and_the_kill_is_structural(self):
        c = candidate(mint="EVIL")
        rug = {**rug_report("EVIL"), "token_extensions": {**PUMP_EXT, "permanentDelegate": {"delegate": "X"}}}
        reasons, codes, _ = safety_checks(c, rug, None, None, FilterConfig())
        self.assertIn("token_extension", codes)


class ExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.src = sources()
        self.risk = Risk(RiskConfig(), AnalystConfig())
        self.ex = Executor(ExecutorConfig(), self.src, self.store, self.risk)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _signal(self, pos, reason="stop_loss"):
        return ExitSignal(position_id=pos.id, mint=pos.mint, symbol=pos.symbol, reason=reason,
                          mark_price=pos.entry_price * 0.9, stop_price=pos.stop_price, ts=time.time())

    def test_no_route_is_not_booked_at_a_made_up_price_within_a_minute(self):
        _, pos = self.ex.enter(passing_plan(self.src))
        self.src.jup.fail_sells = True
        for _ in range(5):
            self.assertIsNone(self.ex.exit(self._signal(pos)))
        self.assertFalse(self.ex.exit_due(pos.id))            # retries are spaced out
        self.assertEqual(len(self.ex.positions), 1)
        # after a minute of failures with a live market price, a modelled price is booked
        fails, first = self.ex.exit_failures[pos.id]
        self.ex.exit_failures[pos.id] = (fails, first - 61)
        sell, closed = self.ex.exit(self._signal(pos))
        self.assertEqual(sell.source, "model-haircut")

    def test_stale_position_is_never_booked_without_a_real_quote(self):
        _, pos = self.ex.enter(passing_plan(self.src))
        self.src.jup.fail_sells = True
        self.ex.exit_failures[pos.id] = (10, time.time() - 3600)
        self.assertIsNone(self.ex.exit(self._signal(pos, reason="stale_price")))
        self.src.jup.fail_sells = False
        sell, closed = self.ex.exit(self._signal(pos, reason="stale_price"))
        self.assertEqual((sell.source, closed.exit_reason), ("jupiter-quote", "stale_price"))


class MarkTests(unittest.TestCase):
    def test_position_is_marked_on_its_own_pool_and_stale_positions_try_to_sell(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = DeskConfig(data_dir=d, smart_wallet_seed_file="/nonexistent")
            src = sources()
            desk = Desk(cfg, "t", echo=False, sources=src)
            desk.scan_cycle()
            pos = desk.executor.open_positions()[0]
            # a second, thin pool quotes a crashed price: it must not trigger the stop
            other = pair(mint="GOOD", symbol="GOOD", pair_address="OTHER", liq=500_000, price=0.0001)
            src.dex.live_prices = None
            src.dex.pairs = [other, {**pair(mint="GOOD", symbol="GOOD"), "priceUsd": "0.00101"}]
            self.assertAlmostEqual(desk._marks([pos])["GOOD"], 0.00101)
            desk.risk_tick()
            self.assertEqual(len(desk.executor.positions), 1)
            # no price anywhere for longer than stale_price_seconds -> sold at a real quote
            src.dex.pairs = []
            with mock.patch.object(src.jup, "prices", return_value={}):
                p = desk.executor.positions[pos.id]
                p.last_price_ts -= cfg.risk.stale_price_seconds + 5
                desk.risk_tick()
            closed = desk.store.query("SELECT exit_reason FROM positions WHERE status='closed'")
            self.assertEqual([r["exit_reason"] for r in closed], ["stale_price"])
            desk.log.close()
            desk.store.close()


class RateLimitTests(unittest.TestCase):
    def test_jupiter_lanes_stay_under_the_keyless_limit(self):
        self.assertLess(HOST_RPM["lite-api.jup.ag"] + RISK_RPM["lite-api.jup.ag"], 30)   # keyless
        self.assertLess(HOST_RPM["api.jup.ag"] + RISK_RPM["api.jup.ag"], 60)             # free key
        self.assertLess(HOST_RPM["api.dexscreener.com"] + RISK_RPM["api.dexscreener.com"], 300)

    def test_retry_after_headers(self):
        self.assertEqual(_retry_after({"Retry-After": "7"}), 7.0)
        self.assertAlmostEqual(_retry_after({"x-ratelimit-reset": str(time.time() + 5)}), 5.0, delta=0.5)
        self.assertIsNone(_retry_after({}))


class PruneAndConfigTests(unittest.TestCase):
    def test_wallet_ledger_is_pruned_after_a_week(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            now = time.time()
            store.executemany("INSERT INTO wallet_token(wallet, mint, first_buy_ts, last_sell_ts) VALUES(?,?,?,?)",
                              [("old", "M", now - 9 * 86400, None), ("new", "M", now - 9 * 86400, now - 3600)])
            SmartWalletBook(store).prune()
            self.assertEqual([r["wallet"] for r in store.query("SELECT wallet FROM wallet_token")], ["new"])
            store.close()

    def test_rules_since_is_a_config_value(self):
        self.assertTrue(load_config().rules_since.startswith("2026-11-15"))


if __name__ == "__main__":
    unittest.main()


class SlowFeedTests(unittest.TestCase):
    def test_jupiter_and_gecko_lists_are_reused_between_refreshes(self):
        from desk.agents.scout import Scout
        from desk.config import ScoutConfig
        src = sources()
        calls = {"jup": 0, "gecko": 0}

        def jup():
            calls["jup"] += 1
            return {"GOOD": "jupiter:test"}

        def gecko():
            calls["gecko"] += 1
            return {"GOOD": "geckoterminal:test"}, {"POOL": "GOOD"}
        src.jup.discovery_mints, src.gecko.discovery = jup, gecko
        scout = Scout(ScoutConfig(), src)
        for _ in range(3):
            rep = scout.scan()
        self.assertEqual(calls, {"jup": 1, "gecko": 1})
        self.assertEqual(rep.trending_pools, ["POOL"])
        scout._slow_at -= 121
        scout.scan()
        self.assertEqual(calls, {"jup": 2, "gecko": 2})


class JupiterKeyFallbackTests(unittest.TestCase):
    def test_a_rejected_key_falls_back_to_the_keyless_api(self):
        import io
        import urllib.error
        from desk import net
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.full_url)
            if req.full_url.startswith("https://api.jup.ag/"):
                raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))
            return io.BytesIO(b'{"ok": 1}')

        old = dict(net._JUPITER_STATE)
        try:
            with mock.patch.object(net.urllib.request, "urlopen", fake_urlopen):
                http = net.Http(retries=1, rpm={"api.jup.ag": 6000, "lite-api.jup.ag": 6000})
                self.assertEqual(http.get("https://api.jup.ag/price/v3", {"ids": "X"}), {"ok": 1})
                self.assertTrue(net.jupiter_key_rejected())
                http.get("https://api.jup.ag/price/v3", {"ids": "Y"})       # straight to keyless now
            self.assertEqual([u.split("/")[2] for u in seen], ["api.jup.ag", "lite-api.jup.ag", "lite-api.jup.ag"])
        finally:
            net._JUPITER_STATE.update(old)


class NotifyErrorTests(unittest.TestCase):
    def test_a_failed_notification_reaches_the_log(self):
        from desk.notify import Notifier
        errors = []
        n = Notifier("t", "http://127.0.0.1:9", timeout=0.5)
        n.on_error = errors.append
        n.send("x", "y")
        n.flush(5)
        self.assertEqual(len(errors), 1)
        self.assertNotIn("t/", errors[0])
