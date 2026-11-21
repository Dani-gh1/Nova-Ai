# CLAUDE.md – desk

Papirhandels-bot til prediktionsmarkeder (Polymarket + Kalshi). **Kun fiktive penge.**

## Absolutte regler
- Aldrig rigtige ordrer, tegnebøger, private nøgler eller autentificerede API-kald. Kun offentlige GET-endpoints.
- Kun Python 3.11+ standardbibliotek. Ingen pip-afhængigheder.
- Papirpenge vises ALTID i procent (dashboard, ntfy, rapporter, logs). Interne beløb er procentpoint af
  startkapitalen (konto = 100.0). `EXEC_NOMINAL_BANKROLL_USD` bruges kun til at regne slippage mod orderbogen;
  eksponér aldrig USD-beløb eller kontraktantal i brugerflader.
- Alle tærskler hører hjemme i `desk/config.py` (kan overskrives med `DESK_<NAVN>`). Ingen magiske tal i agenterne.
- Brugerrettet tekst er på dansk.
- Start ikke botten (`python -m desk run`) eller services uden at brugeren beder om det.

## Arkitektur
Scout → Filter → Analyst → Risk → Executor, Logger skriver alt til SQLite (`data/desk.sqlite3`).
Overdragelserne er hårde: hvert trin tager kun forrige trins type (`desk/models.py`) og logges i `handoffs`/`decisions`.
Risk og Executor arbejder på en `Account` i hukommelsen (`desk/accounts.py`), så samme kode bruges live og i backtest.
`desk/pipeline.py` loader konti fra SQLite, kører én cyklus og skriver tilbage.

## Kommandoer
- Tests: `python3 -m unittest discover -s tests -t .` (ingen netværk; skal være grønne før commit)
- Backtest: `python3 -m desk backtest` (`--offline` bruger cache)
- Dashboard: `python3 -m desk serve` (port 8787)
- Én cyklus: `python3 -m desk run --once`; nulstil konti: `python3 -m desk epoch "note"`

## Konventioner
- Ny strategi: tilføj en `Strategy(...)` i `desk/strategies.py`. Ny `mode` kræver en gren i `Strategy.propose`
  og en test i `tests/test_agents.py`.
- Ændrer du regler/tærskler, så de påvirker live-resultater: foreslå brugeren en ny epoch.
- API-felter: Kalshi bruger `*_dollars` / `*_fp` strenge (ældre cent-felter håndteres også).
  Polymarkets Gamma API svarer 406 på `limit=100` – brug 50.
- Netværksfejl på én platform må aldrig stoppe en cyklus; saml dem i `summary["errors"]`.
- Dashboard-frontenden er én fil (`desk/static/index.html`) uden eksterne afhængigheder; diagrammer er håndskrevet SVG.
