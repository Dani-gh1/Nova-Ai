# CLAUDE.md – Papirdesk

Papirhandels-bot ("desk") til amerikanske aktier og ETF'er. **KUN papirhandel med
fiktive penge** – der må aldrig tilføjes kode der sender ordrer til en rigtig mægler.

## Hurtigt overblik
- Ren Python 3.11+ **stdlib**, ingen afhængigheder. Tilføj ikke pakker.
- Alle tærskler i `config.py`. Agenterne får `cfg` injiceret (tests bruger `tests/helpers.cfg()`).
- Kæde med hårde overdragelser (frosne dataklasser i `desk/models.py`):
  `Snapshot → Scout → Candidate → Filter → Passed → Analyst → Scored → Risk → Order → Executor → Fill`.
  `Logger` (`desk/agents/logger.py`) er den eneste der skriver til SQLite (`data/desk.sqlite`).
- `desk/pipeline.py: Desk.run_cycle` er én cyklus og bruges af BÅDE live-botten og backtesten.
  `defer=True` (backtest) returnerer ordrer, som fyldes til næste dags åbningskurs.
- `desk/strategies.py`: ≥15 regelsæt, hver med egen virtuel konto i `state.json` (papirbogen, `desk/ledger.py`).
- `desk/market_clock.py`: NYSE-helligdage beregnes (inkl. påske/langfredag), halve dage, før-/eftermarked,
  sommertid via zoneinfo. Ekstraordinære lukkedage: `config.EXTRA_HOLIDAYS`.
- `desk/data.py`: Yahoo chart v8 (barer/intradag), Nasdaq API (bid/ask, earnings-kalender),
  NasdaqTrader RSS (halts). Dagsbarer caches i `data/cache/daily/`.
- `desk/bot.py`: hovedløkke – forbered ~45 min før åbning, cyklus hvert 10. min, dagsafslutning, sov ellers.
- `desk/dashboard.py` + `desk/static/index.html`: dansk dashboard på port 8787, kun læsning, **viser kun procent**.

## Kommandoer
```bash
python3 -m unittest discover -s tests -t .     # alle tests (hurtige, ingen netværk)
python3 backtest.py                            # historisk backtest (henter ~550 symboler fra Yahoo)
python3 backtest.py --synthetic                # offline demo
python3 dashboard.py                           # kun dashboard
python3 bot.py                                 # bot + dashboard (normalt via systemd)
```

## Konventioner
- Brugerrettet tekst (dashboard, logbeskeder, ntfy) er på **dansk**.
- Papirpenge vises aldrig som beløb – kun procent (afkast, vægt i % af konto). API'et må ikke sende `cash`.
- Nulstilling: hæv `config.EPOCH`. Ledger arkiverer gammel bog som `state.epoch-<n>.json`;
  alle SQLite-rækker har en `epoch`-kolonne og dashboardet filtrerer på den aktuelle.
- Nyt regelsæt: tilføj et `Strategy(...)` i `STRATEGIES`; ledgeren opretter automatisk en konto.
- Ny tærskel: læg den i `config.py`, brug den via `cfg`, og tilføj den til `RULE_LABELS` i dashboard.py.
- Botten må aldrig dø af datafejl: fejl logges, ntfy'es og løkken fortsætter (systemd `Restart=always` er sidste værn).
- Start ikke botten i en udviklingssession uden at blive bedt om det; brug tests eller en midlertidig cfg.
