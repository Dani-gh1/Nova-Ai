"""Filter: dræber par med for stor spread, for tynd orderbog eller et
pludseligt udsving på over FILTER_MAX_MOVE_1H_PCT på 1 time."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..models import Approved, Book, Candidate


class Filter:
    def __init__(self, cfg, market, logger=None):
        self.cfg, self.market, self.log = cfg, market, logger

    def check_book(self, book: Book) -> tuple[bool, str, float, float, float]:
        cfg = self.cfg
        spread = book.spread_pct()
        bid = book.depth_quote("bid", cfg.FILTER_DEPTH_BAND_PCT)
        ask = book.depth_quote("ask", cfg.FILTER_DEPTH_BAND_PCT)
        if spread > cfg.FILTER_MAX_SPREAD_PCT:
            return False, f"spread {spread:.3f}% > {cfg.FILTER_MAX_SPREAD_PCT}%", spread, bid, ask
        if min(bid, ask) < cfg.FILTER_MIN_DEPTH_USDT:
            return False, "tynd orderbog", spread, bid, ask
        return True, "", spread, bid, ask

    def move_1h_pct(self, symbol: str) -> float:
        """Største udsving (%) inden for den seneste time, målt på 5m-lys."""
        kl = self.market.klines(symbol, "5m", limit=12)
        if not kl:
            return 0.0
        hi = max(k.high for k in kl)
        lo = min(k.low for k in kl)
        first = kl[0].open
        last = kl[-1].close
        return max(abs(last / first - 1), (hi - lo) / lo) * 100

    def _one(self, c: Candidate):
        if not isinstance(c, Candidate):
            raise TypeError("Filter tager kun imod Candidate fra Scout")
        try:
            book = self.market.depth(c.symbol, self.cfg.DEPTH_LIMIT)
            ok, why, spread, bid, ask = self.check_book(book)
            if ok:
                move = self.move_1h_pct(c.symbol)
                if move > self.cfg.FILTER_MAX_MOVE_1H_PCT:
                    ok, why = False, f"udsving {move:.1f}% på 1t"
        except Exception as e:
            return c, None, f"datafejl: {e}"
        if not ok:
            return c, None, why
        return c, Approved(candidate=c, spread_pct=spread, depth_bid=bid, depth_ask=ask, book=book), ""

    def run(self, candidates: list[Candidate]) -> list[Approved]:
        for c in candidates:
            if not isinstance(c, Candidate):
                raise TypeError("Filter tager kun imod Candidate fra Scout")
        with ThreadPoolExecutor(max_workers=getattr(self.cfg, "HTTP_WORKERS", 4)) as pool:
            results = list(pool.map(self._one, candidates))
        out = []
        for c, approved, why in results:
            if approved:
                out.append(approved)
            elif self.log:
                self.log.event("filter", "rejected", symbol=c.symbol, reason=why)
        if self.log:
            self.log.event("filter", "approved", n=len(out), symbols=[a.symbol for a in out])
        return out
