# Papirdesk

Papirhandels-bot ("desk") til de store kryptovalutaer på Binance (BTC, ETH, SOL og
ca. 30 af de mest omsatte USDT-par).

> **KUN PAPIRHANDEL.** Botten bruger fiktive penge. Den bruger ingen API-nøgler og
> har ingen kode, der kan sende ordrer. Den læser kun Binances offentlige markedsdata.

- Ren Python 3.11+ standardbibliotek, ingen afhængigheder.
- Seks agenter med hårde overdragelser: **Scout → Filter → Analyst → Risk → Executor**,
  og **Logger** skriver alt til SQLite.
- 18 strategier kører side om side på hver sin virtuelle konto, plus skyggehandler.
- Dashboard på dansk (port 8787). Papirpenge vises altid i procent.
- Kører 24/7 som systemd-service. Papirbogen gemmes i `state.json`.

## Kommandoer

Alle kommandoer køres fra projektmappen.

```bash
python3 -m unittest
```

```bash
python3 -m desk scan
```
(én tør gennemkørsel Scout → Filter → Analyst → Risk mod live data, uden handler)

```bash
python3 -m desk run
```
(botten + dashboard; det er den kommando systemd bruger)

```bash
python3 -m desk dashboard
```
(kun dashboardet, læser `state.json` og `desk.sqlite3`)

```bash
python3 -m desk status
```

```bash
python3 -m desk backtest --days 90
```

```bash
python3 -m desk backtest --days 60 --symbols BTCUSDT,ETHUSDT,SOLUSDT --interval 1h
```

```bash
python3 -m desk backtest --days 30 --top 20
```

```bash
python3 -m desk notify-test
```

## Arkitektur

| Agent | Fil | Ind → ud | Opgave |
|---|---|---|---|
| Scout | `desk/agents/scout.py` | 24t-ticker → `Candidate` | USDT-par over volumengrænsen, top N; udelukker stablecoins, tokeniserede aktier og gearede tokens |
| Filter | `desk/agents/filter.py` | `Candidate` → `Approved` | Afviser for stor spread, for tynd orderbog (±1 %) og udsving > X % på 1 time (5m-lys) |
| Analyst | `desk/agents/analyst.py` | `Approved` → `Scored` | Score 0-100 pr. profil: momentum 1t/4t/24t/72t, relativ volumen, SMA50/SMA200-trend, volatilitet, RSI, breakout og BTC-trend som markedsfilter |
| Risk | `desk/agents/risk.py` | `Scored` → `EntryPlan`; positioner → `ExitSignal` | Strategiregler, stop under indgang, trailing stop der slås til ved gevinst, tidsgrænse. Kurs tjekkes hvert sekund |
| Executor | `desk/agents/executor.py` | `EntryPlan` → position | Fast % af kontoen, slippage ved at "gå" orderbogen, gebyr pr. side, maks. åbne positioner, karantæne efter salg |
| Logger | `desk/agents/logger.py` | alt → SQLite | Tabellerne `events`, `scores`, `trades`, `equity`, `backtests` |

Hver agent tjekker typen på det, den modtager. Får den noget andet end den forrige
agents output, stopper den med `TypeError`. Det er de hårde overdragelser.

`desk/engine.py` binder agenterne sammen. En scan-tråd kører hele kæden hvert
`SCAN_INTERVAL_SECONDS` (standard 300 s). Hovedløkken henter hvert sekund kurser for
alle åbne positioner og skyggehandler, og Risk og Executor lukker dem ved behov.

## Konfiguration: `config.py`

Alle tærskler ligger i `config.py`: volumengrænse, spread, dybde, udsving, stop,
trailing, gebyr, positionsstørrelse, karantæne, intervaller, ntfy, dashboard og
listen `STRATEGIES`.

**Risk er tilpasset store coins.** Standarden er 2,5 % stop, trailing fra +2 % og
1,2 % afstand. Det er strammere end man ville bruge til memecoins, hvor typiske
værdier er 8-15 %. Strategierne har stop mellem 1,5 og 4 %. Køb og hold BTC har
ingen stop.

### Nulstilling med epoch

Hæv `EPOCH` i `config.py` og genstart servicen. Alle konti starter så forfra på samme
tid. Den gamle papirbog gemmes som `state.json.epoch<N>.json`, og SQLite-rækker er
mærket med epoch, så historikken bevares. Hvis du tilføjer eller ændrer strategier,
så hæv også epoch, ellers starter de nye senere end de andre.

## Strategier (18)

Balanceret, Trendfølger, Langsom trend, Momentum, Hurtigt momentum, Breakout,
Breakout + volumen, Mean reversion, Dyb mean reversion, Kræsen, Aggressiv, Mange små,
Lav volatilitet, Høj volatilitet, Uden BTC-filter, Kun top 5, Kun BTC og ETH, og
Køb og hold BTC (benchmark). Detaljerne står i `config.py` og under fanen **Regler**
i dashboardet.

**Skyggehandler:** hver coin, der bliver scoret, får en skyggeposition med
standard-risk. Den rører ingen konti. Dashboardet viser resultatet pr. scoreinterval,
så du kan se, om høj score faktisk giver bedre handler.

## Backtest

`python3 -m desk backtest` henter historiske klines (cachet i `data/`) og genspiller
alle strategier med præcis samme Analyst-, Risk- og Executor-kode som live.

Forskelle fra live:
- Der findes ingen historisk orderbog. Spread- og dybdefilteret springes derfor over,
  og slippage er fast (`BACKTEST_SLIPPAGE_PCT`). Udsvingsfilteret bruges stadig.
- Kursforløbet inden for et lys simuleres som open → low → high → close (røde lys:
  open → high → low → close). Stop fyldes på stopkursen, eller på open ved gap.
- Beslutninger træffes ved lysets lukning. Profilerne er afstemt til `1h`.

Resultaterne gemmes også i tabellen `backtests`.

## Drift på Linux (systemd)

```bash
sudo useradd --system --home /opt/papirdesk --shell /usr/sbin/nologin papirdesk
```

```bash
sudo mkdir -p /opt/papirdesk && sudo cp -r config.py desk deploy /opt/papirdesk/ && sudo chown -R papirdesk:papirdesk /opt/papirdesk
```

```bash
sudo cp /opt/papirdesk/deploy/papirdesk.service /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now papirdesk
```

```bash
journalctl -u papirdesk -f
```

Servicen har `Restart=always` og `StartLimitIntervalSec=0`, så den altid genstartes.
`state.json` skrives atomisk (temp-fil + `os.replace`), så papirbogen kan gendannes
efter nedbrud eller strømsvigt. En ødelagt fil gemmes som `state.json.broken.<tid>`.

Kræver Python 3.11 eller nyere. Tjek med `python3 --version`.

**Binance-adgang:** `BINANCE_BASE_URLS` prøver `api.binance.com` først og derefter
`data-api.binance.vision`, som er Binances rene markedsdata-spejl og også virker der,
hvor hoveddomænet er geoblokeret (HTTP 451).

### Dashboard fra mobilen

Dashboardet lytter som standard kun på `127.0.0.1:8787`, fordi det ikke har login.
Fra telefonen kan du enten:
- bruge en SSH-tunnel: `ssh -L 8787:127.0.0.1:8787 bruger@server` og åbne
  `http://localhost:8787`, eller
- sætte `DASHBOARD_HOST = "0.0.0.0"` og kun åbne porten på et privat netværk eller
  en VPN som Tailscale eller WireGuard. Læg den ikke åbent på internettet.

### Notifikationer (ntfy)

Sæt `NTFY_URL = "https://ntfy.sh/<dit-hemmelige-emne>"` i `config.py` og test med
`python3 -m desk notify-test`. Der sendes kun ved køb, salg, stop og nedbrud, styret
af `NTFY_EVENTS`. Med 18 strategier kan der komme mange beskeder. Fjern fx `"buy"`
fra `NTFY_EVENTS`, hvis det bliver for meget.

Nedbrud sendes på to måder. Uventede exceptions sendes, før processen dør. En urent
afsluttet tidligere kørsel opdages ved næste start, og så sendes "genstartet efter
nedbrud".

## Filer

| Fil | Indhold |
|---|---|
| `state.json` | Papirbogen: konti, positioner, karantæner, skygger og sidste scan |
| `desk.sqlite3` | Logger-databasen (WAL). Events og scores ryddes efter `LOG_RETENTION_DAYS`; handler og egenkapital beholdes |
| `data/` | Cache med historiske klines til backtest |
