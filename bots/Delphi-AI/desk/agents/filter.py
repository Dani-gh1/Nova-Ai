"""Filter: dræber markeder med bred spread, tynd orderbog, uklare regler eller for kort tid."""
from __future__ import annotations

import time

from .. import config
from ..models import Candidate, Tradable, Market, Book


class Filter:
    name = "filter"

    def __init__(self, book_fetcher=None):
        if book_fetcher is None:
            from .. import sources
            book_fetcher = lambda m: sources.get(m.platform).get_book(m)  # noqa: E731
        self.book_fetcher = book_fetcher

    @staticmethod
    def precheck(m: Market, now: float) -> str | None:
        if m.hours_to_close(now) < config.FILTER_MIN_HOURS_TO_RESOLUTION:
            return "for kort tid til afgørelse"
        if m.resolving:
            return "afgørelse allerede foreslået"
        rules = (m.rules or "").strip()
        if len(rules) < config.FILTER_MIN_RULES_CHARS:
            return "uklare regler (for kort regeltekst)"
        low = rules.lower()
        for phrase in config.FILTER_UNCLEAR_RULE_PHRASES:
            if phrase in low:
                return f"uklare regler ('{phrase}')"
        if not (config.FILTER_PRICE_MIN <= m.yes_price <= config.FILTER_PRICE_MAX):
            return "pris for tæt på 0 eller 100 %"
        if m.spread is not None and m.spread > config.FILTER_MAX_SPREAD:
            return "bred spread (kurs)"
        return None

    @staticmethod
    def book_check(book: Book) -> str | None:
        if book.spread is None:
            return "tom orderbog"
        if book.spread > config.FILTER_MAX_SPREAD + 1e-9:
            return "bred spread (orderbog)"
        band = config.FILTER_DEPTH_BAND
        if min(book.depth_usd("bid", band), book.depth_usd("ask", band)) < config.FILTER_MIN_DEPTH_USD:
            return "tynd orderbog"
        mid = book.mid
        if not (config.FILTER_PRICE_MIN <= mid <= config.FILTER_PRICE_MAX):
            return "pris for tæt på 0 eller 100 %"
        return None

    def run(self, candidates: list[Candidate], now: float | None = None):
        now = now or time.time()
        kills: list[tuple[str, str]] = []
        pre = []
        for c in candidates:
            why = self.precheck(c.market, now)
            if why:
                kills.append((c.market.key, why))
            else:
                pre.append(c.market)
        pre.sort(key=lambda m: -m.volume_24h)
        out: list[Tradable] = []
        for i, m in enumerate(pre):
            if i >= config.FILTER_MAX_BOOKS:
                kills.append((m.key, "uden for dagens orderbogs-budget"))
                continue
            try:
                book = self.book_fetcher(m)
            except Exception as e:
                kills.append((m.key, f"orderbog utilgængelig: {e}"[:200]))
                continue
            why = self.book_check(book)
            if why:
                kills.append((m.key, why))
                continue
            m.best_bid, m.best_ask, m.yes_price = book.best_bid, book.best_ask, book.mid
            out.append(Tradable(m, book))
        return out, kills
