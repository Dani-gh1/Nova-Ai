# CLAUDE.md – Papirdesk

## Ufravigelige regler
- **Kun papirhandel.** Tilføj aldrig private nøgler, seed-fraser, signering, web3-afhængigheder eller noget,
  der sender transaktioner. Executor ændrer kun rækker i SQLite.
- **Kun Python stdlib** (3.11+). Ingen `pip`-afhængigheder.
- **Alle tærskler i `config.py`.** Agenterne må ikke have hårdkodede tal.
- **Hårde overdragelser.** Agenter kalder ikke hinanden. Hver agent tager én type fra `desk/models.py`
  ind og returnerer én type eller `Kill`. Orkestratoren (`desk/app.py`) flytter objekterne, og
  `Logger.handoff()` logger hver overdragelse. Kun `desk/logger.py` skriver til databasen.
- **Papirpenge vises kun i procent** i dashboard, API, ntfy og CLI. Markedstal (likviditet,
  markedsværdi, volumen) må stå i $. `tests/test_dashboard.py` tjekker `FORBIDDEN_KEYS`.
- **Start ikke botten** (`python3 -m desk run`) eller systemd-servicen uden at brugeren beder om det.
  `scan` (tørkørsel i hukommelsen) og tests er ok.

## Kommandoer
- Tests: `python3 -m unittest discover -s tests -t .` (ingen netværk, under 1 s)
- Tørkørsel mod live-API'er: `python3 -m desk scan --chain base`
- Backtest: `python3 -m desk backtest --pools 25` (tager minutter pga. GeckoTerminals hastighedsgrænse)

## Struktur
- `desk/sources.py`: rene `parse_*`-funktioner (testbare) og tynde netværksfunktioner.
  Agenterne får netværksfunktioner injiceret (`Scout(gecko=…)`, `Filter(goplus=…)`, `Desk(price_feed=…)`),
  så tests bruger fakes.
- `risk.check()` er en ren funktion, som både live-løkken og `backtest.candle_exit()` bruger.
  Ændringer i exit-logik slår derfor igennem begge steder.
- Konti: én pr. (strategi, kæde) pr. epoch. `Logger.ensure_accounts()` tilføjer konti for nye strategier
  uden nulstilling. Brug `Logger.new_epoch()` for at nulstille.
- Ny strategi: tilføj `replace(_B, key=…, …)` i `config.STRATEGIES` (unik `key`, dansk `name`).
- Database-skemaet ligger i `desk/logger.py` (`SCHEMA`). Nye kolonner kræver migrering i `Logger.__init__`
  (se `liquidity_usd` som eksempel).

## Konventioner
- UI-tekst, logbeskeder og drabsårsager er på dansk.
- Drabsårsager, der starter med et præfiks i `config.PERMANENT_KILL_REASONS`, vurderes aldrig igen.
- Tidsstempler er unix-sekunder (float). Beløb internt er USD-floats (vises ikke).
