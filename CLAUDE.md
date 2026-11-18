# CLAUDE.md – Papirdesk

Papirhandels-bot til krypto perpetual futures. **KUN papirhandel med fiktive penge.**

## Ufravigelige regler

1. **Aldrig rigtig handel.** Tilføj aldrig ordre-endpoints, API-nøgler, signering
   (HMAC) eller noget, der kan flytte rigtige penge. `tests/test_system.py::PaperOnlyTests`
   skal blive grøn. Kun offentlige GET-endpoints og offentlige websockets.
2. **Kun Python-standardbiblioteket** (3.11+). Ingen tredjepartspakker, heller ikke i tests
   (brug `unittest`) eller dashboardet (ingen CDN'er – alt inline i `desk/static/index.html`).
3. **Hårde overdragelser.** Agenter kommunikerer kun via typerne i `desk/models.py`:
   Scout→Candidate, Filter→FilteredCandidate, Analyst→Signal, Strategy→TradeIdea,
   Risk→RiskPlan, Executor→Position. En agent må ikke kalde en senere agent.
   **Logger er den eneste, der skriver til SQLite.**
4. **Stop ligger altid før likvideringsprisen** – ved åbning, efter slippage og efter
   hver funding-betaling. Testen `test_stop_always_before_liquidation_for_every_strategy_and_leverage`
   må aldrig svækkes.
5. **Papirpenge vises altid i procent** i dashboard, API, CLI og notifikationer. Ingen
   absolutte saldi/margin i `/api/*` (der er en test for det).
6. **Tærskler hører hjemme i `desk/config.py`** – ingen magiske tal spredt i agenterne,
   undtagen rene normaliseringskonstanter i Analyst.
7. Brugerrettet tekst (dashboard, README, CLI, ntfy) er på **dansk**.
8. Start ikke botten/servicen selv; kør tests og evt. `backtest --source synthetic`.

## Kommandoer

```bash
python3 -m unittest discover -s tests -t .
```

```bash
python3 -m desk backtest --source synthetic --days 30
```

```bash
DESK_STATE_PATH=/tmp/s.json DESK_DB_PATH=/tmp/d.sqlite DESK_DASHBOARD_PORT=8799 python3 -m desk dashboard
```

## Arkitektur i korte træk

* `engine.Desk.tick()` (hvert `TICK_SECONDS`): likvidering → stop/mål/tid → funding →
  dagligt tabsloft → drawdown → skyggehandler → equity-snapshot.
* `engine.Desk.scan()` (hvert `SCAN_SECONDS`): Scout → Filter → Analyst → for hvert
  regelsæt: `Strategy.evaluate` → `Risk.assess` → `Executor.open`.
* `feeds.MarketFeed` er interfacet; `BinanceFeed`, `BybitFeed` og `backtest.HistoricalFeed`
  implementerer det. Backtesten bruger præcis samme `Desk`.
* State: `data/state.json` (atomisk skrivning) er autoritativ for konti/positioner;
  SQLite (`data/desk.sqlite`) er loggen. Alle rækker har `epoch`.
* Konto-beløb er interne fiktive USDT (`PAPER_START_BALANCE`) for realistisk slippage;
  konverter med `Account.pct()` før visning.

## Nyt regelsæt

Tilføj en `Strategy(...)` i `desk/strategies.py` (unikt `name`, gearing 1–5). Flag der kan
kræves, defineres i `Analyst.score()` (`flags`). Kør tests og en syntetisk backtest.

## Kendte forenklinger

Ét MMR-trin; likvidering kun ved kursgab forbi likvideringsprisen; live-stop tjekkes med
1m-candles siden sidste tick; backtestens orderbog og likvidationer er proxies; Binance
har kun ~30 dages OI/long-short-historik.
