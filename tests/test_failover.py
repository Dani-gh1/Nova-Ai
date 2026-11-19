"""FailoverFeed: skifter selv børs, når den ene er spærret (HTTP 451/403)."""
import unittest
from unittest import mock

from desk import config, feeds
from desk.net import HttpError

BLOCKED = HttpError("GET https://x fejlede: HTTP Error 451: ")


class Stub:
    def __init__(self, name, error=None):
        self.name, self.error, self.calls, self.liqs = name, error, 0, None

    def __call__(self, start_ws=True):
        return self

    def ticker(self):
        self.calls += 1
        if self.error:
            raise self.error
        return ({self.name: 1.0}, {})

    def universe(self):
        return self.ticker()


def make(binance_err=None, bybit_err=None, ping=None):
    stubs = {"binance": Stub("binance", binance_err), "bybit": Stub("bybit", bybit_err)}
    ping = ping or (lambda url, params=None, timeout=None: (_ for _ in ()).throw(BLOCKED) if "binance" in url and binance_err else {})
    with mock.patch.object(feeds, "get_json", ping):
        return feeds.FailoverFeed(factory=stubs, start_ws=False), stubs, ping


class FailoverTests(unittest.TestCase):
    def setUp(self):
        self._ex = config.EXCHANGE
        self.addCleanup(lambda: setattr(config, "EXCHANGE", self._ex))

    def test_uses_binance_when_it_answers(self):
        f, stubs, _ = make()
        self.assertEqual(f.active, "binance")
        self.assertEqual(f.ticker()[0], {"binance": 1.0})
        self.assertEqual(config.EXCHANGE, "binance")

    def test_falls_back_to_bybit_when_binance_is_blocked_at_start(self):
        f, stubs, _ = make(binance_err=BLOCKED)
        self.assertEqual(f.active, "bybit")
        self.assertEqual(config.EXCHANGE, "bybit")             # dashboardet viser den børs, der bruges
        self.assertEqual(f.ticker()[0], {"bybit": 1.0})

    def test_switches_when_the_active_exchange_becomes_blocked_later(self):
        f, stubs, ping = make()
        stubs["binance"].error = BLOCKED
        with mock.patch.object(feeds, "get_json", lambda url, params=None, timeout=None: (_ for _ in ()).throw(BLOCKED) if "binance" in url else {}):
            self.assertEqual(f.ticker()[0], {"bybit": 1.0})    # samme kald lykkes, via Bybit
        self.assertEqual(f.active, "bybit")

    def test_a_few_ordinary_failures_do_not_switch_but_repeated_ones_do(self):
        boom = HttpError("GET https://x fejlede: timed out")
        f, stubs, _ = make()
        stubs["binance"].error = boom
        for _ in range(2):
            with self.assertRaises(HttpError):
                f.ticker()
        self.assertEqual(f.active, "binance")
        with mock.patch.object(feeds, "get_json", lambda *a, **k: {}):
            self.assertEqual(f.ticker()[0], {"bybit": 1.0})    # tredje fejl i træk: skift
        self.assertEqual(f.active, "bybit")

    def test_when_nothing_answers_it_raises_a_clear_error_and_keeps_trying(self):
        def dead(url, params=None, timeout=None):
            raise BLOCKED
        stubs = {"binance": Stub("binance", BLOCKED), "bybit": Stub("bybit", BLOCKED)}
        with mock.patch.object(feeds, "get_json", dead):
            f = feeds.FailoverFeed(factory=stubs, start_ws=False)
            self.assertIsNone(f.active)
            with self.assertRaises(HttpError) as e:
                f.ticker()
        self.assertIn("Ingen børs svarer", str(e.exception))
        with mock.patch.object(feeds, "get_json", lambda *a, **k: {}):          # børsen kommer tilbage
            f.bad.clear()
            for s in stubs.values():
                s.error = None
            self.assertEqual(f.ticker()[0], {"binance": 1.0})

    def test_make_feed_respects_a_fixed_choice_and_defaults_to_auto(self):
        self.assertEqual(config.__dict__["EXCHANGE"] in ("auto", "binance", "bybit"), True)
        with mock.patch.object(config, "EXCHANGE", "bybit"):
            self.assertIsInstance(feeds.make_feed(start_ws=False), feeds.BybitFeed)
        with mock.patch.object(config, "EXCHANGE", "binance"):
            self.assertIsInstance(feeds.make_feed(start_ws=False), feeds.BinanceFeed)
        with mock.patch.object(feeds, "get_json", lambda *a, **k: {}), mock.patch.object(config, "EXCHANGE", "auto"):
            self.assertIsInstance(feeds.make_feed(start_ws=False), feeds.FailoverFeed)


if __name__ == "__main__":
    unittest.main()
