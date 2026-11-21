"""Handelsunivers: S&P 500-konstituenter (fra Wikipedia, cachet) + de største ETF'er."""
from __future__ import annotations

import json
import time
from html.parser import HTMLParser

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# Reserveliste hvis Wikipedia ikke kan nås: store S&P 500-selskaber med GICS-sektor.
FALLBACK_STOCKS = {
    "Information Technology": "AAPL MSFT NVDA AVGO ORCL CRM AMD ADBE CSCO ACN IBM INTU TXN QCOM AMAT NOW MU LRCX ADI KLAC PANW ANET SNPS CDNS",
    "Communication Services": "GOOGL GOOG META NFLX TMUS DIS VZ T CMCSA",
    "Consumer Discretionary": "AMZN TSLA HD MCD LOW BKNG TJX NKE SBUX CMG ORLY",
    "Financials": "BRK-B JPM V MA BAC WFC GS MS AXP BLK SCHW C SPGI PGR CB MMC",
    "Health Care": "LLY UNH JNJ ABBV MRK TMO ABT ISRG DHR AMGN PFE SYK BSX VRTX GILD MDT",
    "Consumer Staples": "WMT PG COST KO PEP PM MDLZ MO CL",
    "Energy": "XOM CVX COP EOG SLB",
    "Industrials": "GE CAT RTX UNP HON BA DE LMT UPS ETN ADP",
    "Materials": "LIN SHW APD FCX",
    "Utilities": "NEE SO DUK",
    "Real Estate": "PLD AMT EQIX",
}


class _ConstituentsParser(HTMLParser):
    """Læser tabellen id="constituents": kolonne 0 = symbol, kolonne 2 = GICS-sektor."""

    def __init__(self):
        super().__init__()
        self.in_table = False
        self.depth = 0
        self.in_cell = False
        self.row: list[str] = []
        self.cell = ""
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and a.get("id") == "constituents":
            self.in_table, self.depth = True, 1
        elif self.in_table and tag == "table":
            self.depth += 1
        elif self.in_table and tag == "tr":
            self.row = []
        elif self.in_table and tag in ("td", "th"):
            self.in_cell, self.cell = True, ""

    def handle_endtag(self, tag):
        if not self.in_table:
            return
        if tag == "table":
            self.depth -= 1
            if self.depth == 0:
                self.in_table = False
        elif tag in ("td", "th") and self.in_cell:
            self.row.append(self.cell.strip())
            self.in_cell = False
        elif tag == "tr" and self.row:
            self.rows.append(self.row)

    def handle_data(self, data):
        if self.in_cell:
            self.cell += data


def parse_wikipedia(html: str) -> dict[str, str]:
    p = _ConstituentsParser()
    p.feed(html)
    out = {}
    for row in p.rows:
        if len(row) >= 3 and row[0] and row[0] != "Symbol":
            out[row[0].replace(".", "-")] = row[2]
    return out


def fallback_universe() -> dict[str, str]:
    return {sym: sector for sector, syms in FALLBACK_STOCKS.items() for sym in syms.split()}


def load_universe(cfg, http=None, force: bool = False) -> dict[str, dict]:
    """{symbol: {"sector": str, "etf": bool}} – cachet i data/cache/universe.json."""
    cache = cfg.CACHE_DIR / "universe.json"
    stocks: dict[str, str] | None = None
    if cache.exists() and not force:
        try:
            data = json.loads(cache.read_text())
            if time.time() - data.get("ts", 0) < cfg.UNIVERSE_REFRESH_DAYS * 86400:
                stocks = data["stocks"]
        except (ValueError, KeyError):
            stocks = None
    if stocks is None and cfg.UNIVERSE_INCLUDE_SP500 and http is not None:
        try:
            parsed = parse_wikipedia(http.get_text(WIKI_URL))
            if len(parsed) > 400:
                stocks = parsed
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({"ts": time.time(), "stocks": stocks}))
        except Exception:  # noqa: BLE001 – falder tilbage til reservelisten
            stocks = None
    if stocks is None:
        if cache.exists():
            try:
                stocks = json.loads(cache.read_text())["stocks"]
            except (ValueError, KeyError):
                stocks = None
        stocks = stocks or fallback_universe()
    uni = {s: {"sector": sec, "etf": False} for s, sec in sorted(stocks.items())}
    if cfg.UNIVERSE_MAX_SYMBOLS:
        uni = dict(list(uni.items())[: cfg.UNIVERSE_MAX_SYMBOLS])
    for e in cfg.TOP_ETFS:
        uni[e] = {"sector": "ETF", "etf": True}
    return uni


def context_symbols(cfg) -> list[str]:
    return [cfg.INDEX_SYMBOL] + sorted(set(cfg.SECTOR_ETFS.values()))
