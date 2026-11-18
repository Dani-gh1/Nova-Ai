"""Scout: finder kontrakter med usædvanlig funding, OI-ændring eller likvidationsbølger.

Input: list[MarketSnapshot]   Output: list[Candidate]
"""
from __future__ import annotations

from .. import config
from ..models import Candidate, MarketSnapshot


class Scout:
    name = "scout"

    def run(self, snapshots: list[MarketSnapshot]) -> list[Candidate]:
        out: list[tuple[float, Candidate]] = []
        for s in snapshots:
            if s.volume_24h_usd < config.SCOUT_MIN_VOLUME_USD or s.mark_price <= 0:
                continue
            reasons: list[str] = []
            strength = 0.0
            if abs(s.funding_rate) >= config.SCOUT_FUNDING_ABS:
                reasons.append(f"funding {s.funding_rate * 100:+.3f}%")
                strength += abs(s.funding_rate) / config.SCOUT_FUNDING_ABS
            if abs(s.oi_change_1h) >= config.SCOUT_OI_CHANGE_1H:
                reasons.append(f"OI 1t {s.oi_change_1h * 100:+.1f}%")
                strength += abs(s.oi_change_1h) / config.SCOUT_OI_CHANGE_1H
            if abs(s.oi_change_4h) >= config.SCOUT_OI_CHANGE_4H:
                reasons.append(f"OI 4t {s.oi_change_4h * 100:+.1f}%")
                strength += abs(s.oi_change_4h) / config.SCOUT_OI_CHANGE_4H
            liq = s.liq_long_usd_1h + s.liq_short_usd_1h
            hourly_vol = s.volume_24h_usd / 24 or 1
            if liq >= config.SCOUT_LIQ_USD_1H or liq / hourly_vol >= config.SCOUT_LIQ_VOLUME_RATIO:
                dom = "longs" if s.liq_long_usd_1h >= s.liq_short_usd_1h else "shorts"
                reasons.append(f"likvidationsbølge {liq / 1e6:.1f}M ({dom})")
                strength += max(liq / config.SCOUT_LIQ_USD_1H,
                                liq / hourly_vol / config.SCOUT_LIQ_VOLUME_RATIO)
            if reasons:
                out.append((strength, Candidate(snapshot=s, reasons=reasons)))
        out.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in out[:config.SCOUT_MAX_CANDIDATES]]
