# CLAUDE.md – Desk (papirhandels-bot)

## Ufravigelige regler
- **Kun papirhandel.** Tilføj aldrig kode, der placerer ordrer hos en mægler. `data.http_get_json` er den eneste
  netværksvej for data og må kun lave GET. Den eneste POST er ntfy-notifikationer i `notify.py`.
- **Penge vises altid i procent.** API'erne i `dashboard.py` må ikke returnere saldo, beløb eller enheder.
  `tests/test_engine_dashboard.py::test_no_money_amounts_exposed` holder øje med det.
- **Kun stdlib**, Python 3.11+. Ingen tredjepartspakker.
- **Tærskler hører hjemme i `desk/config.py`.** Hardcod ikke tal i agenterne. Strategi-specifikke parametre
  ligger i `StrategyParams` i `desk/strategies.py`.
- **Hårde overdragelser:** Scout → Filter → Analyst → strategi → Risk → Executor. Hver agent får kun den forriges
  frosne objekt (`desk/models.py`). Logger er den eneste, der skriver til SQLite. Engine skriver `state.json`.
- Brugervendt tekst (dashboard, ntfy, logbeskeder) skrives på dansk.

## Kommandoer
```bash
python3 -m unittest discover -s tests -t .     # alle tests (~6 s)
python3 -m desk backtest --days 30             # hurtig backtest (~30 s); 90 dage ≈ 2 min
DESK_PROVIDER=synthetic DESK_DATA_DIR=/tmp/d python3 -m desk run   # lokal demo uden internet
# Standard er DESK_PROVIDER=auto: OANDA (hvis OANDA_TOKEN), ellers Twelve Data, ellers Yahoo Finance (ingen nøgle, scanner altid)
python3 -m desk status | epoch | check | dashboard
```
Testene sætter `DESK_DATA_DIR` til en midlertidig mappe (`tests/__init__.py`). Kør dem aldrig mod `var/`.

## Arkitektur i korte træk
- `engine.Desk.tick(now)` er hele pipelinen. Den bruges både live (`LiveMarketData`) og i backtest
  (`ReplayMarketData`), så ændringer skal virke i begge.
- Lys er altid *lukkede* lys (`closed_only`), og Replay må aldrig se fremtiden.
- Exits (stop/mål/trailing) tjekkes på de M15-lys, der er lukket siden `Position.last_checked`.
  Rammes stop og mål i samme lys, tæller stop først.
- Valutaeksponering: hver position tæller `+risk_pct` på sin basisvaluta og `-risk_pct` på sin kursvaluta
  (fortegn efter retning). Råvarer (XAU, XAG, WTICO, BCO) behandles som "valutaer".
- Handelsdagen skifter kl. 17:00 New York-tid (`market.trading_day`). Swap trækkes ved skiftet, og onsdag tæller tredobbelt.
- Epoke: `Desk.new_epoch` nulstiller konti og skyggehandler. Live sker det via `var/epoch_request.json`
  (CLI'en skriver filen, løkken udfører).
- Ydelse: `market.next_change` og `indicators.atr_series` er memoiserede. Kald dem ikke i løkker uden cache.

## Når du ændrer noget
- Ny tærskel: læg den i `config.py`, tilføj den til `RULE_GROUPS` i `dashboard.py` og giv den en dansk tekst
  i `RULE_DA` i `web/index.html`.
- Ny strategi: tilføj en `StrategyParams` i `STRATEGIES`. Navnet skal være unikt og bruges som konto-id.
  Skift epoke, hvis parametrene for en eksisterende strategi ændres.
- Opdatér `POLICY_RATES` og `CENTRAL_BANK_MEETINGS`, når centralbankerne ændrer dem.
- Kør testene. Ved ændringer i signal- eller risikologik skal backtesten også køres, og `docs/backtest.md` opdateres.
- Start ikke selve botten (`desk run`) i en udviklingssession, medmindre du bliver bedt om det.
