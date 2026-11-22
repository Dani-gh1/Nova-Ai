# Nova AI

Én app med 22 faner – én pr. papirhandels-bot – og underfaner under hver bot.
Kun papirhandel med fiktive penge.

Al koden ligger i dette ene repo: hubben i `hub/` og alle botter i `bots/` (med hele deres tidligere historik).
Botternes data (papirkonti, databaser) er ikke i git.

| Fane (marked) | Kode |
|---|---|
| Aktier USA (S&P 500) | `bots/Atlas` |
| Forex og råvarer | `bots/Meridian-AI` |
| Nye tokens | `bots/Comet-AI` |
| Prognosemarkeder (Polymarket og Kalshi) | `bots/Delphi-AI` |
| Krypto-futures | `bots/Quasar-AI` |
| Krypto (store kryptovalutaer) | `bots/Orion-AI` |
| Memecoins (nye Solana-memecoins) | `bots/Coins` (kører som sin egen tjeneste, `nova.service`) |
| Globale aktier | `bots/Atlas` (egen bog, `bots.json`) |
| Små krypto | `bots/Orion-AI` (egen bog) |
| Ældre memecoins | `bots/Coins` (`variants/nova-classic.toml`) |
| Obligationer, Metaller, Energi, Tema-aktier, Småaktier, Udbytteaktier, Danske aktier, Regnskabsaktier | `bots/Atlas` (hver sin bog og univers) |
| Staking, DeFi, AI-tokens | `bots/Orion-AI` (hver sin bog og liste af mønter) |
| Valuta (valutapar inkl. nye markeder) | `bots/Meridian-AI` (egen bog) |

Hver bot har de samme fire sider, så alt ser ens ud:

* **Overblik**: nøgletal, en glat graf over alle strategiers afkast og én tabel med alle strategier.
* **Strategier**: et kort pr. strategi med tal og strategiens egne regler (søg og sortér).
* **Desk**: botten som system: status, tragt, hvorfor ting bliver dræbt, og agenternes regler og indstillinger.
* **Handler**: åbne og lukkede handler, filtrérbare pr. strategi.

## Kom i gang

```bash
python3 -m hub run
```

Åbn <http://127.0.0.1:8080>. `run` starter botterne (hver på sin egen port, kun lokalt)
og hubben. `python3 -m hub status` viser hvad der kører. Kun Python 3.11+ standardbibliotek.

## Nova Bot (den primære)

Forsidens **Nova Bot** starter forfra hver dag ved midnat og følger altid den strategi, der er steget mest den dag, og skifter i samme
øjeblik, en anden overhaler den. Den kører hele tiden i hubben og gemmer sin konto i `logs/smart-leader.json`.

Eksempel: strategi 1 stiger til +2 %, så står Nova Bot på +2 %. Strategi 2 stiger til +1,5 %: ingen betydning. Falder strategi 1
derefter, falder Nova Bot med, og i det øjeblik strategi 1 kommer under strategi 2 (ved +1,5 %), skifter Nova Bot til strategi 2 og
står på +1,5 %. Springer en strategi forbi en anden i ét hug (fx når en stor handel lukker), følger Nova Bot springet, som en
rigtig konto ville, og skifter lige bagefter.

**Kassen** er stedet, de rigtige penge lægges ind. Nova Bot (samme regel, men kun strategier, der kan handles for
rigtigt) bestemmer, hvilken strategi pengene følger, og Kassen flytter pengene til det marked og ejer de samme positioner.

| Marked | Botter | Status |
|---|---|---|
| Binance spot | Orion, Orion Mid, Staking, DeFi, AI-token | klar |
| Binance futures | Quasar (gearing højst 2x) | klar; penge flyttes automatisk mellem spot og futures |
| Saxo Bank | Atlas-botterne, Meridian, Valuta | på vej (kræver en Saxo-udviklerkonto) |
| Polymarket / Kalshi | Delphi | på vej (kræver en wallet med USDC på Polygon) |
| Solana / Base | Nova, Nova Classic, Comet | på vej (kræver egne wallets) |

Slå til i `/etc/hub.env`: `HUB_LIVE=dry` (simulering med rigtige kurser) eller `HUB_LIVE=1` (rigtige ordrer) plus
`BINANCE_API_KEY` og `BINANCE_API_SECRET` (kun *Spot & Margin trading*, *Futures* og *Universal Transfer*; aldrig *Withdrawals*).
Penge, du sætter ind hos Binance, registreres med `python3 -m hub kasse indskud <kr>`; `python3 -m hub kasse` viser status.
Penge tages ud med `python3 -m hub kasse udbetal <kr>` (Kassen sælger først). Den stopper aldrig af sig selv.

## Gebyrer

Alle botter regner selv realistiske gebyrer og slip ind i deres papirhandel, så alle tal i hubben er efter gebyrer:

| Botter | Indregnet |
|---|---|
| Atlas og aktie-botterne | Kurtage som hos Interactive Brokers ($0,005 pr. aktie, min. $1, maks. 1 %) + 0,05 % slip + halv spread |
| Orion og krypto-botterne | 0,10 % pr. side (Binance spot) + slip fra den rigtige ordrebog |
| Quasar | 0,05 % taker / 0,02 % maker (Binance futures) + funding |
| Meridian og Valuta | Spread som hos en forex-broker + swap over natten |
| Comet | DEX-gebyr 0,25–0,3 % + gas + prisvirkning |
| Nova og Nova Classic | Jupiter-pris med puljegebyr og prisvirkning + $0,05 pr. swap |
| Delphi | Kalshis/Polymarkets egne gebyrer + køb til udbud og salg til bud |

Teksten står under hver strategis regler (`fees.text` i `bots.json`).

## Alle botter

Første fane er **Overblik** for alle botter: en tabel over, om de kører, og en tabel med hver bots bedste strategi.

## Hjemmeside, hjemmeskærm og download

* **Indgang:** `index.html`, `download/` og `assets/` i roden er en lille hjemmeside (GitHub Pages, ligesom Flower) på <https://dani-gh1.github.io/Nova-Ai/>. Skriv serverens
  adresse én gang, så åbner den dit dashboard med ét tryk (adressen gemmes kun i din egen browser). Et delbart link:
  `…/Nova-Ai/?server=<adresse>`. Siden `…/Nova-Ai/download/` har installation, zip og trin til hjemmeskærmen.
* **Som app på telefonen:** åbn dashboardet, og vælg *Føj til hjemmeskærm* (iPhone: Del-knappen i Safari, Android: menuen ⋮).
  Dashboardet har eget ikon (`hub/static/`), manifest, plads til notch og hjemmebjælke og en menu i bunden på telefon.
* **Logo og ikoner:** kilderne er `hub/static/*.svg`. `node tools/icons.js` og `node tools/og.js` tegner PNG-filerne igen
  (kræver `playwright`).
* **GitHub Pages slås til** under *Settings → Pages → Deploy from a branch → `main` → `/ (root)`* (samme valg som Flower).

## Kør 24/7 uden at gøre noget

Hubben skal køre på en maskine, der altid er tændt, fx en Linux-server hos Oracle Cloud (en MacBook går i dvale).
Log ind på serveren med SSH og installér én gang:

```bash
git clone https://github.com/Dani-gh1/Nova-Ai.git && cd Nova-Ai && ./deploy/install.sh
```

Scriptet sætter alt op og skriver adressen og en adgangskode ud til sidst. På en **Mac** virker den samme kommando
(i Terminal, uden sudo): hubben starter ved login via launchd, genstarter sig selv og holder Macen vågen, mens låget er åbent.
Kunne autostart ikke indlæses, skriver scriptet fejlen ud, og du kan i stedet køre `./deploy/run-mac.sh` i et Terminal-vindue (lad det stå
åbent; Macen holdes vågen, så længe låget er åbent). Nova slås fra på en Mac, så der ikke opstår en anden papirkonto; sæt `HUB_EXTERNAL_NOVA=<serverens adresse>:8787` i
`~/Library/Application Support/Nova AI/nova-ai.env` for at se serverens Nova. På serveren gør den selv:

* **Starter ved boot** og genstarter sig selv, hvis hubben går ned (systemd, `Restart=always`).
* **Vagthund:** en bot, der går ned, startes igen efter 5 sekunder (pausen vokser, hvis den bliver ved, højst 5 min.).
  En bot, hvis dashboard ikke svarer i 5 min., selv om processen lever, bliver dræbt og startet igen.
* **Opdaterer sig selv** hvert kvarter fra GitHub (hubben, botterne og Nova). Hver opdatering køres først gennem
  testene; består de ikke, rulles den tilbage, og intet genstartes. Log: `sudo journalctl -u nova-ai-update`.
* **Nova** kører allerede som egen tjeneste (`desk`). Scriptet finder den, starter ingen ekstra kopi og lader
  hubben vise den. Den får også opdateringer og genstart ved nye versioner.
* Logs for hver bot: `logs/<bot>.log` (roteres ved 5 MB). Hubbens: `sudo journalctl -u nova-ai -f`.

**Adgangskode til dashboardet:** slå den fra eller til med `./deploy/password.sh off` (ingen adgangskode), `local` (ingen adgangskode, kun
denne maskine), `on` (ny adgangskode) eller `show`. Scriptet genstarter hubben.

Indstillinger ligger i `/etc/nova-ai.env` (adgangskode, port, `OANDA_TOKEN` til Atlas og Meridian). Ret filen og kør
`sudo systemctl restart nova-ai`. Tjek status: `systemctl status nova-ai`.

## Sådan virker det

* Hubben henter hver bots data fra dens eget dashboard og omsætter den til ét fælles format (`hub/adapters.py`),
  så alle botter vises med samme design (`hub/shell.html`). Botternes egne dashboards er stadig tilgængelige under
  `/b/<bot>/` (link på Desk-siden).
* Adresser kan deles: `#quasar/regler`.
* Hubben åbner kun på 127.0.0.1. Skal den åbnes på netværket: sæt `HUB_PASSWORD` og start med `--host 0.0.0.0`.
  Har et dashboard sit eget login (fx Delphi), sæt `HUB_UPSTREAM_AUTH_DELPHI=bruger:kode`.

## Varianter: nye botter på samme kode

En bot kan genbruge en anden bots kode og kun have egne indstillinger. I `bots.json` giver `launch.overrides` (`{modul: {indstilling: værdi}}`)
nye værdier, før botten starter (`+NAVN` lægger til en liste/mængde, `-NAVN` fjerner strategier med de navne), og `launch.env` giver egne filer,
så papirbogen ikke blandes. `kind: "script"` gør det samme for en bot, der startes med et script (Atlas). Eksempler: Atlas Global, Orion Mid og Nova Classic.

## Tilføj en ny bot

Tilføj en post i `bots.json` med `repo`, `port`, `launch`, `adapter` og `hide`, og skriv en adapter i `hub/adapters.py`, hvis botten har en anden datastruktur end de eksisterende (`forex`, `delphi`, `quasar`, `orion`, `comet`, `nova`). Læg botens kode i `bots/<navn>/`.

## Test

```bash
python3 -m unittest discover -s tests -t .
```

## Flyt til en ny server

Kør på din Mac (den har ssh-nøglen til begge servere):

```bash
./deploy/migrate.sh <gammel-server> <ny-server> [ssh-nøgle]
```

Scriptet stopper den gamle server, kopierer hubben, alle botter, deres data, `/etc/nova-ai.env` og Nova direkte over, slår alle botter til,
installerer hubben og Tailscale på den nye og slår den gamle fra (så ingen bot får to papirkonti). Den gamle servers data bliver liggende
som en ekstra kopi. `HUB_DRY_RUN=1` viser kun, hvad der ville ske. Alle 22 botter kræver ca. 6 GB hukommelse eller mere (fx Oracle Ampere A1).

## Fra hvor som helst, når som helst

Kør på den maskine, hvor hubben kører (helst en server, der altid er tændt). Vælg, hvem der skal kunne åbne det:

```bash
./deploy/public.sh private
```

**Anbefalet.** Dashboardet kan åbnes fra hvor som helst, men **kun fra dine egne enheder** med Tailscale-appen (samme konto). Ingen
adgangskode nødvendig, og ingen andre kan åbne det. Adressen er en fast `https://<maskine>.<dit-net>.ts.net`.

```bash
./deploy/public.sh on
```

Åbent for hele internettet (også computere uden Tailscale), beskyttet af en lang adgangskode, som scriptet laver. Efter 5 forkerte forsøg spærres
adressen (først 1 minut, derefter længere, højst 15). Gætning kan ikke snydes med opdigtede `X-Forwarded-For`-headere.

```bash
./deploy/public.sh open --yes
```

Åbent for hele internettet **uden adgangskode**. Alle, der kender adressen, kan se dashboardet (kun læsning, kun papirhandel). Der er en
hastighedsgrænse på 240 forespørgsler i minuttet pr. besøgende. `.ts.net`-adresser kan findes via certifikatlister, så vælg hellere `private`.

Bruger en anden tjeneste allerede hovedadressen (port 443), fx din Nova, rører scriptet den ikke. Dashboardet får i stedet port 8443 (eller 10000), og adressen
bliver fx `https://<maskine>.ts.net:8443`. `off` fjerner kun hubbens egen adresse.

Kræver Tailscale administratorrettigheder ("Access denied"), kører scriptet kommandoen igen med `sudo`. Du kan i stedet give din bruger lov én gang med
`sudo tailscale set --operator=$USER`.

`./deploy/public.sh off` slår det hele fra, og `status` viser tilstand og adresse. Første gang skal du tillade HTTPS (og til `on`/`open` også Funnel) i
din Tailscale-konto; scriptet siger hvordan. Brug Pages-linket `…/Nova-Ai/?server=https://<adresse>` til at gemme adressen i telefonen.

## Tjek og ret alt på serveren: `verify.sh`

```bash
./deploy/verify.sh
```

Svarer ja eller nej på fire spørgsmål og **retter det, der kan rettes**: 1) kører dashboardet 24/7 (starter ved boot, kører som tjeneste, genstarter
sig selv, opdaterer sig selv), 2) kører alle botter uden problem (med årsagen for dem, der ikke gør), 3) er det uafhængigt af dine enheder (kører på
serveren under systemd, Tailscale starter ved boot), 4) kan alle komme ind, hvor de end er (Funnel slået til, og adressen svarer). Til sidst skriver det
adressen. Standard er åbent for alle uden adgangskode; `VERIFY_ACCESS=on` giver adgangskode, `VERIFY_ACCESS=private` kun dine egne enheder.
`--no-fix` tjekker kun.

## Mac: "unable to get local issuer certificate"

Python fra python.org leveres uden rod-certifikater, så alle HTTPS-kald fejler, selv om netværket er i orden. Så kan ingen bot hente
data og dermed heller ikke handle. `python3 -m hub run` (og `hub doctor`) opdager det selv og bygger en certifikatfil ud fra Macens
egen nøglering, som botterne arver (`SSL_CERT_FILE`). Du kan i stedet køre `Install Certificates.command` i Pythons programmappe.

## Hvis en bot ikke scanner: `hub doctor`

```bash
python3 -m hub doctor
```

Tester alle adresser, botterne henter data fra, og forklarer på dansk, hvad der er galt: børsen er spærret i dit land (HTTP 451/403),
DNS finder ikke adressen, eller netværket/routeren/et antivirusprogram overvåger forbindelsen, så certifikatet afvises
(*self-signed certificate in certificate chain*). Den nævner også, hvem der har udstedt certifikatet. Oversigten viser desuden den
seneste fejllinje under hver bot.

## Datakilder og nøgler

Alle botter kan scanne markedet uden nøgler eller opsætning:

* **Atlas** (amerikanske aktier og ETF'er) bruger Yahoo Finance, Nasdaq og Wikipedia uden nøgler. Den handler kun, mens NYSE er åben (15:30–22:00
  dansk tid på hverdage) og sover ellers. Det er ikke en fejl, og oversigten viser "Venter på børsen" med næste åbning.
* **Meridian** (forex og råvarer) bruger Yahoo Finance, hvis ingen nøgle er sat. Sæt `OANDA_TOKEN` i
  `/etc/nova-ai.env` (Mac: `~/Library/Application Support/Nova AI/nova-ai.env`) for rigtig bid/ask fra OANDA. Så skifter den selv.
* **Quasar** (krypto-futures) prøver Binance og skifter selv til Bybit, hvis Binance er spærret i dit land eller hos din
  server-udbyder.
* Overblikket viser for hver bot, hvornår den **sidst scannede**. Står en bot stille i 20 minutter, vises "Scanner ikke",
  og efter 45 minutter genstarter vagthunden den.
