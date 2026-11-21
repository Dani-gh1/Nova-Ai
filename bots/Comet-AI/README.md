# Papirdesk

En papirhandels-bot ("desk") til nye og små tokens på **Base**, **Ethereum** og **BNB Chain**
(Uniswap, Aerodrome og PancakeSwap).

> **KUN papirhandel med fiktive penge.** Der findes ingen tegnebogsnøgler, ingen signering og
> ingen on-chain-transaktioner nogen steder i koden. Botten læser kun offentlige data.

Ren Python 3.11+ og kun standardbiblioteket. Ingen `pip install`.

## Hurtig start

```bash
python3 -m unittest discover -s tests -t .
```

```bash
python3 -m desk scan --chain base
```

```bash
python3 -m desk run
```

Dashboardet ligger derefter på `http://<server>:8787`.

| Kommando | Hvad den gør |
|---|---|
| `python3 -m desk run` | Starter desken: scouting, kurstjek hvert sekund, dashboard på :8787 |
| `python3 -m desk dashboard` | Starter kun dashboardet (læser databasen) |
| `python3 -m desk scan [--chain base]` | Tørkørsel: én runde Scout → Filter → Analyst uden handler og uden database |
| `python3 -m desk epoch --note "ny test"` | Nulstilling: ny epoch med friske virtuelle konti |
| `python3 -m desk status` | Viser `state.json` og afkast pr. strategi (i procent) |
| `python3 -m desk backtest [--pools 25] [--chain eth]` | Historisk backtest (se nedenfor) |

## Arkitektur: seks agenter med hårde overdragelser

```
 Scout ──Candidate──▶ Filter ──FilteredCandidate──▶ Analyst ──ScoredCandidate──▶ Risk ──TradePlan──▶ Executor
                        │                                                         ▲  │                   │
                        └──Kill                                                    │  └──ExitOrder──────▶│
                                                    kurs hvert sekund ─────────────┘                      │
 Logger ◀──────────────── hver overdragelse, drab, score, handel, egenkapital ────────────────────────────┘
```

Hver agent tager præcis én type ind og afleverer præcis én type (`desk/models.py`). Ingen agent
kalder en anden; orkestratoren (`desk/app.py`) flytter objekterne, og **Logger** skriver hver
overdragelse i tabellen `handoffs` i SQLite (`data/desk.sqlite3`).

| Agent | Fil | Ansvar |
|---|---|---|
| **Scout** | `desk/scout.py` | Par 30 min–72 t gamle med markedsværdi i intervallet, på hver kæde og kun på tilladte DEX'er. Kilder: GeckoTerminal (nye puljer, trending og top-volumen, med unikke købere) og DexScreener (likviditet og markedsværdi, profiler). Par der er for unge, huskes på en watchlist og hentes igen senere. |
| **Filter** | `desk/filter.py` | Dræber ved honeypot, høj købs-/salgsskat, ejerskab der ikke er afgivet, LP der ikke er låst/brændt, for lav likviditet eller volumen, for høj koncentration hos de største holdere, udvikler med tidligere rugs (GoPlus og egen hukommelse) eller +80 % på 1 time. Billige markedstjek kører før GoPlus-kaldet. |
| **Analyst** | `desk/analyst.py` | Score 0–100: momentum, unikke købere, køb/salg, holdervækst, likviditet/markedsværdi. Vægtene står i `config.ANALYST_WEIGHTS`. |
| **Risk** | `desk/risk.py` | Ved indgang: strategiens porte (score-tærskel, likviditet, alder, købspres), stop, trailing stop og gevinstmål → `TradePlan`. Bagefter: tjekker kursen hvert sekund (DexScreener, op til 30 par pr. kald, med rotation og GeckoTerminal som reserve) → `ExitOrder`. Har også en rug-udgang ved forsvundet likviditet eller kurskollaps; den markerer samtidig udvikleren. |
| **Executor** | `desk/executor.py` | Papirfyld: fast procent af kontoen, begrænset af maks. prisvirkning ud fra poolens likviditet (constant-product, `desk/amm.py`), DEX-gebyr og gas pr. kæde. Respekterer maks. åbne positioner, dubletter og karantæne efter salg. |
| **Logger** | `desk/logger.py` | Den eneste agent der skriver til databasen. |

## Strategitest

`config.STRATEGIES` indeholder **18 regelsæt**, fx Basis, Streng, Løs, Stram stop, Gevinst 2x,
Scalp, Lang hold, Dyb likviditet, Unge par, Købspres, Lav prisvirkning, Billig gas og Kombi.
Hvert regelsæt har **sin egen virtuelle konto pr. kæde** (18 × 3, dog kun Base+BNB for "Billig gas"),
så de kører side om side, og kæderne kan sammenlignes direkte.

* **Epoch**: `python3 -m desk epoch` lukker åbne papirpositioner administrativt og opretter friske konti.
  Gamle epochs kan stadig ses i dashboardet via vælgeren øverst.
* **Skyggehandler**: *alle* scorede coins, også dem under alle tærskler, får en skyggeposition
  (fast nominel værdi, fast stop/trailing og maks. holdetid, se `SHADOW_*`). Skyggerne påvirker ingen konto.
  Fanen **Regler** viser deres resultat pr. score-interval, så tærsklerne kan kalibreres.

## Dashboard (port 8787)

Dansk og mobilvenligt, med lyst/mørkt tema efter systemets indstilling. Fanerne er:

* **Oversigt**: gennemsnitligt afkast, "Afkast over tid" (blød, monoton kurve) for alle konti og pr. kæde,
  tragten Scout → Executor, hyppigste drab og seneste hændelser.
* **Strategier**: kurve for top 5, tabel med strategi × kæde og kort pr. strategi og kæde.
* **Handler**: åbne og lukkede papirhandler samt skyggehandler.
* **Regler**: alle tærskler, de 18 regelsæt, skyggehandler pr. score og drab pr. årsag.

**Papirpenge vises altid i procent** (afkast, resultat pr. handel, positionsstørrelse som % af kontoen).
API'et udleverer aldrig kontant-, størrelses- eller P&L-beløb i dollars; en test håndhæver det.
Markedstal (likviditet, markedsværdi, volumen) må stå i dollars.

Dashboardet har ingen login og lytter som standard på `0.0.0.0`, så det kan nås fra mobilen.
Sæt `DESK_HOST=127.0.0.1` og brug SSH-tunnel/VPN, hvis serveren er offentlig. Dashboardet er skrivebeskyttet.

## Drift 24/7

```bash
sudo useradd --system --home /opt/papirdesk papirdesk
```

```bash
sudo cp -r . /opt/papirdesk && sudo chown -R papirdesk: /opt/papirdesk
```

```bash
sudo cp deploy/papirdesk.service /etc/systemd/system/ && sudo systemctl daemon-reload
```

```bash
sudo systemctl enable --now papirdesk
```

```bash
journalctl -u papirdesk -f
```

* `Restart=always`. Alt ligger i SQLite, så en genstart fortsætter med de samme åbne papirpositioner.
* `data/state.json` skrives atomisk hvert 10. sekund (epoch, seneste scouting, seneste kurstjek, tællere).
* **ntfy**: sæt `DESK_NTFY_TOPIC` (og evt. `DESK_NTFY_SERVER`). Der sendes besked ved papirkøb og -salg,
  ved start og med en opsummering hver 6. time.

Miljøvariabler: `DESK_DATA_DIR`, `DESK_HOST`, `DESK_PORT`, `DESK_CHAINS` (fx `base,bsc`),
`DESK_NTFY_TOPIC` og `DESK_NTFY_SERVER`. Alt andet står i `config.py`.

## Historisk backtest

```bash
python3 -m desk backtest --pools 25
```

Backtesten finder puljer, der er 6 t–7 dage gamle (GeckoTerminal nye og trending), og henter deres
5-minutters OHLCV og nuværende GoPlus-data. Data caches i `data/backtest_cache/`. Derefter afspilles
alle regelsæt pr. kæde i én fælles tidslinje med de samme byggesten som live: `Filter.market_check`
og `security_check`, Analyst-komponenterne, `risk.check` og `amm`-matematikken. Resultatet skrives
til `data/backtest_result.json`, og datasættet kan genbruges med `--dataset data/backtest_dataset.json`.

**Begrænsninger, så tallene ikke læses for optimistisk:**

* *Survivorship- og udvælgelsesbias*: trending-puljer er pr. definition dem, der overlevede.
* *Look-ahead*: kontraktsikkerheden er GoPlus' **nuværende** vurdering.
* Likviditet og markedsværdi tilbage i tiden er **skønnet** ud fra nuværende værdier
  (likviditet ∝ √kurs, markedsværdi ∝ kurs).
* Unikke købere, køb/salg og holdervækst findes ikke historisk. Scoren renormaliseres derfor over
  momentum og likviditet/markedsværdi, og "Købspres"-porten ignoreres.
* Stop tjekkes mod candle-lav, før top og gevinstmål tjekkes (konservativt), med 5-minutters opløsning
  i stedet for 1 sekund.

Live-papirhandel over tid giver derfor et mere retvisende billede. Backtesten bruges til at sammenligne
regelsæt relativt.

## Datakilder (gratis og offentlige)

* DexScreener: `tokens/v1`, `latest/dex/pairs`, token-profiler/boosts (ca. 250 kald/min).
* GeckoTerminal: `new_pools`, `trending_pools`, `pools`, `pools/multi`, `ohlcv` (ca. 25 kald/min).
* GoPlus: `token_security` og `address_security` (ca. 25 kald/min).

Grænserne håndhæves pr. vært i `desk/http.py`.

## Kendte afvejninger

* `FILTER_KILL_ON_UNKNOWN_LP=True`: Uniswap v3/v4 og Aerodrome Slipstream har NFT-LP, hvor GoPlus ofte
  ikke kan vise låsning. De dræbes derfor som standard. Det er sikkert, men frasorterer mange puljer.
  Slå det fra i `config.py` for at se dem.
* `FILTER_KILL_ON_UNKNOWN_TAX=True`: helt nye tokens, hvor GoPlus ikke kan simulere skat, dræbes
  midlertidigt og vurderes igen efter 15 min.
* Udviklertjekket bruger GoPlus' `honeypot_related_address` og `number_of_malicious_contracts_created`
  plus egen hukommelse (`dev_rugs`). GoPlus' `phishing_activities` og `stealing_attack` er udeladt,
  fordi de også markerer almindelige adresser.
* Prisvirkning er en constant-product-tilnærmelse ud fra den rapporterede likviditet.

## Filer

```
config.py            alle tærskler, kæder og de 18 regelsæt
desk/models.py       overdragelsestyperne
desk/{scout,filter,analyst,risk,executor,logger}.py   de seks agenter
desk/app.py          orkestrator og løkker (scout, risk 1 s, husholdning)
desk/amm.py          prisvirkning, gebyr
desk/sources.py      DexScreener, GeckoTerminal, GoPlus
desk/dashboard.py    HTTP-server og dansk single-page-dashboard
desk/backtest.py     historisk backtest
desk/notify.py       ntfy
desk/state.py        state.json
deploy/papirdesk.service
tests/               unittest (ingen netværk)
```
