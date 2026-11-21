"""Regelsæt der testes side om side - hver på sin egen virtuelle konto.

Hver strategi vælger blandt Analystens signaler og sender OrderIntents videre til Risk.
Risiko-felter der ikke er sat arver standarden fra config.py.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, asdict

from . import config
from .agents.analyst import quote
from .models import Signal, OrderIntent


@dataclass
class Strategy:
    key: str
    name: str
    description: str
    mode: str
    rules: dict = field(default_factory=dict)
    size_pct: float | None = None
    max_positions: int | None = None
    max_entries_per_cycle: int | None = None
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    exit_hours_before_close: float | None = None
    max_pct_per_market: float | None = None
    max_pct_per_topic: float | None = None

    # --- arvede standarder -------------------------------------------------
    def p(self, name: str):
        v = getattr(self, name)
        if v is not None:
            return v
        return {
            "size_pct": config.EXEC_SIZE_PCT, "max_positions": config.EXEC_MAX_OPEN_POSITIONS,
            "max_entries_per_cycle": config.EXEC_MAX_ENTRIES_PER_CYCLE,
            "stop_loss_pct": config.RISK_STOP_LOSS_PCT, "take_profit_pct": config.RISK_TAKE_PROFIT_PCT,
            "exit_hours_before_close": config.RISK_EXIT_HOURS_BEFORE_CLOSE,
            "max_pct_per_market": config.RISK_MAX_PCT_PER_MARKET,
            "max_pct_per_topic": config.RISK_MAX_PCT_PER_TOPIC,
        }[name]

    def describe(self) -> dict:
        d = asdict(self)
        for k in ("size_pct", "max_positions", "max_entries_per_cycle", "stop_loss_pct", "take_profit_pct",
                  "exit_hours_before_close", "max_pct_per_market", "max_pct_per_topic"):
            d[k] = self.p(k)
        return d

    # --- fælles filtre ------------------------------------------------------
    def _passes(self, s: Signal, side: str, price: float) -> bool:
        r = self.rules
        f = s.features
        if not (r.get("price_min", 0.03) <= price <= r.get("price_max", 0.97)):
            return False
        if f["hours"] < r.get("hours_min", 0) or f["hours"] > r.get("hours_max", 1e9):
            return False
        if r.get("platforms") and s.market.platform not in r["platforms"]:
            return False
        if r.get("topics") and s.market.topic not in r["topics"]:
            return False
        if f["depth_usd"] < r.get("min_depth_usd", 0) or f["volume_24h"] < r.get("min_volume_24h", 0):
            return False
        if s.confidence < r.get("min_conf", 0):
            return False
        return True

    def _intent(self, s: Signal, side: str, reason: str, p_fair: float | None = None, pair_id: str = "") -> OrderIntent:
        return OrderIntent(strategy=self.key, market=s.market, book=s.book, side=side,
                           size_pct=self.p("size_pct"), reason=reason,
                           p_fair=s.p_fair if p_fair is None else p_fair, pair_id=pair_id)

    # --- forslag ------------------------------------------------------------
    def propose(self, signals: list[Signal], ctx: dict | None = None) -> list[OrderIntent]:
        ctx = ctx or {}
        r = self.rules
        ranked: list[tuple[float, OrderIntent | list]] = []
        for s in signals:
            f = s.features
            if self.mode == "edge":
                if s.net_edge < r.get("min_net_edge", 0.03):
                    continue
                if not self._passes(s, s.side, s.entry_price):
                    continue
                ranked.append((s.score, self._intent(s, s.side, f"kant {s.net_edge * 100:.1f} pp")))
            elif self.mode == "calibration":
                p_cal = s.p_market + s.components["calibration"]
                side = "YES" if p_cal >= s.p_market else "NO"
                price, fee = quote(s.market, s.book, side)
                win = p_cal if side == "YES" else 1 - p_cal
                net = win - price - fee
                if s.components.get("cal_n", 0) < r.get("min_cal_n", 30) or net < r.get("min_net_edge", 0.02):
                    continue
                if self._passes(s, side, price):
                    ranked.append((net, self._intent(s, side, f"kalibrering {net * 100:.1f} pp", p_fair=p_cal)))
            elif self.mode == "favorite":
                side = "YES" if s.p_market >= 0.5 else "NO"
                price, fee = quote(s.market, s.book, side)
                if self._passes(s, side, price):
                    ranked.append((price - f["hours"] / 1000, self._intent(s, side, f"favorit {price * 100:.0f} %")))
            elif self.mode in ("contrarian", "momentum", "volume_spike"):
                d1, spike = f["d1"], f["volume_spike"]
                if self.mode == "volume_spike":
                    if spike < r.get("min_spike", 3.0) or abs(d1) < r.get("min_abs_d1", 0.01):
                        continue
                    side = "YES" if d1 > 0 else "NO"
                else:
                    if abs(d1) < r.get("min_abs_d1", 0.05):
                        continue
                    if r.get("require_spike") and spike < config.ANALYST_VOLUME_SPIKE_RATIO:
                        continue
                    if r.get("require_no_spike") and spike >= config.ANALYST_VOLUME_SPIKE_RATIO:
                        continue
                    follow = self.mode == "momentum"
                    side = ("YES" if d1 > 0 else "NO") if follow else ("NO" if d1 > 0 else "YES")
                price, fee = quote(s.market, s.book, side)
                if self._passes(s, side, price):
                    word = {"contrarian": "kontra", "momentum": "momentum", "volume_spike": "volumenspids"}[self.mode]
                    ranked.append((abs(d1), self._intent(s, side, f"{word}: 24t {d1 * 100:+.1f} pp, spids x{spike:.1f}")))
            elif self.mode == "longshot_fade":
                if s.p_market > r.get("yes_max", 0.10):
                    continue
                price, fee = quote(s.market, s.book, "NO")
                if self._passes(s, "NO", price):
                    ranked.append((-s.p_market, self._intent(s, "NO", f"sælg longshot ({s.p_market * 100:.0f} %)")))
            elif self.mode == "time_decay":
                if not (r.get("yes_min", 0.05) <= s.p_market <= r.get("yes_max", 0.30)):
                    continue
                price, fee = quote(s.market, s.book, "NO")
                if self._passes(s, "NO", price):
                    ranked.append((-f["hours"], self._intent(s, "NO", f"tidsforfald, {f['hours']:.0f} t tilbage")))
            elif self.mode == "arbitrage":
                if not s.match or s.market.platform != "polymarket":
                    continue
                other = (ctx.get("signals_by_key") or {}).get(s.match["key"])
                if other is None:
                    continue
                legs = self._arb_legs(s, other)
                if legs:
                    ranked.append(legs)
            elif self.mode == "random":
                rng: random.Random = ctx.get("rng") or random.Random(0)
                if rng.random() > r.get("probability", 0.05):
                    continue
                side = rng.choice(("YES", "NO"))
                price, fee = quote(s.market, s.book, side)
                if self._passes(s, side, price):
                    ranked.append((rng.random(), self._intent(s, side, "møntkast")))
        ranked.sort(key=lambda x: -x[0])
        out: list[OrderIntent] = []
        for _, it in ranked[: self.p("max_entries_per_cycle") * 3]:
            out.extend(it if isinstance(it, list) else [it])
        return out

    def _arb_legs(self, a: Signal, b: Signal):
        """Køb JA hvor det er billigst og NEJ hvor JA er dyrest. Låser gevinst hvis summen < 1."""
        best = None
        for cheap, dear in ((a, b), (b, a)):
            py, fy = quote(cheap.market, cheap.book, "YES")
            pn, fn = quote(dear.market, dear.book, "NO")
            gap = 1 - (py + fy + pn + fn)
            if best is None or gap > best[0]:
                best = (gap, cheap, dear, py, pn)
        gap, cheap, dear, py, pn = best
        if gap < self.rules.get("min_gap", 0.03):
            return None
        pid = f"arb:{a.market.key}|{b.market.key}"
        why = f"arbitrage {gap * 100:.1f} pp (match {a.match['similarity']:.2f})"
        return (gap, [self._intent(cheap, "YES", why, pair_id=pid), self._intent(dear, "NO", why, pair_id=pid)])


STRATEGIES: list[Strategy] = [
    Strategy("balanceret", "Balanceret", "Følger Analystens samlede vurdering, moderat størrelse.",
             "edge", {"min_net_edge": 0.03}),
    Strategy("favorit_naer", "Favorit nær afgørelse",
             "Køber favoritten (85-97 %) når der er under 48 timer til afgørelse.",
             "favorite", {"price_min": 0.85, "price_max": 0.97, "hours_max": 48}, size_pct=3, stop_loss_pct=20),
    Strategy("kontraer", "Kontrær", "Satser imod store 24t-bevægelser uden volumenspids (overreaktion).",
             "contrarian", {"min_abs_d1": 0.08, "require_no_spike": True, "price_min": 0.08, "price_max": 0.9}),
    Strategy("arbitrage", "Cross-platform arbitrage",
             "Samme begivenhed på begge platforme: køb JA billigt det ene sted og NEJ det andet.",
             "arbitrage", {"min_gap": 0.03, "price_min": 0.02, "price_max": 0.98}, size_pct=3,
             stop_loss_pct=100, max_pct_per_market=6),
    Strategy("momentum", "Momentum", "Følger 24t-bevægelser på mindst 5 pp når volumen samtidig spidser.",
             "momentum", {"min_abs_d1": 0.05, "require_spike": True, "price_min": 0.1, "price_max": 0.9},
             stop_loss_pct=25),
    Strategy("hoej_likviditet", "Kun høj likviditet", "Kant fra Analysten, men kun i dybe, livlige markeder.",
             "edge", {"min_net_edge": 0.02, "min_depth_usd": 5000, "min_volume_24h": 50_000}),
    Strategy("kraesen", "Kræsen", "Kun meget store kanter med høj tillid. Få, større positioner.",
             "edge", {"min_net_edge": 0.08, "min_conf": 0.6}, size_pct=4, max_positions=8,
             max_entries_per_cycle=2),
    Strategy("mange_smaa", "Mange små", "Lav tærskel, 0,5 % pr. handel, op til 80 positioner.",
             "edge", {"min_net_edge": 0.01}, size_pct=0.5, max_positions=80, max_entries_per_cycle=15,
             max_pct_per_topic=40),
    Strategy("longshot_salg", "Sælg longshots", "Køber NEJ når JA koster 10 % eller mindre (longshot-skævhed).",
             "longshot_fade", {"yes_max": 0.10, "price_min": 0.88, "price_max": 0.98, "hours_max": 24 * 21},
             stop_loss_pct=50),
    Strategy("kalibrering", "Ren kalibrering",
             "Handler kun på kalibreringskomponenten (historiske udfald af lignende markeder).",
             "calibration", {"min_net_edge": 0.02, "min_cal_n": 30}),
    Strategy("tidsforfald", "Tidsforfald", "Køber NEJ i 5-30 %-markeder med under 72 timer tilbage.",
             "time_decay", {"yes_min": 0.05, "yes_max": 0.30, "hours_max": 72, "price_min": 0.68}),
    Strategy("volumenspids", "Volumenspids", "Følger kursretningen når 24t-volumen er mindst 3x normalen.",
             "volume_spike", {"min_spike": 3.0, "min_abs_d1": 0.02, "price_min": 0.08, "price_max": 0.92}),
    Strategy("kun_polymarket", "Kun Polymarket", "Balanceret, men kun Polymarket.",
             "edge", {"min_net_edge": 0.03, "platforms": ["polymarket"]}),
    Strategy("kun_kalshi", "Kun Kalshi", "Balanceret, men kun Kalshi.",
             "edge", {"min_net_edge": 0.03, "platforms": ["kalshi"]}),
    Strategy("politik", "Kun politik", "Balanceret, kun politiske markeder. Højere emneloft.",
             "edge", {"min_net_edge": 0.025, "topics": ["politik"]}, max_pct_per_topic=60),
    Strategy("sport", "Kun sport", "Balanceret, kun sportsmarkeder. Højere emneloft.",
             "edge", {"min_net_edge": 0.025, "topics": ["sport"]}, max_pct_per_topic=60),
    Strategy("midterzone", "Midterzone", "Kun markeder mellem 30 og 70 % - hvor usikkerheden er størst.",
             "edge", {"min_net_edge": 0.025, "price_min": 0.30, "price_max": 0.70}),
    Strategy("stram_stop", "Stram stop", "Balanceret med stop-loss på 15 % og gevinstsikring på 30 %.",
             "edge", {"min_net_edge": 0.03}, stop_loss_pct=15, take_profit_pct=30),
    Strategy("kontrol", "Kontrol: møntkast", "Tilfældig side i tilfældige markeder - basislinjen der skal slås.",
             "random", {"probability": 0.05}, size_pct=1),
]

BY_KEY = {s.key: s for s in STRATEGIES}
