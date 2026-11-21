#!/usr/bin/env bash
# Installerer Nova AI, så alt kører uden at nogen gør noget:
#   * starter automatisk (ved boot på en Linux-server, ved login på en Mac) og genstarter sig selv, hvis den går ned
#   * vagthunden i hubben genstarter botter, der går ned eller hænger
#   * henter nye versioner fra GitHub hvert kvarter (testet, med automatisk tilbagerulning)
# Kør én gang:   ./deploy/install.sh        (kan køres igen uden skade; virker på Ubuntu/Linux og macOS)
set -euo pipefail

HUB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OS="${HUB_OS:-$(uname -s)}"
DRY="${HUB_DRY_RUN:-0}"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 mangler. Mac: brew install python   Ubuntu: sudo apt install python3"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Python 3.11 eller nyere kræves (du har $("$PY" -V 2>&1))."; exit 1; }
command -v git >/dev/null || { echo "git mangler."; exit 1; }
add() { grep -q "^$1=" "$ENV_FILE" || echo "$1=$2" >> "$ENV_FILE"; }
newpass() { head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20; }

# =========================================================================== macOS
if [ "$OS" = Darwin ]; then
  if [ "$(id -u)" -eq 0 ] && [ "$DRY" != 1 ]; then echo "Kør uden sudo på en Mac:  ./deploy/install.sh"; exit 1; fi
  CONF="${HUB_CONF_DIR:-$HOME/Library/Application Support/Nova AI}"
  LA="${HUB_LA_DIR:-$HOME/Library/LaunchAgents}"
  ENV_FILE="$CONF/nova-ai.env"
  echo "== Nova AI installeres på din Mac (mappe: $HUB_DIR)"
  mkdir -p "$CONF" "$LA" "$HUB_DIR/logs"; touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
  add HUB_HOST 0.0.0.0
  add HUB_PORT 8080
  NEWPASS=""
  if ! grep -q '^HUB_PASSWORD=' "$ENV_FILE"; then NEWPASS="$(newpass)"; echo "HUB_PASSWORD=$NEWPASS" >> "$ENV_FILE"; fi
  # Den rigtige Nova-papirkonto ligger på serveren. En kopi her ville blive en anden konto, så den slås fra.
  if [ -n "${HUB_NOVA_ADDR:-}" ]; then add HUB_EXTERNAL_NOVA "$HUB_NOVA_ADDR"; else add HUB_DISABLE_NOVA 1; fi
  grep -q 'OANDA_TOKEN=' "$ENV_FILE" || echo '#OANDA_TOKEN=   # valgfri: giver Atlas og Meridian rigtig bid/ask. Uden den bruger de Yahoo Finance' >> "$ENV_FILE"
  echo "• Henter botterne (første gang kan det tage et minut) …"
  ( set -a; . "$ENV_FILE"; set +a; cd "$HUB_DIR" && "$PY" -m hub fetch )

  chmod +x "$HUB_DIR/deploy/run-mac.sh" "$HUB_DIR/deploy/update.sh"
  PATHV="$(dirname "$PY"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
  plist() {   # plist <label> <extra nøgler> <programargumenter...>
    local label="$1" extra="$2"; shift 2
    { echo '<?xml version="1.0" encoding="UTF-8"?>'
      echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
      echo '<plist version="1.0"><dict>'
      echo "<key>Label</key><string>$label</string>"
      echo '<key>ProgramArguments</key><array>'; for a in "$@"; do echo "<string>$a</string>"; done; echo '</array>'
      echo "<key>WorkingDirectory</key><string>$HUB_DIR</string>"
      echo "<key>EnvironmentVariables</key><dict><key>PATH</key><string>$PATHV</string><key>HUB_PY</key><string>$PY</string>"
      echo "<key>HUB_CONF_DIR</key><string>$CONF</string></dict>"
      echo "<key>StandardOutPath</key><string>$HUB_DIR/logs/$label.log</string><key>StandardErrorPath</key><string>$HUB_DIR/logs/$label.log</string>"
      echo "$extra"; echo '</dict></plist>'; } > "$LA/$label.plist"
  }
  plist ai.hub.dashboard '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>5</integer>' /bin/bash "$HUB_DIR/deploy/run-mac.sh"
  plist ai.hub.update '<key>RunAtLoad</key><false/><key>StartInterval</key><integer>900</integer>' /bin/bash "$HUB_DIR/deploy/update.sh"
  if [ "$DRY" = 1 ]; then echo "(tør kørsel: launchd røres ikke)"; exit 0; fi
  FAILED=""
  for l in ai.hub.dashboard ai.hub.update; do
    if command -v plutil >/dev/null 2>&1 && ! plutil -lint "$LA/$l.plist" >/dev/null 2>&1; then echo "✗ $l.plist er ikke en gyldig fil: $(plutil -lint "$LA/$l.plist" 2>&1)"; FAILED="$FAILED $l"; continue; fi
    launchctl bootout "gui/$(id -u)/$l" 2>/dev/null || true
    if ! ERR="$(launchctl bootstrap "gui/$(id -u)" "$LA/$l.plist" 2>&1)"; then
      echo "! launchctl bootstrap fejlede for $l: ${ERR:-ingen fejltekst}"
      echo "  Prøver den ældre metode (launchctl load) …"
      launchctl unload "$LA/$l.plist" 2>/dev/null || true
      if ! ERR2="$(launchctl load -w "$LA/$l.plist" 2>&1)"; then echo "✗ $l kunne ikke indlæses: ${ERR2:-ingen fejltekst}"; FAILED="$FAILED $l"; continue; fi
    fi
    launchctl print "gui/$(id -u)/$l" >/dev/null 2>&1 || { echo "✗ $l blev ikke indlæst, selv om launchctl ikke gav en fejl"; FAILED="$FAILED $l"; }
  done
  if [ -n "$FAILED" ]; then
    echo; echo "Autostart kunne ikke sættes op for:$FAILED. Hubben virker stadig. Start den selv i et Terminal-vindue (lad det stå åbent):"
    echo "    $HUB_DIR/deploy/run-mac.sh"
    echo "Send mig fejlteksten ovenfor, så retter jeg autostart."; exit 1
  fi
  PORT="$(grep '^HUB_PORT=' "$ENV_FILE" | cut -d= -f2)"
  for _ in $(seq 1 30); do curl -fs -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
  curl -fs -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && echo "✓ Nova AI kører" || { echo "✗ hubben svarer ikke endnu: se $HUB_DIR/logs/ai.hub.dashboard.log"; exit 1; }
  LAN="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  echo
  echo "Færdig. Åbn på denne Mac:  http://localhost:$PORT"
  [ -n "$LAN" ] && echo "Fra telefonen på samme wifi:  http://$LAN:$PORT"
  echo "Brugernavn: valgfrit   Adgangskode: ${NEWPASS:-(den står i $ENV_FILE)}"
  echo
  echo "Den starter selv, når du logger ind, og genstarter sig selv, hvis den går ned. Den holder Macen vågen, så længe"
  echo "den er tændt og låget er åbent. Lukker du låget, går Macen i dvale, og botterne holder pause. Til rigtig døgndrift"
  echo "skal hubben køre på en server (samme kommando, bare dér)."
  echo "Nova er slået fra her (den kører på serveren). Vil du se serverens Nova: sæt HUB_EXTERNAL_NOVA=<serverens adresse>:8787 i"
  echo "$ENV_FILE og kør: launchctl kickstart -k gui/$(id -u)/ai.hub.dashboard"
  exit 0
fi

# =========================================================================== Linux (Ubuntu-server)
ENV_FILE="${HUB_ENV_FILE:-/etc/nova-ai.env}"
UNIT_DIR="${HUB_UNIT_DIR:-/etc/systemd/system}"
if [ "$(id -u)" -ne 0 ]; then exec sudo -E bash "$0" "$@"; fi
RUN_USER="${HUB_USER:-$(stat -c %U "$HUB_DIR")}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
as_user() { if [ "$RUN_USER" != root ]; then runuser -u "$RUN_USER" -- "$@"; else "$@"; fi; }

echo "== Nova AI installeres (bruger: $RUN_USER, mappe: $HUB_DIR)"

# --- Nova kører måske allerede som egen service (så må hubben ikke starte en ekstra kopi) ---
NOVA_DIR="${HUB_NOVA_DIR:-$RUN_HOME/Coins}"
NOVA_SERVICE="${HUB_NOVA_SERVICE:-desk}"
NOVA_PORT=""
if [ "$DRY" != 1 ] && systemctl cat "$NOVA_SERVICE.service" >/dev/null 2>&1 && [ -d "$NOVA_DIR/.git" ]; then
  NOVA_PORT="$(systemctl cat "$NOVA_SERVICE.service" | grep -oE -- '--dashboard[ =]+[0-9]+' | grep -oE '[0-9]+' | head -1 || true)"
  NOVA_PORT="${NOVA_PORT:-8787}"
  echo "• Fandt Nova som tjenesten '$NOVA_SERVICE' (port $NOVA_PORT) i $NOVA_DIR – hubben viser den, men starter ingen kopi"
elif [ -n "${HUB_NOVA_PORT:-}" ]; then
  NOVA_PORT="$HUB_NOVA_PORT"
fi
if [ "$DRY" != 1 ]; then
  others="$(systemctl list-units --type=service --state=running --no-legend 2>/dev/null | grep -Ei 'papirdesk|desk' | grep -v "^ *$NOVA_SERVICE\.service" | grep -v '^ *hub' || true)"
  [ -n "$others" ] && { echo "• Der kører allerede andre desk-tjenester:"; echo "$others" | sed 's/^/    /'; echo "  Hubben starter sine egne kopier af de øvrige botter (hver med egen database). Stop de gamle, hvis du ikke vil have dem to gange."; }
fi

# --- aktie-botten (Atlas) bruger tidszoner (New York), som kræver systemets tidszonedatabase ---
if [ ! -e /usr/share/zoneinfo/America/New_York ] && [ "$DRY" != 1 ] && command -v apt-get >/dev/null 2>&1; then
  echo "• Installerer tidszonedatabasen (tzdata), som aktie-botten skal bruge"; apt-get install -y -qq tzdata >/dev/null 2>&1 || echo "! Kunne ikke installere tzdata: kør selv  sudo apt-get install -y tzdata"
fi

# --- hent botterne ---
if [ -n "$NOVA_PORT" ]; then export HUB_EXTERNAL_NOVA="$NOVA_PORT"; fi
( cd "$HUB_DIR" && as_user env ${HUB_EXTERNAL_NOVA:+HUB_EXTERNAL_NOVA="$HUB_EXTERNAL_NOVA"} "$PY" -m hub fetch )

# --- /etc/nova-ai.env: oprettes første gang; eksisterende værdier overskrives aldrig ---
touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
add HUB_HOST 0.0.0.0
add HUB_PORT 8080
NEWPASS=""
if ! grep -q '^HUB_PASSWORD=' "$ENV_FILE"; then NEWPASS="$(newpass)"; echo "HUB_PASSWORD=$NEWPASS" >> "$ENV_FILE"; fi
if [ -n "$NOVA_PORT" ]; then
  add HUB_EXTERNAL_NOVA "$NOVA_PORT"; add HUB_UPDATE_NOVA_DIR "$NOVA_DIR"; add HUB_UPDATE_NOVA_SERVICE "$NOVA_SERVICE"
  add HUB_UPDATE_NOVA_BRANCH "$(as_user git -C "$NOVA_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
fi
grep -q 'OANDA_TOKEN=' "$ENV_FILE" || echo '#OANDA_TOKEN=   # valgfri: giver Meridian rigtig bid/ask. Uden den bruger den Yahoo Finance' >> "$ENV_FILE"
chown "root:$RUN_USER" "$ENV_FILE" 2>/dev/null && chmod 640 "$ENV_FILE"       # ejeren kan læse (og `hub status` kan se indstillingerne); andre kan ikke

# --- systemd ---
sub() { sed -e "s#@USER@#$RUN_USER#g" -e "s#@DIR@#$HUB_DIR#g" -e "s#@PY@#$PY#g" "$1" > "$UNIT_DIR/$(basename "$1")"; }
mkdir -p "$UNIT_DIR"
for f in nova-ai.service nova-ai-update.service nova-ai-update.timer; do sub "$HUB_DIR/deploy/$f"; done
chmod +x "$HUB_DIR/deploy/update.sh"
if [ "$DRY" = 1 ]; then echo "(tør kørsel: systemd røres ikke)"; exit 0; fi
systemctl daemon-reload
systemctl enable --now nova-ai.service nova-ai-update.timer
sleep 5
systemctl is-active --quiet nova-ai.service && echo "✓ Nova AI kører" || { echo "✗ Nova AI startede ikke: sudo journalctl -u nova-ai -n 30"; exit 1; }

IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"; IP="${IP:-$(hostname -I | awk '{print $1}')}"
PORT="$(grep '^HUB_PORT=' "$ENV_FILE" | cut -d= -f2)"
echo
echo "Færdig. Hubben åbner på:  http://$IP:$PORT"
echo "Brugernavn: valgfrit   Adgangskode: ${NEWPASS:-(den står i $ENV_FILE)}"
echo "Logs:    sudo journalctl -u nova-ai -f      og botternes egne i $HUB_DIR/logs/"
echo "Status:  systemctl status nova-ai           Opdatering: systemctl list-timers nova-ai-update"
