# CLAUDE.md – Papirdesk

## Hvad det er
Papirhandels-bot til store krypto-USDT-par på Binance. **KUN fiktive penge.** Ingen
API-nøgler og ingen ordre-kode. `desk/binance.py` kan kun lave GET mod offentlige
endpoints. **Byg aldrig noget til rigtige penge eller med nøgler med handelsrettigheder
uden først at spørge brugeren.**

## Faste krav fra brugeren
- Ren Python 3.11+ stdlib, ingen afhængigheder.
- Alle tærskler i `config.py`. Hardcode ikke tal i agenterne.
- Papirpenge vises ALTID i procent, aldrig i dollars/USDT. Det gælder dashboard,
  notifikationer og CLI. `START_BALANCE` er en intern regneenhed.
  Testen `test_dashboard_never_exposes_money` håndhæver det.
- Dashboard på dansk og mobilvenligt, port 8787, fanerne Oversigt, Strategier,
  Handler og Regler.
- Mindst 15 strategier side om side; nulstilles ved at ændre `EPOCH`.
- ntfy kun ved køb, salg, stop og nedbrud.
- systemd med `Restart=always`. Papirbogen ligger i `state.json`.
- Start ikke botten (`python3 -m desk run`) selv. `scan`, `backtest` og tests er i orden.

## Struktur
```
config.py                 alle tærskler + STRATEGIES (18 stk.)
desk/models.py            dataklasser til overdragelser: Candidate → Approved → Scored → EntryPlan; Position, Account, Book, Kline
desk/agents/scout.py      24t-ticker → Candidate (volumen, top N, udelukkelser)
desk/agents/filter.py     → Approved (spread, dybde ±1 %, udsving 1t via 5m-lys)
desk/agents/analyst.py    → Scored. Rene funktioner compute_features / score_profiles / btc_ok / move_too_big (deles med backtest)
desk/agents/risk.py       plan() → EntryPlan; evaluate()/check() stop, trailing og tid → ExitSignal
desk/agents/executor.py   fill_buy/fill_sell (går orderbogen), gebyr, sizing, maks. positioner, karantæne
desk/agents/logger.py     SQLite: events, scores, trades, equity, backtests (epoch-mærket)
desk/engine.py            Desk: scan-tråd (SCAN_INTERVAL) + tick hvert sekund, skyggehandler, notifikationer, gem state
desk/state.py             state.json: atomisk skrivning, gendannelse, epoch-nulstilling (arkiverer gammel fil)
desk/backtest.py          fetch_history (cache i data/) + run_backtest (samme Analyst/Risk/Executor)
desk/dashboard.py         DashboardAPI (JSON, kun procent) + ThreadingHTTPServer
desk/web/index.html       SPA: monoton kubisk kurve, sparklines, mørk/lys tilstand
desk/notify.py            ntfy via JSON-publicering
desk/__main__.py          CLI: run | scan | dashboard | status | backtest | notify-test
deploy/papirdesk.service  systemd-unit (/opt/papirdesk, bruger papirdesk)
tests/                    unittest; FakeMarket i tests/helpers.py, ingen netværk
```

## Nøglebeslutninger
- Hårde overdragelser: hver agents `run`/`plan`/`enter` kaster `TypeError` ved forkert inputtype.
- Analyst-profiler: balanced, momentum, trend, breakout, meanrev, lowvol, highvol og
  hold. En strategi vælger profil og min_score, og Risk filtrerer på strategiens
  øvrige regler (btc_filter, top_n, symbols, min_rel_vol, max_volatility_pct).
- Features beregnes på AFSLUTTEDE lys plus aktuel kurs, så live og backtest er ens.
- Stop er relativt til den faktiske fyldpris (inkl. slippage). Trailing-stop flytter sig kun op.
- Skyggehandler: én pr. scoret symbol, 1000 enheder, standard-risk og ejer `__shadow__`.
  De logges i `trades` med `shadow=1`.
- `clean_shutdown` i state: false mens botten kører. Er den false ved start, sendes en crash-notifikation.
- Binance: `api.binance.com` er geoblokeret (451) visse steder. Derfor falder klienten
  tilbage til `data-api.binance.vision`.
- Scout udelukker stablecoins og tokeniserede aktier (fx NVDAB, SOXLB) via `SCOUT_EXCLUDE_BASES`.
  Listen skal udvides, når Binance tilføjer nye aktietokens.

## Kommandoer
- Tests: `python3 -m unittest`
- Tør scanning: `python3 -m desk scan`
- Backtest: `python3 -m desk backtest --days 90 [--symbols A,B | --top N]`
