# Papirdesk – papirhandel med krypto-perpetuals

> ## ⚠️ KUN PAPIRHANDEL
> Papirdesk handler **udelukkende med fiktive penge**. Den bruger kun **offentlige**
> markedsdata fra Binance eller Bybit, har **ingen API-nøgler** og **kan ikke sende ordrer**.
> En test (`tests/test_system.py`) fejler, hvis koden nogensinde kommer til at indeholde
> ordre-endpoints eller nøglesignering.
>
> ## ⚠️ Gearing kan give tab større end indsatsen
> Perpetual futures med gearing er ekstremt risikable. I virkeligheden kan et kursgab,
> en ustabil børs, auto-deleveraging eller cross-margin betyde, at du **taber mere end
> det beløb, du satte ind** – og et positivt papirresultat er **ingen garanti** for,
> at strategien tjener penge med rigtige penge. Intet her er investeringsrådgivning.

## Hvad den gør

Seks agenter med hårde overdragelser – hver agent modtager kun den forrige agents
output-type (`desk/models.py`):

```
MarketSnapshot ─Scout─▶ Candidate ─Filter─▶ FilteredCandidate ─Analyst─▶ Signal
     ─(regelsæt)─▶ TradeIdea ─Risk─▶ RiskPlan ─Executor─▶ Position
                                           Logger skriver alt til SQLite
```

| Agent | Fil | Ansvar |
|---|---|---|
| **Scout** | `desk/agents/scout.py` | Finder kontrakter med usædvanlig funding, OI-ændring (1t/4t) eller likvidationsbølger blandt de mest handlede. |
| **Filter** | `desk/agents/filter.py` | Dræber kontrakter med for stor spread, tynd orderbog (dybde ±0,5 %) eller for høj volatilitet (ATR og største 1t-bevægelse). |
| **Analyst** | `desk/agents/analyst.py` | Scorer 0–100 for **både** long og short ud fra momentum, funding-ekstremer (+ long/short-forhold), OI-ændring vs. pris, likvidationer (realiserede + estimeret likvidationskort) og trend. |
| **Risk** | `desk/agents/risk.py` | Gearing 1x–5x, likvideringspris (isoleret margin), funding hver 8. time, hårdt stop der **altid** ligger før likvideringsprisen, maks. samlet kontogearing, dagligt tabsloft der pauser handel til midnat UTC. |
| **Executor** | `desk/agents/executor.py` | Fast procent af kontoen som margin, slippage ved at "gå" orderbogen, taker/maker-gebyr, maks. åbne positioner. Lukker ved stop, mål, tid. |
| **Logger** | `desk/agents/logger.py` | Den eneste der skriver til SQLite: hændelser fra alle agenter, signaler, handler, equity, skyggehandler, epochs. |

Alle tærskler står i **`desk/config.py`** og kan overskrives med miljøvariabler
(`DESK_<NAVN>`, fx `DESK_EXCHANGE=bybit`, `DESK_MAX_ACCOUNT_LEVERAGE=2`).

### Simuleringen

* **Konto:** hver strategi har sin egen virtuelle konto. Internt føres den i en fiktiv
  referencestørrelse (`PAPER_START_BALANCE`, kun brugt til at regne realistisk slippage mod
  den rigtige orderbog) – men **alt vises i procent** af startkapitalen.
* **Likvideringspris:** isoleret margin, lineær USDT-kontrakt:
  `long = (entry − margin/qty) / (1 − mmr)`, `short = (entry + margin/qty) / (1 + mmr)`.
* **Hårdt stop:** højst `STOP_MAX_FRACTION_OF_LIQ` (80 %) af afstanden til likvidering.
  Afvises hvis det ikke kan ligge før likvideringsprisen – også efter slippage.
* **Funding:** ved 00/08/16 UTC betaler long en positiv rate (short modtager). Betalingen
  trækkes fra positionens isolerede margin, så likvideringsprisen kryber nærmere; Risk
  strammer da stoppet, så det stadig ligger før.
* **Likvidering:** sker kun hvis prisen springer forbi likvideringsprisen, før stoppet kan
  handles (kursgab). Hele marginen tabes. Kortene tæller antal likvideringer.
* **Stop vs. mål i samme bar:** stoppet antages ramt først (konservativt).
* **Dagligt tabsloft:** falder en konto `DAILY_LOSS_LIMIT` (5 %) fra dagens start, åbnes
  ingen nye handler før næste UTC-døgn. Åbne positioner styres videre.

### 18 regelsæt side om side (`desk/strategies.py`)

| Gruppe | Regelsæt |
|---|---|
| Funding-arbitrage-lignende | Funding-carry 1x, Funding-carry 2x |
| Trend med lav gearing | Trend 2x, Langsom trend 1x |
| Mean reversion | Likvidations-rebound 2x og 3x, OI-divergens 2x |
| Breakout | Breakout + OI 3x, Breakout stramt 2x |
| Aggressiv | Aggressiv 5x, Aggressiv momentum 5x, Scalper 3x |
| Kræsen | Kræsen 2x, Kræsen kun long 2x |
| Positionering | Shorter trængte longs 2x, Short squeeze 2x, Kontrær positionering 1x |
| Standard | Balanceret 2x |

Et regelsæt er ren data: vægte for Analystens fem komponenter, krav til flag
(fx `liq_wave`, `breakout_up`, `!funding_positive`), min. score og "kant" (score minus
modsat side), gearing, margin %, stop (evt. ATR-baseret), mål og maks. holdetid.
Nye regelsæt får automatisk en frisk konto ved næste start.

**Skyggehandler:** hvert scoret signal handles hypotetisk på *begge* sider (1x, fast stop,
mål og horisont fra config). Fanen *Regler* viser gennemsnitligt udfald pr. scoreinterval –
så du kan se, om høj score faktisk betyder noget.

**Epoch:** `python3 -m desk epoch "ny tærskel for funding"` nulstiller alle konti til 100 %.
Historikken bevares i SQLite under det gamle epoch-nummer.

## Krav

* Python **3.11+**, kun standardbiblioteket. Ingen `pip install`.
* Netadgang til `fapi.binance.com` + `fstream.binance.com` (eller `api.bybit.com` +
  `stream.bybit.com`). Begge børser blokerer visse lande (bl.a. USA) – brug den der virker,
  hvor botten kører (`DESK_EXCHANGE=bybit`).
  Standarden er `DESK_EXCHANGE=auto`: botten prøver Binance og skifter selv til Bybit, hvis Binance er spærret (og skifter
  igen, hvis den børs senere bliver spærret). Brug `binance` eller `bybit` for at låse valget.

## Kom i gang

```bash
python3 -m unittest discover -s tests -t .
```

```bash
python3 -m desk once
```

```bash
python3 -m desk run
```

Åbn derefter `http://<maskinen>:8787`. Øvrige kommandoer:

```bash
python3 -m desk status
```

```bash
python3 -m desk dashboard
```

```bash
python3 -m desk epoch "note om hvad der er ændret"
```

### Dashboard (port 8787)

Dansk og mobilvenligt, uden eksterne afhængigheder (`desk/static/index.html`):

* **Oversigt** – nøgletal og *Afkast over tid* som bløde kurver (de tre bedste regelsæt +
  gennemsnit af alle), med crosshair/tooltip.
* **Strategier** – et kort pr. regelsæt med afkast, største fald, antal likvideringer,
  handler, træfsikkerhed, eksponering og en minikurve. Kan sorteres.
* **Handler** – åbne positioner (resultat, afstand til stop og likvidering, funding) og
  lukkede handler.
* **Regler** – skyggehandel-statistik, alle regelsæt, alle tærskler, epochs.

Papirpenge vises **altid** i procent. API'et (`/api/overview`, `/api/trades`, `/api/rules`)
returnerer heller ingen absolutte beløb. Dashboardet har ingen login – eksponér ikke port
8787 mod internettet; brug fx en SSH-tunnel eller VPN.

## Backtest

Samme pipeline og samme regelsæt over historiske 1t-bars – uden lookahead (bar *i*
styrer først åbne positioner med high/low, derefter scannes på lukkekursen).

```bash
python3 -m desk backtest --source binance --days 30
```

```bash
python3 -m desk backtest --source synthetic --days 60 --seed 7
```

Muligheder: `--symbols BTCUSDT,ETHUSDT`, `--json fil.json`, `--db sti.sqlite`,
`--state data/backtest_state.json` (så kan dashboardet vise backtesten:
`DESK_STATE_PATH=data/backtest_state.json DESK_DB_PATH=data/backtest.sqlite python3 -m desk dashboard`).

Begrænsninger, du skal kende:

* Binance gemmer kun ca. **30 dages** OI- og long/short-historik.
* Likvidationer findes ikke historisk via REST. Backtesten bruger en **proxy**: stor
  bevægelse (> 1,5×ATR) kombineret med OI-fald eller omsætningsspring.
* Orderbogen i backtesten er syntetisk (spread og dybde fra config/omsætning).
* `--source synthetic` tester mekanikken – ikke om en strategi har en kant.

Et kørt eksempel ligger i [`BACKTEST.md`](BACKTEST.md).

## Drift som systemd-service

```bash
sudo useradd --system --home /opt/papirdesk --shell /usr/sbin/nologin papirdesk
```

```bash
sudo cp -r . /opt/papirdesk && sudo mkdir -p /opt/papirdesk/data && sudo chown -R papirdesk: /opt/papirdesk
```

```bash
sudo cp deploy/papirdesk.service /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now papirdesk
```

```bash
journalctl -u papirdesk -f
```

Servicen kører med `Restart=always` og må kun skrive i `data/`. Al løbende tilstand
(konti, åbne positioner, epoch, sidste priser) ligger i `data/state.json`, som skrives
atomisk efter hvert tick, så en genstart fortsætter præcis hvor den slap. Efter
`desk epoch` skal servicen genstartes (`sudo systemctl restart papirdesk`), ellers
overskriver den kørende proces den nye state.

### ntfy-notifikationer

Sæt et (svært gætteligt) emne i servicefilen: `Environment=DESK_NTFY_TOPIC=mit-emne`
og evt. `DESK_NTFY_SERVER`. Du får besked ved start, nye/lukkede handler (samlet),
likvideringer, pause pga. tabsloft, ny epoch og gentagne fejl. Beløb er i procent.

## Filer

```
desk/config.py          alle tærskler
desk/models.py          datatyperne mellem agenterne
desk/agents/*.py        Scout, Filter, Analyst, Risk, Executor, Logger
desk/strategies.py      de 18 regelsæt
desk/engine.py          løkken, konti, state.json, epochs
desk/feeds.py           Binance/Bybit (REST + likvidations-websocket)
desk/net.py             HTTP og minimal WebSocket-klient (stdlib)
desk/shadow.py          skyggehandler
desk/dashboard.py       HTTP-server + API
desk/static/index.html  dashboardet
desk/backtest.py        backtest + syntetisk data
deploy/papirdesk.service
tests/                  unittest (ingen netværk)
```
