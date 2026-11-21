"""Filter: dræber kontrakter med tynd orderbog, for stor spread eller for høj volatilitet.

Input: list[Candidate]   Output: (list[FilteredCandidate], list[(Candidate, grund)])
"""
from __future__ import annotations

from .. import config
from ..indicators import atr_pct, max_move_pct
from ..models import Candidate, FilteredCandidate


class Filter:
    name = "filter"

    def __init__(self, feed):
        self.feed = feed

    def run(self, candidates: list[Candidate]):
        passed: list[FilteredCandidate] = []
        killed: list[tuple[Candidate, str]] = []
        for c in candidates:
            sym = c.snapshot.symbol
            try:
                book = self.feed.orderbook(sym)
                klines = self.feed.klines(sym, config.ANALYST_KLINE_LIMIT)
            except Exception as e:  # noqa: BLE001
                killed.append((c, f"data mangler: {e}"))
                continue
            reason = self.check(book, klines)
            if reason:
                killed.append((c, reason))
                continue
            passed.append(FilteredCandidate(
                snapshot=c.snapshot, reasons=c.reasons, book=book, klines=klines,
                spread_bps=book.spread_bps,
                depth_bid_usd=book.depth_usd("bid", config.FILTER_DEPTH_BAND),
                depth_ask_usd=book.depth_usd("ask", config.FILTER_DEPTH_BAND),
                atr_pct=atr_pct(klines, 24)))
        return passed, killed

    @staticmethod
    def check(book, klines) -> str | None:
        if not book.bids or not book.asks:
            return "tom orderbog"
        if book.spread_bps > config.FILTER_MAX_SPREAD_BPS:
            return f"spread {book.spread_bps:.1f} bps > {config.FILTER_MAX_SPREAD_BPS}"
        depth = min(book.depth_usd("bid", config.FILTER_DEPTH_BAND),
                    book.depth_usd("ask", config.FILTER_DEPTH_BAND))
        if depth < config.FILTER_MIN_DEPTH_USD:
            return f"tynd bog: {depth / 1e3:.0f}k USD < {config.FILTER_MIN_DEPTH_USD / 1e3:.0f}k"
        if len(klines) < 50:
            return "for lidt kurshistorik"
        a = atr_pct(klines, 24)
        if a > config.FILTER_MAX_VOL_1H:
            return f"volatilitet ATR {a * 100:.2f}% > {config.FILTER_MAX_VOL_1H * 100:.2f}%"
        m = max_move_pct(klines, 24)
        if m > config.FILTER_MAX_MOVE_1H:
            return f"enkeltbevægelse {m * 100:.1f}% > {config.FILTER_MAX_MOVE_1H * 100:.1f}%"
        return None
