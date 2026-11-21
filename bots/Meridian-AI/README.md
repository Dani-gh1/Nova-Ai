# Desk – papirhandels-bot til forex og råvarer

**Kun papirhandel med fiktive penge.** Der findes ingen kode, der sender ordrer til en mægler.
Datalaget kan kun lave `GET`-kald, og en test holder øje med det. Brug en *practice*-/demonøgle
eller en nøgle, der kun kan læse data.

- Instrumenter: EUR/USD, GBP/USD, USD/JPY, USD/CHF, AUD/USD, USD/CAD, NZD/USD, EUR/GBP, EUR/JPY,
  GBP/JPY, guld (XAU), sølv (XAG), WTI- og Brent-olie.
- Ren Python 3.11+ og kun standardbiblioteket (ingen `pip install`).
- 18 strategier kører side om side, hver på sin egen virtuelle konto.
- Dansk, mobilvenligt dashboard på port 8787. Alle tal vises i procent.

## Hurtig start

```bash
cd desk
python3 -m unittest discover -s tests -t .      # 58 tests
python3 -m desk backtest --days 90              # skriver docs/backtest.md
export OANDA_TOKEN=...                          # valgfri: practice-token fra OANDA (rigtig bid/ask). Uden den bruges Yahoo Finance
python3 -m desk check                           # test datakilden
python3 -m desk run                             # bot + dashboard (http://<host>:8787)
```

Uden nøgle bruger botten Yahoo Finance (`DESK_PROVIDER=auto`: OANDA hvis `OANDA_TOKEN` er sat, ellers Twelve Data hvis `TWELVEDATA_KEY` er sat, ellers Yahoo).
Vil du prøve det hele uden internet, brug `DESK_PROVIDER=synthetic python3 -m desk run` (syntetiske data).

## Arkitektur: seks agenter med hårde overdragelser

```
Scout ──ScoutReport──▶ Filter ──FilterReport──▶ Analyst ──AnalystReport──▶ strategi ──TradeIntent──▶
Risk ──RiskDecision──▶ Executor ──ExecutionResult        Logger skriver hvert led til SQLite
```

Overdragelserne er frosne dataklasser (`desk/models.py`). Hver agent får **kun** det, den forrige agent afleverede.

| Agent | Fil | Opgave |
|---|---|---|
| Scout | `agents/scout.py` | Vælger instrumenter i deres egne sessioner (Asien/London/New York) med nok udsving (ATR-ratio). Uden for egen session kræves mere udsving. |
| Filter | `agents/filter.py` | Dræber kandidater ved for stor spread (i forhold til ATR og til normal spread), vigtige nyheder inden for ±30 min, meget lav volatilitet, forældede data og rollover-vinduet kl. 17 New York-tid. |
| Analyst | `agents/analyst.py` | Giver en score fra 0 til 100 for både long og short ud fra trend (M15/H1/H4), momentum, ATR-udsving, renteforskel (carry) og korrelation med beslægtede par. Beregner også breakout- og mean reversion-komponenter. |
| Risk | `agents/risk.py` | Sætter stop og trailing stop i ATR-multipla. Har loft over samlet risiko pr. valuta (fem EUR-handler tæller som én stor EUR-handel), loft over korrelerede positioner, dagligt tabsloft og stop ved drawdown. Åbner ingen nye handler lige før weekenden. |
| Executor | `agents/executor.py` | Fast risiko i % af kontoen pr. handel. Fylder på bid/ask (spread), lægger glid på stop og trækker swap ved rollover (onsdag ×3). Har maks. antal positioner, gearingsloft, karantæne efter lukning og lukker alt før weekenden. |
| Logger | `agents/logger.py` | Den eneste, der skriver til SQLite: tik, scout, filter, signaler, beslutninger, handler, egenkapital, skyggehandler og hændelser. |

Alle tærskler ligger i **`desk/config.py`** og vises også under fanen *Regler*.

### Markedets ur (`desk/market.py`)
- Åbent fra søndag 17:00 til fredag 17:00 New York-tid. Sommertid håndteres via `zoneinfo`.
  Helligdagene 25/12 og 1/1 er lukkede.
- Sessionerne følger lokal tid: Sydney/Tokyo (= Asien), London og New York. Overlappet kan ses i dashboardet.
- Handelsdagen og dagligt tabsloft skifter kl. 17:00 New York-tid. Råvarer holder pause fra 17 til 18.

### Data
| Kilde | Pris | Bemærkning |
|---|---|---|
| **Yahoo Finance** (`yahoo`, standard uden nøgle) | ingen nøgle | Uofficielt, men åbent endpoint (kun GET). Første gang ca. 2 måneders 15-minutters lys, derefter det seneste døgn. Ingen bid/ask, så spreaden modelleres. Guld, sølv og olie er futures-priser. Botten scanner altså altid, også uden opsætning. |
| **OANDA practice** (`oanda`, anbefalet) | gratis demokonto | Bruger kun `GET /v3/instruments/{x}/candles` med `price=MBA`, som giver rigtig bid/ask og dermed rigtig spread. |
| Twelve Data (`twelvedata`) | gratis 800 kald/dag | Henter kun M15 og bygger selv H1/H4. Spread modelleres. 14 instrumenter hvert 5. min er for mange kald til gratisplanen, så hæv `REFRESH_S["M15"]` eller skær ned på `INSTRUMENTS`. |
| Alpha Vantage | 25 kald/dag gratis | Ikke implementeret, fordi kvoten er for lille til intradag-data. |
| `synthetic` | – | Til demo, tests og backtest. |

Nyheder kommer fra en gratis ugekalender i JSON (`DESK_NEWS_URL`, ForexFactory-format) og fra faste regler:
jobtal (NFP) første fredag, US CPI og rentemøder for Fed, ECB og BoE fra `CENTRAL_BANK_MEETINGS`.
Med de faste regler virker nyhedsfilteret også uden net.

**Opdatér `POLICY_RATES` og `CENTRAL_BANK_MEETINGS` i config.py**, når centralbankerne ændrer dem.
De bruges til carry-score og swap.

## Strategier (18)

`standard`, `trend`, `trend-langsom`, `breakout-london`, `breakout-newyork`, `meanrev-asien`, `nyhedsfilter`,
`kraesen`, `aggressiv`, `mange-smaa`, `momentum`, `carry`, `korrelation`, `kun-raavarer`, `kun-majors`,
`swing`, `scalp-overlap` og `tilfaeldig` (kontrol, der vælger ved møntkast og har samme risikostyring).
Hver strategi har sine egne komponentvægte, minimumsscore, risiko, stop, mål, maks. antal positioner og karantæne
(se `desk/strategies.py`). En strategi, der ikke slår `tilfaeldig`, har ingen fordel.

**Skyggehandler:** alle scorede signaler med score ≥ 55 følges som fiktive handler med 2 ATR stop og 2R mål,
uanset om en strategi handlede. Fanen *Handler* viser hitrate og gennemsnitlig R pr. scoreinterval,
så man kan se, om scoren virker.

**Epoke:** nulstiller alle konti uden at slette historikken.

```bash
python3 -m desk epoch --reason "nye renter i config"   # den kørende bot udfører den inden for 1 min
python3 -m desk epoch --offline                        # når botten ikke kører
```

## Dashboard (port 8787)

Fanerne er *Oversigt*, *Strategier*, *Handler* og *Regler*. Øverst ses, om markedet er åbent eller lukket,
den aktive session, hvornår markedet næste gang åbner eller lukker, og hvornår botten sidst gav lyd fra sig.
*Afkast over tid* er en blød kurve pr. strategi. Du kan fremhæve op til 4 strategier, og grafen kan også vises
som tabel. Dashboardet kan kun læse og har ingen knapper, der ændrer noget.
Det lytter på `0.0.0.0` som standard. Hvis serveren står på internettet, så sæt det bag en firewall eller VPN
(fx Tailscale), eller brug `DESK_DASHBOARD_HOST=127.0.0.1`.

## Drift (systemd, 24/7)

```bash
sudo useradd -r -s /usr/sbin/nologin desk
sudo cp -r desk /opt/desk
sudo cp /opt/desk/deploy/desk.env.example /etc/desk.env
sudo chmod 600 /etc/desk.env
sudo nano /etc/desk.env                # OANDA_TOKEN, NTFY_TOPIC
sudo cp /opt/desk/deploy/desk.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now desk
journalctl -u desk -f
```

- Tjenesten kører med `Restart=always`. Hovedløkken fanger selv fejl og sender en ntfy-alarm efter 3 fejl i træk.
- `state.json` (konti, positioner, karantæne, skyggehandler og epoke) skrives atomisk efter hvert tik.
  Er filen beskadiget, gemmes den som `.corrupt`, og botten starter rent.
- SQLite i WAL-tilstand ligger i `/var/lib/desk/desk.sqlite3`. Dashboardet åbner den skrivebeskyttet.
- ntfy sender besked ved start, åbne/lukke-handler (`NTFY_ON_TRADES`), tabsloft, datafejl og en dagsstatus kl. 17 NY.
- Mens markedet er lukket, sover botten 5 minutter ad gangen og laver ingen API-kald.

## Backtest

`python3 -m desk backtest --days 90 [--seed 7] [--source synthetic|csv:<mappe>]` kører **præcis samme pipeline**
som live. Kun uret og datalaget er udskiftet (`ReplayMarketData`, der aldrig ser ind i fremtiden).
Til CSV skal der ligge én fil pr. instrument, fx `EUR_USD.csv`, med kolonnerne `time,open,high,low,close` (UTC, M15).

Resultatet står i [`docs/backtest.md`](docs/backtest.md). **Vigtigt:** standardkørslen bruger *syntetiske* data.
De tester, at mekanikken virker (stops, spread, swap, lofter og sessioner), men siger intet om rigtig rentabilitet.
Kør backtesten på rigtige OANDA-data, og lad botten papirhandle i flere måneder, før du tror på en strategi.

## Filer

```
desk/config.py        alle tærskler          desk/engine.py     orkestrering, state.json, live-løkke
desk/models.py        overdragelseskontrakter desk/strategies.py 18 regelsæt
desk/market.py        weekend/sessioner       desk/accounts.py   P&L, omregning, swap, eksponering
desk/data.py          OANDA/TwelveData/replay desk/news.py       økonomisk kalender
desk/agents/*.py      de seks agenter         desk/shadow.py     skyggehandler
desk/dashboard.py     HTTP + API              desk/web/index.html dashboard (vanilla JS/SVG)
desk/backtest.py      backtest + rapport      desk/synthetic.py  syntetisk marked
deploy/               systemd-unit og env-eksempel
```
