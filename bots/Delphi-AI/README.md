# desk – papirhandels-bot til prediktionsmarkeder

**Kun papirhandel med fiktive penge.** desk læser offentlige data fra Polymarket og Kalshi
(ingen konto, ingen API-nøgler, ingen tegnebog) og simulerer handler på 19 virtuelle konti,
der kører side om side. Der sendes aldrig ordrer nogen steder hen.

Ren Python 3.11+ standardbibliotek – ingen afhængigheder.

```
Scout ──▶ Filter ──▶ Analyst ──▶ [pr. strategi] Risk ──▶ Executor
   └───────────── Logger skriver hvert trin til SQLite ─────────────┘
```

## Hurtig start

```bash
cd desk
```

```bash
python3 -m unittest discover -s tests -t .
```

```bash
python3 -m desk backtest
```

```bash
python3 -m desk serve
```

```bash
python3 -m desk run --once
```

```bash
python3 -m desk run
```

Andre kommandoer: `python3 -m desk status` (state.json + afkast pr. strategi) og
`python3 -m desk epoch "ny note"` (nulstil alle konti, se Epochs nedenfor).

Dashboardet ligger på <http://localhost:8787>.

## Data

| | Polymarket | Kalshi |
|---|---|---|
| Markeder | Gamma API `/events` (åbne, sorteret efter 24t-volumen) | trade-api v2 `/events?with_nested_markets=true` |
| Pris (= sandsynlighed) | midt mellem bedste bud/udbud, ellers sidste handel | `yes_bid/ask_dollars`, ellers `last_price` |
| Orderbog | CLOB `/book?token_id=` | `/markets/{ticker}/orderbook` (JA-bud + NEJ-bud → JA-udbud = 1 − NEJ-bud) |
| Volumen | `volumeNum`, `volume24hr` | `volume_fp`, `volume_24h_fp` |
| Lukketid | `endDate` | `close_time` (tidligere `expected_expiration_time` hvis den findes) |
| Udfald | `outcomePrices` = [1,0]/[0,1] når lukket | `status=finalized/settled`, `result=yes/no` |
| Historik (backtest) | CLOB `/prices-history` | `/series/{s}/markets/{t}/candlesticks` |

Begge platforme rate-limites venligt (`*_MIN_INTERVAL` i config) med retry og backoff ved 429/5xx.
Gamma svarer 406 på sider med 100 events, så der hentes 50 ad gangen.

## Agenterne (hårde overdragelser)

Hver agent er en klasse i `desk/agents/` og tager kun det forrige trins outputtype
(`Candidate` → `Tradable` → `Signal` → `OrderIntent` → `Position`). Hver overdragelse
logges i tabellen `handoffs`, og hver afvisning med årsag i `decisions`.

- **Scout** (`scout.py`) – åbne markeder med samlet volumen ≥ `SCOUT_MIN_VOLUME_USD`, 24t-volumen ≥
  `SCOUT_MIN_VOLUME_24H_USD` og lukketid mellem `SCOUT_MIN_HOURS_TO_CLOSE` timer og `SCOUT_MAX_DAYS_TO_CLOSE` dage.
  Fejler én platform, fortsætter den anden.
- **Filter** (`filter.py`) – dræber markeder med for kort tid til afgørelse, igangværende afgørelse
  (UMA-forslag), uklare regler (for kort regeltekst eller fraser som "sole discretion"), pris under 2 % / over 98 %,
  bred spread og tynd orderbog (dybde inden for 5 point på hver side). Henter maks. `FILTER_MAX_BOOKS` orderbøger pr. cyklus.
- **Analyst** (`analyst.py`) – vurderer hvor meget markedet tager fejl, som summen af fire komponenter i
  sandsynlighedspoint:
  - *kalibrering*: historiske udfald af lignende markeder (prisspand × horisont × emne), krympet mod en
    favorit/longshot-prior (`calibration.py`). Data kommer fra afgjorte skyggehandler og fra backtesten.
  - *momentum + volumenspids*: en andel af 24t-kursændringen forventes at fortsætte, forstærket når
    24t-volumen er ≥ 3× det normale.
  - *tidsforfald*: nær afgørelse trækkes favoritter op og longshots ned.
  - *cross-platform*: prisen for samme begivenhed på den anden platform (heuristisk titel-matching, `matching.py`;
    tal i titlerne skal være ens, og lukketiderne højst 3 dage fra hinanden).
  Resultatet er `p_fair`, den foretrukne side, nettokant efter spread og gebyr, tillid og score.
  Rå features (24t-ændring, volumenspids, timer tilbage, dybde, …) sendes med, så strategierne kan bruge dem direkte.
- **Risk** (`risk.py`) – loft pr. marked og pr. emne (politik, sport, krypto, økonomi, teknologi, kultur, vejr, andet)
  i % af kontoværdien; nedskalerer eller afviser ordrer. Lukker positioner før afgørelse, når kursen er gået
  `stop_loss_pct` imod (målt på midtpris), valgfri gevinstsikring og valgfri udgang X timer før lukning.
  Arbitrage-par holdes til afgørelse.
- **Executor** (`executor.py`) – fast % af kontoværdien pr. handel, går gennem orderbogen med en limit på
  bedste pris + `EXEC_MAX_SLIPPAGE`, afviser ved for lav fyldningsgrad, lægger gebyr til
  (Kalshi: `ceil(0,07·C·P·(1−P))`; Polymarket: markedets `feeSchedule`), håndhæver maks. åbne positioner og maks.
  nye handler pr. cyklus. Udbetaling ved afgørelse: 1 eller 0 pr. kontrakt (annullering: 0,5).
- **Logger** (`logger.py`) – SQLite i WAL-tilstand (`data/desk.sqlite3`): runs, handoffs, decisions, markets,
  snapshots, signals, shadow_trades, trades, accounts, equity, calibration_obs, backtest_runs, log.

## Penge er altid procent

Hver konto starter på 100 (= 100 % af startkapitalen), og alle beløb gemmes som procentpoint af startkapitalen.
`EXEC_NOMINAL_BANKROLL_USD` bruges *kun* internt til at oversætte en indsats i % til et antal kontrakter, så
slippage kan regnes mod den rigtige orderbog. Dashboard, ntfy og rapporter viser kun procent; API'et udelader
kontraktantal.

## Strategierne (19)

| Nøgle | Idé |
|---|---|
| `balanceret` | Følger Analystens samlede kant (≥ 3 pp) |
| `favorit_naer` | Køb favoritten (85–97 %) < 48 t før afgørelse |
| `kontraer` | Mod store 24t-ryk uden volumenspids |
| `arbitrage` | Samme begivenhed på begge platforme: JA billigt ét sted + NEJ det andet, når summen < 97 % |
| `momentum` | Med 24t-ryk ≥ 5 pp når volumen spidser |
| `hoej_likviditet` | Kant, men kun dybe og livlige markeder |
| `kraesen` | Kun kant ≥ 8 pp med høj tillid, få store positioner |
| `mange_smaa` | Kant ≥ 1 pp, 0,5 % pr. handel, op til 80 positioner |
| `longshot_salg` | Køb NEJ når JA ≤ 10 % |
| `kalibrering` | Kun kalibreringskomponenten |
| `tidsforfald` | Køb NEJ i 5–30 %-markeder med < 72 t tilbage |
| `volumenspids` | Følg retningen når 24t-volumen ≥ 3× normalt |
| `kun_polymarket`, `kun_kalshi` | Balanceret pr. platform |
| `politik`, `sport` | Balanceret pr. emne, højere emneloft |
| `midterzone` | Kun 30–70 %-markeder |
| `stram_stop` | Balanceret med 15 % stop og 30 % gevinstsikring |
| `kontrol` | Møntkast – basislinjen alle skal slå |

Regelsættene er data (`desk/strategies.py`), og fanen **Regler** viser dem sammen med alle tærskler.
En ny strategi er én linje i `STRATEGIES`.

### Skyggehandler

Hvert marked, Analysten scorer, får én skyggehandel pr. epoch (side og pris som Analysten foreslår), uanset om
nogen strategi handler. De afgøres sammen med markedet og bruges til kalibreringsgrafen og som ny
kalibreringsdata.

### Epochs

`python3 -m desk epoch "note"` starter en ny epoch: alle konti starter forfra på 100 %, og gamle handler bevares
under den forrige epoch (vælg den i dashboardets rullemenu). Brug det, når du ændrer regler eller tærskler.

## Konfiguration

Alle tærskler ligger i `desk/config.py`. Alle kan overskrives med miljøvariabler `DESK_<NAVN>`, fx
`DESK_CYCLE_SECONDS=600` eller `DESK_NTFY_TOPIC=mit-hemmelige-emne`.

## Drift (24/7)

**Trin-for-trin-guide til Oracle Cloud: [docs/ORACLE.md](docs/ORACLE.md).**


```bash
sudo bash deploy/install.sh
```

```bash
sudo systemctl enable --now desk desk-dashboard
```

- `deploy/desk.service` kører loopet (`Restart=always`), `deploy/desk-dashboard.service` kører dashboardet.
- Indstillinger og ntfy-emne i `/etc/desk/desk.env` (se `deploy/desk.env.example`).
- `data/state.json` skrives atomisk efter hver cyklus: epoch, seneste cyklus, tællere, afkast pr. strategi.
  Sandheden ligger i SQLite, så en genstart fortsætter præcis hvor den slap.
- **ntfy**: sæt `DESK_NTFY_TOPIC`. Du får besked ved nye/lukkede handler (samlet pr. cyklus), fejl (højst én pr.
  `NTFY_ERROR_COOLDOWN_MIN`) og en daglig status kl. `NTFY_DAILY_SUMMARY_HOUR_UTC` UTC.
- Dashboardet lytter på `0.0.0.0:8787`. Sæt `DESK_DASHBOARD_PASSWORD` (brugernavn `desk`), hvis porten er åben mod
  internettet, eller sæt `DESK_DASHBOARD_HOST=127.0.0.1` og brug en SSH-tunnel.
- Prisøjebliksbilleder ældre end 30 dage ryddes automatisk.

## Backtest

`python3 -m desk backtest [--days 45] [--max-markets 400] [--offline]`

1. Henter afgjorte binære markeder fra begge platforme i perioden med volumen ≥ `BACKTEST_MIN_VOLUME_USD` og deres
   timepriser (cachet i `data/cache/backtest/`, så `--offline` kan genkøre uden netværk).
2. Beslutningspunkter ved lukning minus 168/72/24/6 timer. Ved hvert punkt bygges et marked med den historiske pris
   og 24t-ændring og en syntetisk orderbog, som går gennem de samme Filter-, Analyst-, strategi-, Risk- og
   Executor-klasser som live.
3. Walk-forward: kalibreringen lærer kun af markeder, der var afgjort før beslutningstidspunktet.
4. Positioner tjekkes hver time mod prisforløbet (stop-loss m.m.) og udbetales ved afgørelse.
5. Rapport i `reports/backtest_<tid>.md` (+ `.json`), resultatet vises i dashboardets Strategier-fane, og
   kalibreringsobservationerne gemmes, så live-Analysten starter med historik.

**Forbehold:** orderbogen er syntetisk, samlet volumen kendes kun bagudrettet, kun afgjorte markeder med høj volumen
er med (survivorship), og hvis udfaldet var kendt før den planlagte lukning, kan der være lækage.
Med få hundrede markeder er tallene pr. strategi støjfyldte. Brug dem til at sortere idéer, ikke som facit.

## Tests

`python3 -m unittest discover -s tests -t .` – kører uden netværk med syntetiske markeder: parsing af begge API'er,
gebyrer, orderbogsgang, alle agenter, strategierne (inkl. arbitrage med to ben), en hel cyklus med afgørelse,
stop-loss og epoch, backtest-simulatoren og alle dashboard-endpoints.

## Filer

```
desk/config.py        alle tærskler
desk/agents/          scout, filter, analyst, risk, executor, logger
desk/sources/         polymarket.py, kalshi.py (kun læsning)
desk/strategies.py    de 19 regelsæt
desk/pipeline.py      én cyklus (orkestrering)
desk/runner.py        24/7-loop, state.json, ntfy
desk/backtest.py      backtest på afgjorte markeder
desk/dashboard.py     HTTP-server + JSON-API; desk/static/index.html er frontenden
deploy/               systemd-units, env-eksempel, install.sh
```
