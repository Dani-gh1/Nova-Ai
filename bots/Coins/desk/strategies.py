"""Strategy test: twenty rule sets run side by side on the same coins, each on its own
virtual account, so they can be compared fairly from the same start. They cover the most
widely used memecoin approaches, one idea each (rewritten 2026-11-19; the earlier 17 were
mostly variations of one "picky" idea).

  1  Momentum              buy strength: score >= 75 and $150k+ 1h volume
  2  Scalper               quick small wins: +6% target, -3% stop, out within 20 minutes
  3  Swing 24 t            give it up to a day: wide stop, wide trailing
  4  Månestråle            lottery: many tiny stakes, wide stop, one big winner pays for all
  5  Nylancering           launch sniper: only the first 60 minutes, +30% target
  6  Tidlig bølge          young and small: first 6 hours, $100k-$3M
  7  Etableret             old and big: $5-$20M, $500k+ liquidity, small steady moves
  8  Dybe pools            only pools with $300k+ liquidity and at most 0.3% impact
  9  Volumeneksplosion     $500k+ 1h volume, but not already +60%
  10 Smart money           score >= 80 only: buyers, holders and smart wallets all agree
  11 Rolig akkumulering    buy before it is obvious: at most +8% in the last hour
  12 Ikke FOMO             never buy the top: at most +30% in 1h, entry drift <= 1.5%
  13 Risiko 1:2            fixed risk/reward: -5% stop, +10% target, no trailing
  14 Risiko 1:3            -4% stop, +12% target, no trailing
  15 Sikker først          strictest anti-rug filters (LP 100%, top 10 <= 15%, dev <= 1%)
  16 Mange små             spread wide: 0.5% stakes, 10 open, quick exits
  17 Høj overbevisning     8% stakes, score >= 78, only 2 open, 24 h pause after 2 losses
  18 Kontrol               no score threshold: does the score help at all?
  19 Diamanthænder         -35% stop, no trailing, 24 h hold
  20 Tæt trailing          lock gains early: trailing from +4%, 3% under the high

All are measured the same way: a real Jupiter quote of the book's own order size on entry
(so a big order pays its real price impact), the real stop and trailing rules on every mark,
and an estimated sale (the mark minus the price impact the buy paid, minus fees). The real
paper account keeps running untouched.

The books get their coins from the desk's normal pipeline: every scorecard the Analyst makes
for the real window, and every scorecard from the shadow's wide window ($100k-$20M, 10 min-7
days). Filter rules shared by all books are applied once; each book's own extra limits are
checked per book.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field

from .messages import Candidate, new_id
from .store import Store

START_EQUITY = 10_000.0


@dataclass(frozen=True)
class StrategyRules:
    key: str
    name: str
    min_age_minutes: float
    max_age_hours: float
    min_mcap: float
    max_mcap: float
    min_lp_pct: float
    min_score: float
    stake_pct: float
    impact_cap_pct: float        # 0 = no cap
    max_open: int                # 0 = no cap (cash is the only limit)
    cooldown_minutes: float
    stop_pct: float
    trail_trigger_pct: float
    trail_distance_pct: float
    rules: tuple[tuple[str, tuple[str, ...]], ...] = field(default=())
    min_liq_usd: float = 40_000.0    # the Filter already requires $40k; a book can ask for more
    # Stricter filters a book can add on top of the shared Filter (None = use the shared rule).
    # A missing measurement fails the check (fail closed, like the Filter).
    min_vol_1h_usd: float | None = None
    max_change_1h_pct: float | None = None
    max_top10_pct: float | None = None
    max_dev_pct: float | None = None
    max_bundle_pct: float | None = None
    max_entry_drift_pct: float | None = None   # skip if the quote's price moved more than this since the scan
    min_cost_per_side_pct: float = 0.0        # costs per side are at least this (floor on the real impact)
    pause_after_losses: int = 0               # after this many losses in a row ...
    pause_hours: float = 0.0                  # ... buy nothing for this long (0 = never pause)
    take_profit_pct: float | None = None      # sell when the mark is this far above the entry
    max_hold_minutes: float | None = None     # sell after this long, whatever the price


def _rules(r: dict) -> tuple:
    return tuple((k, tuple(v)) for k, v in r.items())


STRATEGIES = (
    StrategyRules(
        "s1", "Momentum", 30, 72, 300_000, 20_000_000, 95, 75, 1.5, 1.0, 4, 60, 7, 10, 8,
        _rules({
            "Idé": ["Køb styrke: høj score og kraftig 1-times volumen. Den mest udbredte memecoin-strategi"],
            "Scout": ["Alder 30 min.–72 t.", "Markedsværdi $300k–$20 mio."],
            "Filter": ["1-times volumen mindst $150k", "Som strategi 2's grundfiltre (LP mindst 95 %)"],
            "Analyst": ["Køber ved score 75 eller mere"],
            "Executor": ["1,5 % af kontoen pr. handel", "Prisvirkning højst 1,0 %", "Højst 4 åbne", "Karantæne 1 t."],
            "Risk": ["Stop −7 %", "Trailing fra +10 %, 8 % under toppen"],
        }), min_vol_1h_usd=150_000),
    StrategyRules(
        "s2", "Scalper", 10, 72, 100_000, 20_000_000, 95, 68, 2.0, 0.5, 5, 10, 3, 3, 2,
        _rules({
            "Idé": ["Mange hurtige gevinster: små mål, stramt stop, ude igen på minutter"],
            "Scout": ["Alder 10 min.–72 t.", "Markedsværdi $100k–$20 mio. (bredt vindue)"],
            "Filter": ["Likviditet mindst $100k, 1-times volumen mindst $200k"],
            "Analyst": ["Køber ved score 68 eller mere"],
            "Executor": ["2 % af kontoen pr. handel", "Prisvirkning højst 0,5 %", "Højst 5 åbne", "Karantæne 10 min."],
            "Risk": ["Stop −3 %", "Sælger ved +6 % (gevinstmål)", "Trailing fra +3 %, 2 % under toppen",
                     "Sælger efter 20 min., hvis intet andet er sket"],
        }), min_liq_usd=100_000, min_vol_1h_usd=200_000, take_profit_pct=6.0, max_hold_minutes=20),
    StrategyRules(
        "s3", "Swing 24 t", 120, 72, 300_000, 20_000_000, 95, 72, 2.0, 1.0, 4, 180, 10, 20, 12,
        _rules({
            "Idé": ["Giv handlen op til et døgn: bredt stop og trailing, der lader vinderne løbe"],
            "Scout": ["Alder 2–72 t.", "Markedsværdi $300k–$20 mio."],
            "Filter": ["Som strategi 2's grundfiltre (LP mindst 95 %)"],
            "Analyst": ["Køber ved score 72 eller mere"],
            "Executor": ["2 % af kontoen pr. handel", "Prisvirkning højst 1,0 %", "Højst 4 åbne", "Karantæne 3 t."],
            "Risk": ["Stop −10 %", "Trailing fra +20 %, 12 % under toppen", "Sælger efter 24 t., hvis intet andet er sket"],
        }), max_hold_minutes=1440),
    StrategyRules(
        "s4", "Månestråle", 10, 168, 100_000, 20_000_000, 90, 60, 0.5, 1.0, 10, 30, 20, 50, 25,
        _rules({
            "Idé": ["Lotteri-strategien: mange små indsatser, bredt stop, og en enkelt kæmpevinder betaler for alle tab"],
            "Scout": ["Alder 10 min.–7 dage", "Markedsværdi $100k–$20 mio. (bredt vindue)"],
            "Filter": ["LP mindst 90 %"],
            "Analyst": ["Køber ved score 60 eller mere"],
            "Executor": ["0,5 % af kontoen pr. handel", "Prisvirkning højst 1,0 %", "Højst 10 åbne", "Karantæne 30 min."],
            "Risk": ["Stop −20 %", "Trailing fra +50 %, 25 % under toppen"],
        })),
    StrategyRules(
        "s5", "Nylancering", 10, 1, 100_000, 2_000_000, 90, 60, 0.5, 2.0, 8, 15, 10, 25, 15,
        _rules({
            "Idé": ["Skyd tidligt: kun de første 60 minutter efter lanceringen, små indsatser, hurtigt gevinstmål"],
            "Scout": ["Alder 10–60 min.", "Markedsværdi $100k–$2 mio."],
            "Filter": ["LP mindst 90 %"],
            "Analyst": ["Køber ved score 60 eller mere"],
            "Executor": ["0,5 % af kontoen pr. handel", "Prisvirkning højst 2,0 %", "Højst 8 åbne", "Karantæne 15 min."],
            "Risk": ["Stop −10 %", "Sælger ved +30 % (gevinstmål)", "Trailing fra +25 %, 15 % under toppen",
                     "Sælger efter 45 min., hvis intet andet er sket"],
        }), take_profit_pct=30.0, max_hold_minutes=45),
    StrategyRules(
        "s6", "Tidlig bølge", 30, 6, 100_000, 3_000_000, 95, 70, 1.0, 1.0, 4, 60, 8, 15, 10,
        _rules({
            "Idé": ["Kom ind, mens coinen stadig er ung og lille: de første 6 timer, før de store kommer ind"],
            "Scout": ["Alder 30 min.–6 t.", "Markedsværdi $100k–$3 mio."],
            "Filter": ["Som strategi 2's grundfiltre (LP mindst 95 %)"],
            "Analyst": ["Køber ved score 70 eller mere"],
            "Executor": ["1 % af kontoen pr. handel", "Prisvirkning højst 1,0 %", "Højst 4 åbne", "Karantæne 1 t."],
            "Risk": ["Stop −8 %", "Trailing fra +15 %, 10 % under toppen"],
        })),
    StrategyRules(
        "s7", "Etableret", 1440, 168, 5_000_000, 20_000_000, 95, 70, 3.0, 0.5, 3, 120, 5, 8, 5,
        _rules({
            "Idé": ["De store og gamle: høj markedsværdi, dyb likviditet og færre rug-pulls. Små, men stabile bevægelser"],
            "Scout": ["Alder 1–7 dage", "Markedsværdi $5–$20 mio."],
            "Filter": ["Likviditet mindst $500k", "Top 10 ≤ 20 %"],
            "Analyst": ["Køber ved score 70 eller mere"],
            "Executor": ["3 % af kontoen pr. handel", "Prisvirkning højst 0,5 %", "Højst 3 åbne", "Karantæne 2 t."],
            "Risk": ["Stop −5 %", "Trailing fra +8 %, 5 % under toppen"],
        }), min_liq_usd=500_000, max_top10_pct=20),
    StrategyRules(
        "s8", "Dybe pools", 30, 72, 300_000, 20_000_000, 95, 70, 3.0, 0.3, 3, 120, 6, 10, 7,
        _rules({
            "Idé": ["Kun coins, der kan handles næsten uden prisvirkning. Billige handler, så mere af gevinsten bliver tilbage"],
            "Scout": ["Alder 30 min.–72 t.", "Markedsværdi $300k–$20 mio."],
            "Filter": ["Likviditet mindst $300k"],
            "Analyst": ["Køber ved score 70 eller mere"],
            "Executor": ["3 % af kontoen pr. handel", "Prisvirkning højst 0,3 %", "Højst 3 åbne", "Karantæne 2 t."],
            "Risk": ["Stop −6 %", "Trailing fra +10 %, 7 % under toppen"],
        }), min_liq_usd=300_000),
    StrategyRules(
        "s9", "Volumeneksplosion", 30, 72, 300_000, 20_000_000, 95, 65, 1.5, 1.5, 4, 60, 8, 12, 8,
        _rules({
            "Idé": ["Følg pengene: coins, hvor handelsvolumen sprænger skalaen, men som ikke allerede er løbet løbsk"],
            "Scout": ["Alder 30 min.–72 t.", "Markedsværdi $300k–$20 mio."],
            "Filter": ["1-times volumen mindst $500k", "Højst +60 % på 1 time (ikke for sent ind)"],
            "Analyst": ["Køber ved score 65 eller mere"],
            "Executor": ["1,5 % af kontoen pr. handel", "Prisvirkning højst 1,5 %", "Højst 4 åbne", "Karantæne 1 t."],
            "Risk": ["Stop −8 %", "Trailing fra +12 %, 8 % under toppen", "Sælger efter 4 t., hvis intet andet er sket"],
        }), min_vol_1h_usd=500_000, max_change_1h_pct=60, max_hold_minutes=240),
    StrategyRules(
        "s10", "Smart money", 30, 72, 300_000, 20_000_000, 95, 80, 5.0, 1.0, 3, 120, 6, 10, 6,
        _rules({
            "Idé": ["Kun når alt peger samme vej: køberstrøm, holdervækst og smart wallets er enige. Få, men store handler"],
            "Scout": ["Alder 30 min.–72 t.", "Markedsværdi $300k–$20 mio."],
            "Filter": ["Som strategi 2's grundfiltre (LP mindst 95 %)"],
            "Analyst": ["Køber ved score 80 eller mere"],
            "Executor": ["5 % af kontoen pr. handel", "Prisvirkning højst 1,0 %", "Højst 3 åbne", "Karantæne 2 t."],
            "Risk": ["Stop −6 %", "Trailing fra +10 %, 6 % under toppen"],
        })),
    StrategyRules(
        "s11", "Rolig akkumulering", 30, 72, 300_000, 20_000_000, 95, 62, 1.5, 1.0, 4, 90, 6, 10, 6,
        _rules({
            "Idé": ["Køb, før det bliver åbenlyst: coins med god score, men som endnu ikke har bevæget sig meget"],
            "Scout": ["Alder 30 min.–72 t.", "Markedsværdi $300k–$20 mio."],
            "Filter": ["Højst +8 % på 1 time"],
            "Analyst": ["Køber ved score 62 eller mere"],
            "Executor": ["1,5 % af kontoen pr. handel", "Prisvirkning højst 1,0 %", "Højst 4 åbne", "Karantæne 90 min."],
            "Risk": ["Stop −6 %", "Trailing fra +10 %, 6 % under toppen"],
        }), max_change_1h_pct=8),
    StrategyRules(
        "s12", "Ikke FOMO", 30, 72, 300_000, 20_000_000, 95, 70, 2.0, 0.5, 3, 120, 6, 10, 7,
        _rules({
            "Idé": ["Køb aldrig toppen: højst +30 % på 1 time, kursen må ikke have flyttet sig siden scanningen, og ejerne skal være spredt"],
            "Scout": ["Alder 30 min.–72 t.", "Markedsværdi $300k–$20 mio."],
            "Filter": ["Højst +30 % på 1 time", "Top 10 ≤ 20 %"],
            "Analyst": ["Køber ved score 70 eller mere"],
            "Executor": ["2 % af kontoen pr. handel", "Prisvirkning højst 0,5 %", "Højst 3 åbne", "Karantæne 2 t.",
                         "Køber ikke, hvis kursen har flyttet sig over 1,5 % siden scanningen"],
            "Risk": ["Stop −6 %", "Trailing fra +10 %, 7 % under toppen"],
        }), max_change_1h_pct=30, max_top10_pct=20, max_entry_drift_pct=1.5),
    StrategyRules(
        "s13", "Risiko 1:2", 30, 72, 300_000, 20_000_000, 95, 70, 2.0, 1.0, 4, 60, 5, 100, 10,
        _rules({
            "Idé": ["Klassisk fast risiko/gevinst: risikér 5 for at tjene 10. Ingen trailing, kun stop eller mål"],
            "Scout": ["Alder 30 min.–72 t.", "Markedsværdi $300k–$20 mio."],
            "Filter": ["Som strategi 2's grundfiltre (LP mindst 95 %)"],
            "Analyst": ["Køber ved score 70 eller mere"],
            "Executor": ["2 % af kontoen pr. handel", "Prisvirkning højst 1,0 %", "Højst 4 åbne", "Karantæne 1 t."],
            "Risk": ["Stop −5 %", "Sælger ved +10 % (gevinstmål)", "Ingen trailing"],
        }), take_profit_pct=10.0),
    StrategyRules(
        "s14", "Risiko 1:3", 30, 72, 300_000, 20_000_000, 95, 70, 2.0, 1.0, 4, 60, 4, 100, 10,
        _rules({
            "Idé": ["Som 1:2, men risikér 4 for at tjene 12. Kræver kun, at hver fjerde handel vinder, for at gå i nul"],
            "Scout": ["Alder 30 min.–72 t.", "Markedsværdi $300k–$20 mio."],
            "Filter": ["Som strategi 2's grundfiltre (LP mindst 95 %)"],
            "Analyst": ["Køber ved score 70 eller mere"],
            "Executor": ["2 % af kontoen pr. handel", "Prisvirkning højst 1,0 %", "Højst 4 åbne", "Karantæne 1 t."],
            "Risk": ["Stop −4 %", "Sælger ved +12 % (gevinstmål)", "Ingen trailing"],
        }), take_profit_pct=12.0),
    StrategyRules(
        "s15", "Sikker først", 30, 72, 300_000, 20_000_000, 100, 68, 2.0, 0.5, 3, 120, 6, 10, 6,
        _rules({
            "Idé": ["Undgå rug-pulls for enhver pris: de strammeste sikkerhedskrav, selv med lavere scoretærskel"],
            "Scout": ["Alder 30 min.–72 t.", "Markedsværdi $300k–$20 mio."],
            "Filter": ["LP 100 % brændt/låst", "Likviditet mindst $200k", "Top 10 ≤ 15 %, udvikler ≤ 1 %, bundtede ≤ 3 %"],
            "Analyst": ["Køber ved score 68 eller mere"],
            "Executor": ["2 % af kontoen pr. handel", "Prisvirkning højst 0,5 %", "Højst 3 åbne", "Karantæne 2 t."],
            "Risk": ["Stop −6 %", "Trailing fra +10 %, 6 % under toppen"],
        }), min_liq_usd=200_000, max_top10_pct=15, max_dev_pct=1, max_bundle_pct=3),
    StrategyRules(
        "s16", "Mange små", 10, 168, 100_000, 20_000_000, 90, 55, 0.5, 1.0, 10, 15, 4, 3, 2,
        _rules({
            "Idé": ["Spred bredt: rigtig mange små handler, lave krav og hurtigt ude igen"],
            "Scout": ["Alder 10 min.–7 dage", "Markedsværdi $100k–$20 mio. (bredt vindue)"],
            "Filter": ["LP mindst 90 %"],
            "Analyst": ["Køber ved score 55 eller mere"],
            "Executor": ["0,5 % af kontoen pr. handel", "Prisvirkning højst 1,0 %", "Højst 10 åbne", "Karantæne 15 min."],
            "Risk": ["Stop −4 %", "Sælger ved +5 % (gevinstmål)", "Trailing fra +3 %, 2 % under toppen",
                     "Sælger efter 30 min., hvis intet andet er sket"],
        }), take_profit_pct=5.0, max_hold_minutes=30),
    StrategyRules(
        "s17", "Høj overbevisning", 30, 72, 300_000, 20_000_000, 95, 78, 8.0, 1.0, 2, 180, 6, 12, 8,
        _rules({
            "Idé": ["Satser stort, men sjældent: kun de allerbedste coins, og pause, når det går dårligt"],
            "Scout": ["Alder 30 min.–72 t.", "Markedsværdi $300k–$20 mio."],
            "Filter": ["Som strategi 2's grundfiltre (LP mindst 95 %)"],
            "Analyst": ["Køber ved score 78 eller mere"],
            "Executor": ["8 % af kontoen pr. handel (den rigtige kontos maksimum)", "Prisvirkning højst 1,0 %", "Højst 2 åbne",
                         "Karantæne 3 t."],
            "Risk": ["Stop −6 %", "Trailing fra +12 %, 8 % under toppen", "Pause i 24 t. efter 2 tab i træk"],
        }), pause_after_losses=2, pause_hours=24),
    StrategyRules(
        "s18", "Kontrol: ingen scoretærskel", 30, 72, 300_000, 20_000_000, 95, 0, 1.0, 1.0, 3, 120, 6, 8, 6,
        _rules({
            "Idé": ["Kontrolgruppen: køber alt, der består filtrene, uanset score. Viser, om scoren overhovedet hjælper"],
            "Scout": ["Alder 30 min.–72 t.", "Markedsværdi $300k–$20 mio."],
            "Filter": ["Som strategi 2's grundfiltre (LP mindst 95 %)"],
            "Analyst": ["Ingen scoretærskel: køber alt, der kommer igennem filtrene"],
            "Executor": ["1 % af kontoen pr. handel", "Prisvirkning højst 1,0 %", "Højst 3 åbne", "Karantæne 2 t."],
            "Risk": ["Stop −6 %", "Trailing fra +8 %, 6 % under toppen"],
        })),
    StrategyRules(
        "s19", "Diamanthænder", 30, 72, 300_000, 20_000_000, 95, 72, 1.0, 1.0, 3, 240, 35, 100, 20,
        _rules({
            "Idé": ["Det modsatte af en stram stop: hold i op til et døgn og lad kursen svinge. Tester, om stop-tabene æder gevinsten"],
            "Scout": ["Alder 30 min.–72 t.", "Markedsværdi $300k–$20 mio."],
            "Filter": ["Som strategi 2's grundfiltre (LP mindst 95 %)"],
            "Analyst": ["Køber ved score 72 eller mere"],
            "Executor": ["1 % af kontoen pr. handel", "Prisvirkning højst 1,0 %", "Højst 3 åbne", "Karantæne 4 t."],
            "Risk": ["Stop −35 %", "Ingen trailing", "Sælger efter 24 t., hvis intet andet er sket"],
        }), max_hold_minutes=1440),
    StrategyRules(
        "s20", "Tæt trailing", 30, 72, 300_000, 20_000_000, 95, 70, 1.5, 1.0, 4, 60, 5, 4, 3,
        _rules({
            "Idé": ["Lås gevinsten tidligt: trailing slås til ved +4 % og følger tæt efter"],
            "Scout": ["Alder 30 min.–72 t.", "Markedsværdi $300k–$20 mio."],
            "Filter": ["Som strategi 2's grundfiltre (LP mindst 95 %)"],
            "Analyst": ["Køber ved score 70 eller mere"],
            "Executor": ["1,5 % af kontoen pr. handel", "Prisvirkning højst 1,0 %", "Højst 4 åbne", "Karantæne 1 t."],
            "Risk": ["Stop −5 %", "Trailing fra +4 %, 3 % under toppen"],
        })),
)
MIN_LP = min(s.min_lp_pct for s in STRATEGIES)


@dataclass
class BookPosition:
    id: str
    strategy: str
    mint: str
    symbol: str
    pair_address: str
    score: float
    opened_ts: float
    qty: float
    cost_usd: float
    entry_price: float
    entry_impact_pct: float
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
    proceeds_usd: float | None = None
    pnl_usd: float | None = None


COLS = list(BookPosition.__dataclass_fields__)


class StrategyBook:
    def __init__(self, rules: StrategyRules, store: Store, fit, tx_cost_usd: float, min_order_usd: float,
                 start_ts: float | None = None):
        self.r = rules
        self.store = store
        self.fit = fit                  # (mint, usd, cap) -> (quote, usd, impact_pct, tried)
        self.tx = tx_cost_usd
        self.min_order = min_order_usd
        self.lock = threading.RLock()
        row = store.query("SELECT * FROM strategy_account WHERE strategy=?", (rules.key,))
        if row:
            self.start_ts, self.start_equity, self.cash = row[0]["start_ts"], row[0]["start_equity"], row[0]["cash"]
        else:
            self.start_ts, self.start_equity, self.cash = start_ts or time.time(), START_EQUITY, START_EQUITY
            self._save_account()
        self.open: dict[str, BookPosition] = {}
        for r in store.query("SELECT * FROM strategy_positions WHERE strategy=? AND status='open'", (rules.key,)):
            d = dict(r)
            d["trail_active"] = bool(d["trail_active"])
            self.open[d["id"]] = BookPosition(**d)

    def _save_account(self) -> None:
        self.store.execute("INSERT INTO strategy_account(strategy, start_ts, start_equity, cash) VALUES(?,?,?,?) "
                           "ON CONFLICT(strategy) DO UPDATE SET cash=excluded.cash",
                           (self.r.key, self.start_ts, self.start_equity, self.cash))

    def _save(self, p: BookPosition) -> None:
        d = asdict(p)
        d["trail_active"] = int(d["trail_active"])
        self.store.execute(f"INSERT OR REPLACE INTO strategy_positions({','.join(COLS)}) "
                           f"VALUES({','.join('?' * len(COLS))})", [d[c] for c in COLS])

    def equity(self) -> float:
        with self.lock:
            return self.cash + sum(p.qty * p.last_price for p in self.open.values())

    # ---- entries -------------------------------------------------------------
    def in_window(self, c: Candidate) -> bool:
        return (self.r.min_age_minutes / 60 <= c.pair_age_hours <= self.r.max_age_hours
                and self.r.min_mcap <= c.mcap_usd <= self.r.max_mcap)

    def wants(self, c: Candidate, now: float | None = None) -> bool:
        if not self.in_window(c):
            return False
        now = now or time.time()
        with self.lock:
            if any(p.mint == c.mint for p in self.open.values()):
                return False
            if 0 < self.r.max_open <= len(self.open):
                return False
        last = self.store.query("SELECT MAX(closed_ts) AS t FROM strategy_positions WHERE strategy=? AND mint=?",
                                (self.r.key, c.mint))[0]["t"]
        return not (last and now - last < self.r.cooldown_minutes * 60)

    def qualifies(self, c: Candidate, score: float, checks) -> bool:
        ch = checks if isinstance(checks, dict) else {"lp_locked_pct": checks}
        r = self.r

        def at_most(value, limit):
            return limit is None or (value is not None and value <= limit)
        lp = ch.get("lp_locked_pct")
        return (score >= r.min_score and (lp is None or lp >= r.min_lp_pct)
                and c.liquidity_usd >= r.min_liq_usd
                and (r.min_vol_1h_usd is None or c.volume.get("h1", 0.0) >= r.min_vol_1h_usd)
                and at_most(c.price_change.get("h1"), r.max_change_1h_pct)
                and at_most(ch.get("top10_pct"), r.max_top10_pct)
                and at_most(ch.get("dev_pct"), r.max_dev_pct)
                and at_most(ch.get("bundle_pct"), r.max_bundle_pct))

    def paused(self, now: float) -> bool:
        """After N losses in a row, buy nothing for pause_hours from the last of them."""
        n = self.r.pause_after_losses
        if n <= 0:
            return False
        last = self.store.query("SELECT pnl_usd, closed_ts FROM strategy_positions WHERE strategy=? AND status='closed' "
                                "ORDER BY closed_ts DESC LIMIT ?", (self.r.key, n))
        return (len(last) == n and all((r["pnl_usd"] or 0) < 0 for r in last)
                and now - last[0]["closed_ts"] < self.r.pause_hours * 3600)

    def consider(self, c: Candidate, score: float, checks, decimals: int | None) -> BookPosition | None:
        if not self.qualifies(c, score, checks) or decimals is None:
            return None
        if not self.wants(c) or self.paused(time.time()):
            return None
        with self.lock:
            size = min(self.equity() * self.r.stake_pct / 100, self.cash - self.tx)
        if size < self.min_order:
            return None
        quote, size, impact, _ = self.fit(c.mint, size, self.r.impact_cap_pct)
        if quote is None:
            return None
        spent = int(quote["inAmount"]) / 1e6
        qty = int(quote["outAmount"]) / 10 ** int(decimals)
        if qty <= 0 or spent <= 0:
            return None
        impact = impact or 0.0
        mid = spent / qty / (1 + impact / 100)
        if self.r.max_entry_drift_pct is not None and c.price_usd > 0 \
                and abs(mid / c.price_usd - 1) * 100 > self.r.max_entry_drift_pct:
            return None
        if impact < self.r.min_cost_per_side_pct:    # charge at least the floor: fewer tokens for the same money
            impact = self.r.min_cost_per_side_pct
            qty = spent / (mid * (1 + impact / 100))
        entry = spent / qty
        now = time.time()
        p = BookPosition(
            id=new_id("st"), strategy=self.r.key, mint=c.mint, symbol=c.symbol, pair_address=c.pair_address,
            score=score, opened_ts=now, qty=qty, cost_usd=spent + self.tx, entry_price=entry,
            entry_impact_pct=impact, stop_price=entry * (1 - self.r.stop_pct / 100), high_price=entry,
            trail_active=False, trail_trigger_price=entry * (1 + self.r.trail_trigger_pct / 100),
            trail_distance_pct=self.r.trail_distance_pct, last_price=entry / (1 + impact / 100), last_price_ts=now)
        with self.lock:
            if p.cost_usd > self.cash + 1e-9:
                return None
            self.cash -= p.cost_usd
            self.open[p.id] = p
            self._save(p)
            self._save_account()
        return p

    # ---- marks and exits -----------------------------------------------------------
    def tick(self, marks: dict[str, float], risk, stale_s: float, now: float | None = None) -> list[BookPosition]:
        now = now or time.time()
        closed = []
        with self.lock:
            positions = list(self.open.values())
        for p in positions:
            mark = marks.get(p.mint)
            if mark is None:
                if now - p.last_price_ts > stale_s:
                    closed.append(self._close(p, p.last_price, "stale_price", now))
                continue
            d = risk.evaluate(p, mark, now)
            p.high_price = max(p.high_price, d.high_price)
            p.stop_price = max(p.stop_price, d.stop_price)
            p.trail_active = p.trail_active or d.trail_active
            p.last_price, p.last_price_ts = mark, now
            if d.exit:
                closed.append(self._close(p, mark, d.exit.reason, now))
            elif self.r.take_profit_pct is not None and mark >= p.entry_price * (1 + self.r.take_profit_pct / 100):
                closed.append(self._close(p, mark, "take_profit", now))
            elif self.r.max_hold_minutes is not None and now - p.opened_ts >= self.r.max_hold_minutes * 60:
                closed.append(self._close(p, mark, "time_stop", now))
            else:
                with self.lock:
                    self._save(p)
        return closed

    def _close(self, p: BookPosition, mark: float, reason: str, now: float) -> BookPosition:
        exit_price = mark * (1 - p.entry_impact_pct / 100)       # estimated: the buy's impact again
        proceeds = p.qty * exit_price - self.tx
        p.status, p.closed_ts, p.exit_price, p.exit_reason = "closed", now, exit_price, reason
        p.proceeds_usd, p.pnl_usd = proceeds, proceeds - p.cost_usd
        p.last_price = mark
        with self.lock:
            self.open.pop(p.id, None)
            self.cash += proceeds
            self._save(p)
            self._save_account()
        return p


def reset_if_new_epoch(store: Store, epoch: str | None) -> bool:
    """Start every book over (once) when the configured epoch changes. Only the strategy test's
    own tables and journal lines are removed; the paper account and the shadow book stay."""
    if not epoch or store.get_account("strategy_epoch") == epoch:
        return False
    with store.lock:
        for table in ("strategy_account", "strategy_positions", "strategy_equity"):
            store.conn.execute(f"DELETE FROM {table}")
        store.conn.execute("DELETE FROM events WHERE agent='strategy'")
        store.conn.execute("DELETE FROM account WHERE key='strategy_mark_every_s'")
        store.conn.commit()
    store.set_account("strategy_epoch", epoch)
    return True


class StrategyLab:
    """The strategy books, fed from the desk's pipeline and marked from the shadow thread."""
    EQUITY_EVERY_S = 60.0

    def __init__(self, store: Store, fit, tx_cost_usd: float, min_order_usd: float, risk, stale_s: float,
                 strategies=STRATEGIES, epoch: str | None = None):
        self.reset = reset_if_new_epoch(store, epoch)
        self.store = store
        self.risk = risk
        self.stale_s = stale_s
        start = time.time()                    # books created together share one start
        self.books = [StrategyBook(s, store, fit, tx_cost_usd, min_order_usd, start) for s in strategies]
        self._equity_at = 0.0

    def wants(self, c: Candidate) -> bool:
        return any(b.wants(c) for b in self.books)

    # ---- speed: what each book sees per scan round, and how often it is marked ----------
    def new_round(self, real_window: list[Candidate], wide: list[Candidate]) -> None:
        self.round = {b.r.key: {"window": sum(1 for c in [*real_window, *wide] if b.in_window(c)),
                                "scored": [], "qualified": [], "bought": 0} for b in self.books}

    def note(self, c: Candidate, score: float, lp, bought: list[str]) -> None:
        rnd = getattr(self, "round", None)
        if rnd is None:
            return
        for b in self.books:
            if not b.in_window(c):
                continue
            r = rnd[b.r.key]
            r["scored"].append(c.mint)
            if b.qualifies(c, score, lp):
                r["qualified"].append(c.mint)
            r["bought"] += b.r.key in bought

    def end_round(self) -> dict:
        rnd, self.round = getattr(self, "round", None) or {}, None
        return rnd

    def consider(self, c: Candidate, score: float, checks, decimals: int | None) -> list[BookPosition]:
        out = []
        for b in self.books:
            p = b.consider(c, score, checks, decimals)
            if p:
                out.append(p)
        return out

    def open_positions(self) -> list[BookPosition]:
        return [p for b in self.books for p in list(b.open.values())]

    def tick(self, marks: dict[str, float], now: float | None = None) -> list[BookPosition]:
        now = now or time.time()
        self._ticks = getattr(self, "_ticks", [])
        self._ticks.append(now)
        self._ticks = [t for t in self._ticks if now - t <= 300]
        if len(self._ticks) > 1 and now - getattr(self, "_speed_at", 0) >= 60:
            self._speed_at = now
            gaps = len(self._ticks) - 1
            self.store.set_account("strategy_mark_every_s", (self._ticks[-1] - self._ticks[0]) / gaps)
        closed = [p for b in self.books for p in b.tick(marks, self.risk, self.stale_s, now)]
        if closed or now - self._equity_at >= self.EQUITY_EVERY_S:
            self._equity_at = now
            self.store.executemany("INSERT INTO strategy_equity(ts, strategy, equity) VALUES(?,?,?)",
                                   [(now, b.r.key, b.equity()) for b in self.books])
        return closed
