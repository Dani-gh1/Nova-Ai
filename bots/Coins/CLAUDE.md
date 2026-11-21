# Memecoin-desk: kontekst til nye Claude-sessioner

Denne fil indlæses automatisk af Claude Code. Den samler det vigtigste fra den første
session, så en ny session kan fortsætte uden at starte forfra.

## Hvad projektet er
En Solana-memecoin-desk, **kun papirhandel**. Seks agenter med hårde overdragelser:
Scout → Filter → Analyst → Risk → Executor, og Logger skriver alt.
Det er ren Python-stdlib (3.11+), uden afhængigheder. Se `README.md` for kommandoerne.

- Scout: par mellem 30 min og 72 t gamle, mcap $300k–$20M (DexScreener, GeckoTerminal, Jupiter).
- Filter dræber, hvis: mint/freeze ikke er revoked, LP ikke er brændt/låst (95 %), liq < $40k,
  1h-vol < $50k, top10 > 25 %, dev > 3 % eller tidligere rugs, bundle > 10 %, eller +80 % på 1 time.
- Analyst scorer 0–100 (momentum, unikke købere, køb/salg, holdervækst, liq/mcap, smart wallets).
  Tærsklen er **73**. Brugeren valgte den efter en backtest, hvor alt under 73 tabte.
- Executor: fast 1 % af kontoen pr. handel, maks. 1 % prisvirkning, maks. 3 åbne positioner,
  og 120 min karantæne efter salg. Scanning hvert 20. sekund.
- Risk: stop −6 % under indgang, trailing stop slås til ved +8 % og ligger 6 % under toppen.
  Kursen tjekkes hvert sekund.
- Alle tærskler står i `desk/config.py`.
- Skyggehandel (`desk/shadow.py`, fra 2026-11-16): følger hver scoret coin (også under 73) og
  coins afvist kun for +80 % med de rigtige regler, uden at røre papirkontoen. Bruges til at vælge
  tærskel og filtre med mere data. Vises under fanen Handler.
- Skyggen har også et bredt vindue ($100k–$20M, 10 min–7 dage): coins uden for det rigtige vindue
  skyggehandles kun, så vi kan se, om et bredere rigtigt vindue ville betale sig.

## Sådan køres den
- `python3 -m desk run --dashboard 8787` (brug `python3`, ikke `python`, på brugerens Mac).
- Dashboardet ligger på `http://127.0.0.1:8787` (dansk UI, 4 faner).
- Papirbogen gemmes portabelt i `runs/state.json` og gendannes, når databasen er tom.
- Testene køres med `python3 -m unittest discover -s tests` (96 tests).
- **Desken kører 24/7 på brugerens Oracle Cloud-server** (Ubuntu 24.04, offentlig IP 89.168.96.169,
  bruger `ubuntu`, mappe `~/Coins`) som systemd-servicen `desk` (`Restart=always`). Brugeren logger
  ind fra Macen med `ssh -i ~/.ssh/oracle ubuntu@89.168.96.169`. Opdatering på serveren:
  `git checkout -- runs/state.json && git pull --ff-only origin claude/gallant-planck-vsfkxd &&
  sudo systemctl restart desk`. Log: `sudo journalctl -u desk -n 20`.
- Dashboardet hedder **Nova AI** (hed før Moonshot) og ligger som app på brugerens iPhone-hjemmeskærm. Telefonen
  når serveren via Tailscale (serverens `100.x`-IP, port 8787). Oracle-firewallen åbner kun for
  `tailscale0`.
- **Start ikke desken i cloud-sessioner eller på Macen.** Brugeren vil kun have én papirkonto, og
  den ligger på serveren. Serverens nye handler ligger i dens database, ikke på GitHub.
- Notifikationer til telefonen via ntfy-appen (`desk/notify.py`): miljøvariablen `NTFY_TOPIC` sættes
  med `sudo systemctl edit desk` ligesom `JUPITER_API_KEY`. Sendes kun ved køb, salg, stop og nedbrud (brugerens ønske),
  og ved køb og salg i hver af strategierne (S1–S20).
- **Strategitest (midlertidig):** `desk/strategies.py` kører **20 regelsæt** på virtuelle konti side om side,
  hver på sin egen kendte memecoin-idé (omskrevet 2026-11-19, de tidligere 17 var mest varianter af "Kræsen"):
  1 Momentum (score ≥ 75, 1t-volumen ≥ $150k), 2 Scalper (+6 % mål, −3 % stop, ude efter 20 min), 3 Swing 24 t,
  4 Månestråle (0,5 %, stop −20 %, trailing +50 %/25 %), 5 Nylancering (kun de første 60 min, +30 % mål),
  6 Tidlig bølge (første 6 t, $100k–$3M), 7 Etableret ($5–20M, liq ≥ $500k), 8 Dybe pools (liq ≥ $300k, impact ≤ 0,3 %),
  9 Volumeneksplosion (1t-vol ≥ $500k, højst +60 %), 10 Smart money (score ≥ 80), 11 Rolig akkumulering (højst +8 % på 1 t.),
  12 Ikke FOMO (højst +30 %, drift ≤ 1,5 %), 13 Risiko 1:2 (−5 %/+10 %), 14 Risiko 1:3 (−4 %/+12 %), 15 Sikker først
  (LP 100 %, top 10 ≤ 15 %), 16 Mange små, 17 Høj overbevisning (8 %, score ≥ 78, pause efter 2 tab), 18 Kontrol (ingen
  scoretærskel: viser, om scoren hjælper), 19 Diamanthænder (stop −35 %, 24 t.), 20 Tæt trailing (+4 %/3 %).
  Alle ligger inden for skyggens brede vindue ($100k–$20M, 10 min–7 dage). Se `desk/strategies.py`.
  Dashboardet viser KUN strategitesten, fordi `dashboard_view = "strategies"` i `desk/config.py`. Når brugeren siger til,
  skiftes det tilbage ved at sætte `dashboard_view = "full"` (det normale dashboard er urørt i `dashboard.html` og kan
  altid ses på `/full`). Spørg, om strategitesten (`strategy_test`) også skal slås fra.
  Alle strategier nulstilles samtidig ved at ændre `strategy_epoch` i config (sidst: `2026-11-19-20strategier`).
  Den rigtige papirkonto påvirkes ikke af strategitesten.
- Der arbejdes på branchen `claude/gallant-planck-vsfkxd`.

## Brugerens ønsker (vigtigt)
- Brugeren skriver dansk, så svar på dansk og kort.
- Papirpenge vises **altid i procent**, aldrig i dollars. Markedstal (liq, vol, mcap) må stå i $.
- Rigtige penge og indbetalinger er udskudt ("det venter vi med"). Byg det ikke uopfordret.

## Status pr. 2026-11-19
- Brugeren bad om at lave alle strategier om til de bedste og mest udbredte, så der testes en masse forskelligt.
  Nu 20 strategier (se ovenfor), startet forfra med epoch `2026-11-19-20strategier`. Samme regel som før: lad dem
  køre uændret, og vent på mindst 20–30 lukkede handler pr. strategi, før der drages konklusioner.

## Status pr. 2026-11-16 (ældre)
- 4 lukkede handler: CSI (+), WOHAIHAO (+), bill (−11,1 %) og Socks (ca. −9 %, stop med slippage).
  Kontoen står på ca. −1,05 %. Macen kører videre fra denne bog (`runs/state.json` på GitHub er
  cloud-bogen fra 2026-11-16; Macens nye handler ligger kun lokalt på Macen).
- Datagrundlaget for tærsklen er meget tyndt (én handel over 73 i backtesten), så analysen bør
  køres igen, når der er mere data.
- Flere datakilder gav ikke flere coins. Markedet er flaskehalsen, og der kommer flest coins
  omkring kl. 03–07 dansk tid.
- Sidst på dagen 2026-11-16: 17 strategier startet samtidig (epoch `2026-11-16-17strategier`). Brugeren
  lader dem køre 1–2 uger uden ændringer. Derefter: evaluér strategierne og skyggehandlen sammen med
  brugeren (mindst 20–30 lukkede handler pr. strategi før konklusioner), og skift dashboardet tilbage,
  når brugeren siger til. Claudes forudsigelser var svagt negative for de fleste; kun 6, 10–13, 15, 16
  svagt positive, og kun fordi de satser mere.
- Udestående for brugeren: nyt ntfy-kanalnavn og ny Jupiter-nøgle (begge har stået i chatten) og en
  kopi af backuppen væk fra serveren (`~/backup`, daglig kl. 04).
