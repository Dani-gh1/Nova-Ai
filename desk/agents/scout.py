"""Scout: finder handlebare USDT-par med 24t-volumen over grænsen."""
from __future__ import annotations

from ..models import Candidate


class Scout:
    def __init__(self, cfg, market, logger=None):
        self.cfg, self.market, self.log = cfg, market, logger

    def select(self, tickers: list[dict]) -> list[Candidate]:
        cfg = self.cfg
        rows = []
        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith(cfg.QUOTE):
                continue
            base = sym[: -len(cfg.QUOTE)]
            if base in cfg.SCOUT_EXCLUDE_BASES or sym.endswith(cfg.SCOUT_EXCLUDE_SUFFIXES):
                continue
            qv = float(t.get("quoteVolume", 0) or 0)
            last = float(t.get("lastPrice", 0) or 0)
            if qv < cfg.SCOUT_MIN_QUOTE_VOLUME_24H or last <= 0:
                continue
            # Binance markerer delistede/pausede par med count=0
            if int(t.get("count", 1) or 0) == 0:
                continue
            rows.append((qv, sym, last))
        rows.sort(reverse=True)
        rows = rows[: cfg.SCOUT_MAX_SYMBOLS]
        return [Candidate(symbol=s, quote_volume=qv, last_price=p, rank=i + 1)
                for i, (qv, s, p) in enumerate(rows)]

    def run(self) -> list[Candidate]:
        out = self.select(self.market.ticker_24h())
        if self.log:
            self.log.event("scout", "candidates", n=len(out), symbols=[c.symbol for c in out])
        return out
