"""Smart-wallet book.

There is no free, trustworthy list of "smart money" on Solana, so the desk builds its own
from wallet-level trades it observes (GeckoTerminal swaps on every pool it analyses plus a
couple of trending pools per cycle). A wallet becomes smart when, across distinct tokens,
it has repeatedly round-tripped for real profit:

  per (wallet, token): bought >= $50, sold >= 50% of what it bought, matched realised PnL
  >= +$50 and >= +30% ROI, <= 40 swaps, and held >= 60s (filters MEV / sniper bots)
  smart: >= 2 winning tokens and a win rate >= 60%

You can also seed wallets you already trust in data/smart_wallets.txt (one per line).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .store import Store

MIN_BUY_USD = 50.0
MIN_SOLD_FRACTION = 0.5
MIN_PROFIT_USD = 50.0
MIN_ROI = 0.30
MAX_SWAPS = 40
MIN_HOLD_S = 60.0
MIN_WINS = 2
MIN_WIN_RATE = 0.6


def classify(row: dict) -> str | None:
    """'win' | 'loss' | None (not a completed, bot-free round trip)."""
    buy_usd, sell_usd = row["buy_usd"], row["sell_usd"]
    buy_tok, sell_tok = row["buy_tokens"], row["sell_tokens"]
    if buy_usd < MIN_BUY_USD or buy_tok <= 0 or sell_tok <= 0:
        return None
    if row["n_trades"] > MAX_SWAPS:
        return None
    if row["first_buy_ts"] is None or row["last_sell_ts"] is None:
        return None
    if row["last_sell_ts"] - row["first_buy_ts"] < MIN_HOLD_S:
        return None
    sold_frac = min(1.0, sell_tok / buy_tok)
    if sold_frac < MIN_SOLD_FRACTION:
        return None
    # Match only the tokens we saw bought against the tokens we saw sold.
    matched_cost = buy_usd * sold_frac
    matched_proceeds = sell_usd * min(1.0, buy_tok / sell_tok)
    pnl = matched_proceeds - matched_cost
    roi = pnl / matched_cost if matched_cost else 0.0
    if pnl >= MIN_PROFIT_USD and roi >= MIN_ROI:
        return "win"
    if pnl < 0:
        return "loss"
    return None


class SmartWalletBook:
    def __init__(self, store: Store, seed_file: str | None = None):
        self.store = store
        self.smart: set[str] = {r["wallet"] for r in store.query("SELECT wallet FROM smart_wallets")}
        if seed_file and Path(seed_file).exists():
            seeds = []
            for line in Path(seed_file).read_text().splitlines():
                w = line.split("#", 1)[0].strip()
                if w:
                    seeds.append(w)
            now = time.time()
            store.executemany(
                "INSERT OR IGNORE INTO smart_wallets(wallet, source, wins, losses, realized_usd, added_ts, updated_ts) "
                "VALUES(?, 'seed', 0, 0, 0, ?, ?)", [(w, now, now) for w in seeds])
            self.smart.update(seeds)

    def size(self) -> int:
        return len(self.smart)

    def is_smart(self, wallet: str | None) -> bool:
        return bool(wallet) and wallet in self.smart

    def ingest(self, mint: str, trades: list[dict]) -> list[str]:
        """Fold new trades into the per-wallet ledger. Returns wallets newly marked smart.

        Dedupe uses a per-mint watermark (newest timestamp seen + the trade ids at that
        timestamp), so storage stays O(coins) no matter how long the desk runs."""
        trades = [t for t in trades if t.get("id") and t.get("wallet") and t.get("ts")]
        if not trades:
            return []
        mark = self.store.query("SELECT ts, ids FROM trade_marks WHERE mint=?", (mint,))
        mark_ts, mark_ids = (mark[0]["ts"], set(json.loads(mark[0]["ids"]))) if mark else (0.0, set())
        fresh = [t for t in trades if t["ts"] > mark_ts or (t["ts"] == mark_ts and t["id"] not in mark_ids)]
        if not fresh:
            return []
        newest = max(t["ts"] for t in trades)
        ids_at_newest = [t["id"] for t in trades if t["ts"] == newest]
        if newest == mark_ts:
            ids_at_newest = sorted(mark_ids | set(ids_at_newest))
        self.store.execute(
            "INSERT INTO trade_marks(mint, ts, ids) VALUES(?,?,?) "
            "ON CONFLICT(mint) DO UPDATE SET ts=excluded.ts, ids=excluded.ids",
            (mint, newest, json.dumps(ids_at_newest)))
        rows = []
        for t in fresh:
            is_buy = t["kind"] == "buy"
            rows.append((
                t["wallet"], mint,
                t["usd"] if is_buy else 0.0, 0.0 if is_buy else t["usd"],
                t["tokens"] if is_buy else 0.0, 0.0 if is_buy else t["tokens"],
                t["ts"] if is_buy else None, None if is_buy else t["ts"],
            ))
        self.store.executemany(
            """INSERT INTO wallet_token(wallet, mint, buy_usd, sell_usd, buy_tokens, sell_tokens,
                                        n_trades, first_buy_ts, last_sell_ts)
               VALUES(?,?,?,?,?,?,1,?,?)
               ON CONFLICT(wallet, mint) DO UPDATE SET
                 buy_usd = buy_usd + excluded.buy_usd,
                 sell_usd = sell_usd + excluded.sell_usd,
                 buy_tokens = buy_tokens + excluded.buy_tokens,
                 sell_tokens = sell_tokens + excluded.sell_tokens,
                 n_trades = n_trades + 1,
                 first_buy_ts = CASE WHEN first_buy_ts IS NULL THEN excluded.first_buy_ts
                                     WHEN excluded.first_buy_ts IS NULL THEN first_buy_ts
                                     ELSE MIN(first_buy_ts, excluded.first_buy_ts) END,
                 last_sell_ts = CASE WHEN last_sell_ts IS NULL THEN excluded.last_sell_ts
                                     WHEN excluded.last_sell_ts IS NULL THEN last_sell_ts
                                     ELSE MAX(last_sell_ts, excluded.last_sell_ts) END""",
            rows)
        return self._promote({t["wallet"] for t in fresh})

    def _promote(self, wallets: set[str]) -> list[str]:
        promoted = []
        now = time.time()
        for w in wallets:
            if w in self.smart:
                continue
            rows = self.store.query("SELECT * FROM wallet_token WHERE wallet=?", (w,))
            wins = losses = 0
            realized = 0.0
            for r in rows:
                r = dict(r)
                verdict = classify(r)
                if verdict == "win":
                    wins += 1
                elif verdict == "loss":
                    losses += 1
                if verdict:
                    frac = min(1.0, r["sell_tokens"] / r["buy_tokens"])
                    realized += r["sell_usd"] * min(1.0, r["buy_tokens"] / r["sell_tokens"]) - r["buy_usd"] * frac
            if wins >= MIN_WINS and wins / (wins + losses) >= MIN_WIN_RATE:
                self.store.execute(
                    "INSERT OR REPLACE INTO smart_wallets(wallet, source, wins, losses, realized_usd, added_ts, updated_ts) "
                    "VALUES(?, 'learned', ?, ?, ?, ?, ?)", (w, wins, losses, realized, now, now))
                self.smart.add(w)
                promoted.append(w)
        return promoted

    def prune(self, max_age_s: float = 72 * 3600, ledger_age_s: float = 7 * 86400) -> None:
        """Forget watermarks for coins not traded in a while (they age out of the 72h window), and
        ledger rows with no activity for a week. The ledger grows by tens of thousands of rows a
        day; without this the database would keep growing for as long as the desk runs. Promoted
        wallets stay in smart_wallets either way."""
        now = time.time()
        self.store.execute("DELETE FROM trade_marks WHERE ts < ?", (now - max_age_s,))
        self.store.execute("DELETE FROM wallet_token WHERE MAX(COALESCE(first_buy_ts, 0), "
                           "COALESCE(last_sell_ts, 0)) < ?", (now - ledger_age_s,))
