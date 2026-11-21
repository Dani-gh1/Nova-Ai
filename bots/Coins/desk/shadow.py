"""Shadow trading: what would have happened if the desk had bought every coin it scored?

The real desk buys a coin or two a day, so it takes months to learn whether the score
threshold (73) and the filters are right. The shadow book pretends to buy every coin that
gets a score - also the ones under the threshold - and coins the Filter only rejected for
having pumped too much already. It follows each one with exactly the real rules (stop -6%
from the fill, trailing stop from +8%, 6% under the high, 120 min before the same coin again)
and records the result. No money moves and the real paper account is never touched.

Entries are priced off a real Jupiter quote of the size a real order would have (shrunk to
the same 1% impact cap). Exits are priced off the mark minus the impact the buy paid, so the
shadow book never spends Jupiter quota on sales. Marks come from DexScreener every few seconds,
on the shadow's own HTTP client in its own thread, so it can never delay a real stop.

This is a measurement, not a strategy: it ignores the 3-position cap (that is the point) and
its exits are estimates. Use it to compare score bands with each other, not as a forecast.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass

from .config import RiskConfig, ShadowConfig
from .messages import Candidate, new_id
from .store import Store

# scored by the Analyst / killed by the Filter only for +80% in 1h / scored, but only inside the
# wide shadow window (younger than 30 min, mcap under $300k, or older than 72 h)
KINDS = ("scored", "pumped", "wide_young", "wide_low_mcap", "wide_old")


@dataclass
class ShadowPosition:
    id: str
    mint: str
    symbol: str
    pair_address: str
    kind: str
    score: float | None
    opened_ts: float
    entry_price: float
    entry_impact_pct: float
    stake_usd: float
    stop_price: float
    high_price: float
    trail_active: bool
    trail_trigger_price: float
    trail_distance_pct: float
    last_price: float
    last_price_ts: float
    status: str = "open"
    closed_ts: float | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    pnl_pct: float | None = None


COLS = list(ShadowPosition.__dataclass_fields__)


class ShadowBook:
    def __init__(self, cfg: ShadowConfig, risk_cfg: RiskConfig, store: Store, executor, cooldown_minutes: float,
                 tx_cost_usd: float):
        self.cfg = cfg
        self.risk_cfg = risk_cfg
        self.store = store
        self.executor = executor          # only for order_size() / _fit_size(): the same sizing as a real buy
        self.cooldown_s = cooldown_minutes * 60
        self.tx_cost_usd = tx_cost_usd
        self.lock = threading.Lock()
        self.open: dict[str, ShadowPosition] = {}
        for r in store.query("SELECT * FROM shadow_positions WHERE status='open'"):
            d = dict(r)
            d["trail_active"] = bool(d["trail_active"])
            self.open[d["id"]] = ShadowPosition(**d)

    # ---- entries (scan thread) -----------------------------------------------
    def wants(self, mint: str, now: float | None = None) -> bool:
        if not self.cfg.enabled:
            return False
        now = now or time.time()
        with self.lock:
            if any(p.mint == mint for p in self.open.values()) or len(self.open) >= self.cfg.max_open:
                return False
        last = self.store.query("SELECT MAX(closed_ts) AS t FROM shadow_positions WHERE mint=?", (mint,))[0]["t"]
        return not (last and now - last < self.cooldown_s)

    def consider(self, c: Candidate, kind: str, score: float | None, decimals: int | None) -> ShadowPosition | None:
        """Open a shadow position for this coin if it has none (and is not cooling down)."""
        if kind not in KINDS or decimals is None or not self.wants(c.mint):
            return None
        size = self.executor.order_size()
        quote, size, impact, _ = self.executor._fit_size(c.mint, size)
        if quote is None:
            return None
        spent = int(quote["inAmount"]) / 1e6
        qty = int(quote["outAmount"]) / 10 ** int(decimals)
        if qty <= 0 or spent <= 0:
            return None
        entry = spent / qty
        now = time.time()
        r = self.risk_cfg
        pos = ShadowPosition(
            id=new_id("sh"), mint=c.mint, symbol=c.symbol, pair_address=c.pair_address, kind=kind, score=score,
            opened_ts=now, entry_price=entry, entry_impact_pct=impact or 0.0, stake_usd=spent,
            stop_price=entry * (1 - r.stop_loss_pct / 100), high_price=entry, trail_active=False,
            trail_trigger_price=entry * (1 + r.trail_trigger_pct / 100), trail_distance_pct=r.trail_distance_pct,
            last_price=entry / (1 + (impact or 0.0) / 100), last_price_ts=now)
        with self.lock:
            self.open[pos.id] = pos
            self._save(pos)
        return pos

    # ---- marks and exits (shadow thread) -----------------------------------------
    def tick(self, marks: dict[str, float], risk, now: float | None = None) -> list[ShadowPosition]:
        """Mark every open shadow; close the ones whose stop is hit (or whose price is gone)."""
        now = now or time.time()
        closed = []
        with self.lock:
            positions = list(self.open.values())
        for pos in positions:
            mark = marks.get(pos.mint)
            if mark is None:
                if now - pos.last_price_ts > self.risk_cfg.stale_price_seconds:
                    closed.append(self._close(pos, pos.last_price, "stale_price", now))
                continue
            d = risk.evaluate(pos, mark, now)
            pos.high_price = max(pos.high_price, d.high_price)
            pos.stop_price = max(pos.stop_price, d.stop_price)
            pos.trail_active = pos.trail_active or d.trail_active
            pos.last_price, pos.last_price_ts = mark, now
            if d.exit:
                closed.append(self._close(pos, mark, d.exit.reason, now))
            else:
                with self.lock:
                    self._save(pos)
        return closed

    def _close(self, pos: ShadowPosition, mark: float, reason: str, now: float) -> ShadowPosition:
        # The sale is estimated: the mark minus the same price impact the buy paid, and the fees.
        exit_price = mark * (1 - pos.entry_impact_pct / 100)
        fees_pct = 2 * self.tx_cost_usd / pos.stake_usd * 100 if pos.stake_usd else 0.0
        pos.status, pos.closed_ts, pos.exit_price, pos.exit_reason = "closed", now, exit_price, reason
        pos.pnl_pct = (exit_price / pos.entry_price - 1) * 100 - fees_pct
        with self.lock:
            self.open.pop(pos.id, None)
            self._save(pos)
        return pos

    def _save(self, pos: ShadowPosition) -> None:
        d = asdict(pos)
        d["trail_active"] = int(d["trail_active"])
        self.store.execute(f"INSERT OR REPLACE INTO shadow_positions({','.join(COLS)}) "
                           f"VALUES({','.join('?' * len(COLS))})", [d[c] for c in COLS])

    def open_positions(self) -> list[ShadowPosition]:
        with self.lock:
            return list(self.open.values())


def shadow_line(pos: ShadowPosition) -> str:
    score = "pumpet" if pos.kind == "pumped" else f"score {pos.score:.0f}"
    if pos.kind.startswith("wide_"):
        score += ", " + pos.kind
    if pos.status == "open":
        return f"SHADOW BUY  {pos.symbol:<10} ({score}) impact {pos.entry_impact_pct:.2f}%"
    return f"SHADOW SELL {pos.symbol:<10} ({score}) {pos.pnl_pct:+.1f}% ({pos.exit_reason})"


def shadow_payload(pos: ShadowPosition) -> dict:
    return json.loads(json.dumps(asdict(pos)))
