# Sæt desk og dashboardet op på en Oracle Cloud-server

Guiden bruger Oracle Clouds gratis niveau (Always Free) og Ubuntu 24.04, som har Python 3.12.
Ubuntu 22.04 har kun Python 3.10, og desk kræver 3.11+, så vælg 24.04.

Tid: ca. 20 minutter. Du skal bruge en Oracle Cloud-konto og en SSH-nøgle.

---

## 1. Opret serveren

1. Log ind på <https://cloud.oracle.com> → **Compute → Instances → Create instance**.
2. **Name:** `desk`.
3. **Image and shape → Edit:**
   - Image: **Canonical Ubuntu 24.04**.
   - Shape: **VM.Standard.A1.Flex** (Ampere, 1 OCPU / 6 GB er rigeligt) eller **VM.Standard.E2.1.Micro**.
     Begge er Always Free. desk bruger meget lidt CPU og RAM.
4. **Networking:** lad Oracle oprette et nyt VCN med et *public subnet*, og sørg for at
   **Assign a public IPv4 address** er slået til.
5. **Add SSH keys:** upload din offentlige nøgle (`~/.ssh/id_ed25519.pub`) eller lad Oracle generere en og gem den.
6. **Create.** Når status er *Running*, så notér **Public IP address**, fx `141.147.12.34`.

Får du "Out of capacity" på A1, så prøv igen senere, vælg et andet *Availability domain* eller brug E2.1.Micro.

## 2. Log ind og installér

Fra din egen computer (skift IP'en ud):

```bash
ssh ubuntu@141.147.12.34
```

På serveren:

```bash
sudo apt update && sudo apt -y upgrade && sudo apt -y install git python3
```

```bash
python3 --version
```

Den skal vise 3.11 eller nyere.

Hent koden:

```bash
git clone https://github.com/Dani-gh1/Delphi-AI.git ~/Delphi-AI
```

> **Er repoet privat?** Så beder git om brugernavn og adgangskode. Brug dit GitHub-brugernavn og et
> *fine-grained personal access token* med kun læseadgang til `Delphi-AI` (GitHub → Settings → Developer settings →
> Personal access tokens) som adgangskode.

Kør testene (tager under et sekund, ingen netværk):

```bash
cd ~/Delphi-AI && python3 -m unittest discover -s tests -t .
```

Installér som systemd-services (opretter brugeren `desk`, kopierer til `/opt/desk` og starter ingenting):

```bash
sudo bash deploy/install.sh
```

## 3. Indstillinger

```bash
sudo nano /etc/desk/desk.env
```

Sæt mindst:

```
DESK_DASHBOARD_PASSWORD=en-lang-tilfældig-adgangskode
DESK_NTFY_TOPIC=desk-et-svært-gætteligt-navn-8f3k2
```

- Brugernavnet til dashboardet er `desk`. Uden adgangskode kan alle med IP'en se dashboardet.
- ntfy: installér ntfy-appen på telefonen og abonnér på samme emnenavn. Emnet er offentligt for alle, der kender
  navnet, så gør det svært at gætte.

En god tilfældig adgangskode kan laves med:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(18))"
```

## 4. Giv Analysten historik (anbefalet)

Backtesten henter afgjorte markeder og gemmer kalibreringsdata, så Analysten ikke starter fra nul.
Den tager 5–10 minutter:

```bash
cd /opt/desk && sudo -u desk python3 -m desk backtest
```

## 5. Start desk og dashboardet

```bash
sudo systemctl enable --now desk desk-dashboard
```

Tjek at begge kører:

```bash
systemctl status desk desk-dashboard --no-pager
```

Følg botten live (Ctrl+C for at stoppe visningen, botten kører videre):

```bash
journalctl -u desk -f
```

Efter et par minutter skal du se linjer som `cyklus 1: ok scout=... filter=... signaler=...`.

## 6. Åbn port 8787, så du kan se dashboardet

Oracle blokerer porten to steder, og den skal åbnes begge steder.

**a) I Oracle Cloud (Security List):**

1. **Networking → Virtual cloud networks →** dit VCN → **Subnets →** dit public subnet → **Security Lists →**
   *Default Security List*.
2. **Add Ingress Rules:**
   - Source CIDR: din egen IP med `/32` (find den på <https://ifconfig.me>), eller `0.0.0.0/0` hvis du vil kunne
     åbne den overalt, fx fra mobilen. Så **skal** adgangskoden fra trin 3 være sat.
   - IP Protocol: **TCP**
   - Destination port range: **8787**
3. **Add Ingress Rules.**

**b) På selve serveren** (Oracles Ubuntu-images har en iptables-regel, der afviser alt andet end SSH):

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8787 -j ACCEPT
```

```bash
sudo netfilter-persistent save
```

**Dit dashboard ligger nu på `http://<din-public-ip>:8787`**, fx `http://141.147.12.34:8787`.
Browseren beder om brugernavn (`desk`) og adgangskoden fra trin 3.

### Alternativ uden åben port: SSH-tunnel

Vil du ikke have porten åben mod internettet, så spring trin 6 over og kør på din egen computer:

```bash
ssh -N -L 8787:localhost:8787 ubuntu@141.147.12.34
```

Åbn derefter <http://localhost:8787>.

> Bemærk: med en åben port går adgangskoden over almindelig HTTP (ukrypteret). Vil du have HTTPS, så peg et
> domæne på serveren og sæt fx Caddy foran (`reverse_proxy localhost:8787`), og sæt
> `DESK_DASHBOARD_HOST=127.0.0.1` i `/etc/desk/desk.env`.

## 7. Drift

| Opgave | Kommando |
|---|---|
| Status og afkast | `cd /opt/desk && sudo -u desk python3 -m desk status` |
| Genstart | `sudo systemctl restart desk desk-dashboard` |
| Stop | `sudo systemctl stop desk desk-dashboard` |
| Logs | `journalctl -u desk -n 100 --no-pager` |
| Ny epoch (nulstil konti) | `cd /opt/desk && sudo -u desk python3 -m desk epoch "ny regel"` |
| Backup af databasen | `sudo cp /opt/desk/data/desk.sqlite3 ~/desk-backup-$(date +%F).sqlite3` |

**Opdatér til nyeste kode fra GitHub:**

```bash
cd ~/Delphi-AI && git pull && sudo bash deploy/install.sh && sudo systemctl restart desk desk-dashboard
```

`install.sh` rører ikke `/opt/desk/data` (database, state.json, cache) eller `/etc/desk/desk.env`.

**Genstarter serveren?** Begge services starter selv igen (`enable` + `Restart=always`) og fortsætter, hvor de slap,
fordi alt ligger i SQLite.

## Fejlfinding

- **Dashboardet svarer ikke udefra:** tjek først på serveren med `curl -s localhost:8787/healthz`, som skal svare `ok`.
  Virker det, er det trin 6 (Security List eller iptables).
- **`python3 --version` er under 3.11:** du har valgt et ældre image. Opret serveren igen med Ubuntu 24.04.
- **"Seneste cyklus" står stille på dashboardet:** `journalctl -u desk -n 50`. Fejl fra én platform stopper ikke den
  anden, og fejl sendes også til ntfy.
- **Disk:** databasen vokser med ca. nogle MB om dagen; prissnapshots ældre end 30 dage ryddes automatisk.
