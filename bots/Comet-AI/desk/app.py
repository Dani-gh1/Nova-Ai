"""Orkestrator: flytter overdragelsesobjekter mellem agenterne (hårde overdragelser)
og kører de tre løkker: scouting, kursovervågning (1 s) og husholdning."""
from __future__ import annotations

import logging
import signal
import threading
import time
from collections import defaultdict

import config
from desk import notify, risk, sources, state
from desk.analyst import Analyst
from desk.executor import Executor
from desk.filter import Filter
from desk.logger import Logger
from desk.models import ExitOrder, FilteredCandidate, Kill, ScoredCandidate, TradePlan
from desk.scout import Scout

log = logging.getLogger("desk")


class Desk:
    def __init__(self, logger: Logger | None = None, price_feed=None, gecko_price_feed=None):
        self.logger = logger or Logger()
        self.logger.ensure_accounts()
        self.scout = Scout(self.logger)
        self.filter = Filter(self.logger)
        self.analyst = Analyst(self.logger)
        self.executor = Executor(self.logger)
        self.price_feed = price_feed or sources.ds_pair_prices
        self.gecko_price_feed = gecko_price_feed or sources.gecko_pools_multi
        self.stop = threading.Event()
        self.stats = defaultdict(int)
        self.started_at = time.time()
        self.last_scout: dict[str, float] = {}
        self.last_price_tick = 0.0
        self._rot: dict[str, int] = defaultdict(int)
        self._last_gecko_fallback = 0.0
        self._last_eq = 0.0
        self._last_sum = time.time()

    # ================================================================ pipeline
    def process(self, c) -> ScoredCandidate | Kill:
        L = self.logger
        L.handoff("scout", "filter", c.chain, c.pair_address, c.to_dict())
        self.stats["kandidater"] += 1
        r = self.filter.handle(c)
        if isinstance(r, Kill):
            L.kill(c, r)
            self.stats["dræbt"] += 1
            return r
        assert isinstance(r, FilteredCandidate)
        L.handoff("filter", "analyst", c.chain, c.pair_address, r.security)
        sc = self.analyst.handle(r)
        L.score(sc)
        L.handoff("analyst", "risk", c.chain, c.pair_address, {"score": sc.score, "components": sc.components})
        self.stats["scoret"] += 1

        f = self.executor.open_shadow(sc)
        if f.ok:
            self.stats["skygger"] += 1

        for acct in L.accounts():
            if acct["chain"] != c.chain:
                continue
            st = config.strategy_by_key(acct["strategy_key"])
            p = risk.plan(sc, acct, st)
            if isinstance(p, Kill):
                continue
            assert isinstance(p, TradePlan)
            L.handoff("risk", "executor", c.chain, c.pair_address,
                      {"strategy": st.key, "account": acct["id"], "stop_loss": p.stop_loss,
                       "trailing": p.trailing_stop, "take_profit": p.take_profit})
            fill = self.executor.open(p)
            L.handoff("executor", "logger", c.chain, c.pair_address,
                      {"strategy": st.key, "ok": fill.ok, "reason": fill.reason, "impact": fill.impact})
            if fill.ok:
                self.stats["køb"] += 1
                L.event("info", "køb", f"{st.name} [{config.CHAINS[c.chain].name}] køber {c.symbol} "
                                       f"(score {sc.score:.0f}, prisvirkning {fill.impact * 100:.2f} %)".replace(".", ","))
                if config.NTFY_NOTIFY_TRADES:
                    notify.send(f"Papirkøb {c.symbol}", f"{st.name} på {config.CHAINS[c.chain].name}: "
                                f"score {sc.score:.0f}, {st.position_pct:.0%} af kontoen")
        return sc

    def scout_once(self, chain: str):
        for c in self.scout.run(chain):
            if self.stop.is_set():
                return
            try:
                self.process(c)
            except Exception as e:
                log.exception("pipeline-fejl %s", c.pair_address)
                self.logger.event("error", "pipeline", f"{c.chain} {c.symbol}: {e}")
        self.last_scout[chain] = time.time()

    # ================================================================ kursovervågning
    def _fetch_prices(self, positions) -> dict[tuple, dict]:
        by_chain = defaultdict(set)
        for p in positions:
            by_chain[p["chain"]].add(p["pair"])
        prices = {}
        missing = defaultdict(list)
        for chain, pairs in by_chain.items():
            pairs = sorted(pairs)
            # højst ét DexScreener-kald (30 par) pr. kæde pr. sekund; rotér hvis flere
            off = self._rot[chain] % len(pairs)
            chunk = (pairs[off:] + pairs[:off])[:30]
            self._rot[chain] += 30
            got = self.price_feed(chain, chunk)
            for pair in chunk:
                if pair in got and got[pair]["price"] > 0:
                    prices[(chain, pair)] = got[pair]
                else:
                    missing[chain].append(pair)
        if missing and time.time() - self._last_gecko_fallback > 30:
            self._last_gecko_fallback = time.time()
            for chain, pairs in missing.items():
                for pair, v in self.gecko_price_feed(chain, pairs).items():
                    if v["price"] > 0:
                        prices[(chain, pair)] = v
        return prices

    def risk_tick(self, now: float | None = None):
        now = now or time.time()
        L = self.logger
        positions = L.open_positions()
        if not positions:
            return
        prices = self._fetch_prices(positions)
        for pos in positions:
            q = prices.get((pos["chain"], pos["pair"]))
            if q:
                price, liq = q["price"], q["liquidity"]
                peak = max(pos["peak_price"] or 0, price)
                trough = min(pos["trough_price"] or price, price)
                pos.update(peak_price=peak)
                ex = risk.check(pos, price, liq, now)
                if not ex:
                    if price != pos["last_price"] or liq != pos["last_liq"]:
                        L.update_position(pos["id"], last_price=price, last_liq=liq, last_price_ts=now,
                                          peak_price=peak, trough_price=trough)
                    continue
                L.update_position(pos["id"], peak_price=peak, trough_price=trough)
            elif now - (pos["last_price_ts"] or now) > config.PRICE_STALE_AFTER_S and \
                    now - pos["opened_at"] >= pos["max_hold_h"] * 3600:
                ex = ExitOrder(pos["id"], "maks. holdetid (gammel kurs)", pos["last_price"], pos["last_liq"] or 0)
            else:
                continue
            L.handoff("risk", "executor", pos["chain"], pos["pair"], ex)
            fill = self.executor.close(ex, now)
            if not fill.ok:
                continue
            closed = L.position(pos["id"])
            if ex.reason.startswith("rug"):
                L.mark_dev_rug(pos["chain"], pos["creator"], pos["token"], ex.reason)
            if pos["shadow"]:
                self.stats["skygger lukket"] += 1
                continue
            self.stats["salg"] += 1
            st = config.strategy_by_key(pos["strategy_key"])
            msg = (f"{st.name} [{config.CHAINS[pos['chain']].name}] sælger {pos['symbol']}: "
                   f"{closed['pnl_pct']:+.1f} % ({ex.reason})".replace(".", ","))
            L.event("info", "salg", msg)
            if config.NTFY_NOTIFY_TRADES:
                notify.send(f"Papirsalg {pos['symbol']} {closed['pnl_pct']:+.1f} %".replace(".", ","), msg,
                            "high" if ex.reason.startswith("rug") else "default",
                            "warning" if closed["pnl_pct"] < 0 else "moneybag")

    # ================================================================ løkker
    def _loop(self, name, fn, interval):
        while not self.stop.is_set():
            t0 = time.monotonic()
            try:
                fn()
            except Exception as e:
                log.exception("%s-løkke fejlede", name)
                try:
                    self.logger.event("error", name, str(e))
                except Exception:
                    pass
                self.stop.wait(5)
            self.stop.wait(max(0.0, interval - (time.monotonic() - t0)))

    def _scout_all(self):
        for ch in config.ENABLED_CHAINS:
            if self.stop.is_set():
                return
            self.scout_once(ch)

    def _risk(self):
        self.risk_tick()
        self.last_price_tick = time.time()

    def state_dict(self) -> dict:
        L = self.logger
        return {"epoch": L.epoch(), "started_at": self.started_at, "chains": list(config.ENABLED_CHAINS),
                "last_scout": self.last_scout, "last_price_tick": self.last_price_tick,
                "open_positions": len(L.open_positions(shadow=False)),
                "open_shadows": len(L.open_positions(shadow=True)), "stats": dict(self.stats)}

    def _housekeeping(self):
        now = time.time()
        if now - self._last_eq >= config.EQUITY_SNAPSHOT_EVERY_S:
            self._last_eq = now
            self.logger.snapshot_equity(now)
        state.write(self.state_dict())
        if config.NTFY_TOPIC and now - self._last_sum >= config.NTFY_SUMMARY_EVERY_S:
            self._last_sum = now
            from desk.dashboard import strategies_payload
            rows = sorted(strategies_payload(self.logger)["per_strategy"], key=lambda r: -r["return_pct"])[:3]
            notify.send("Desk-opsummering", "Bedste strategier: " +
                        ", ".join(f"{r['name']} {r['return_pct']:+.1f}%" for r in rows), "low", "bar_chart")

    def run(self, with_dashboard: bool = True):
        L = self.logger
        L.event("info", "start", f"Desk startet (epoch {L.epoch()}, kæder {', '.join(config.ENABLED_CHAINS)})")
        notify.send("Papirdesk startet", f"Epoch {L.epoch()} - kun papirhandel", "low", "rocket")
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        threads = [
            threading.Thread(target=self._loop, args=("scout", self._scout_all, config.SCOUT_INTERVAL_S), daemon=True),
            threading.Thread(target=self._loop, args=("risk", self._risk, config.PRICE_CHECK_INTERVAL_S), daemon=True),
            threading.Thread(target=self._loop, args=("hus", self._housekeeping, config.STATE_WRITE_EVERY_S), daemon=True),
        ]
        server = None
        if with_dashboard:
            from desk.dashboard import make_server
            server = make_server(Logger(L.path) if L.path != ":memory:" else L, desk=self)
            threads.append(threading.Thread(target=server.serve_forever, daemon=True))
        for t in threads:
            t.start()
        log.info("Desk kører. Dashboard: http://%s:%d", config.DASHBOARD_HOST, config.DASHBOARD_PORT)
        while not self.stop.is_set():
            self.stop.wait(1)
        if server:
            server.shutdown()
        state.write({**self.state_dict(), "stopped": True})
        L.event("info", "stop", "Desk stoppet")
