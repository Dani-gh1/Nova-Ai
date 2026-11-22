#!/usr/bin/env bash
# Åbn dashboardet fra hvor som helst via Tailscale (gratis, HTTPS, ingen åbne porte). Tre tilstande:
#   ./deploy/public.sh private       kun DINE egne enheder (med Tailscale) kan åbne det, fra hvor som helst. Ingen adgangskode nødvendig.
#   ./deploy/public.sh on            åbent for hele internettet, beskyttet af en lang adgangskode
#   ./deploy/public.sh open --yes    åbent for hele internettet UDEN adgangskode (alle, der kender adressen, kan se det)
#   ./deploy/public.sh off           slå det fra igen            ./deploy/public.sh status   vis adressen
# Kør det på den maskine, hvor hubben kører, helst en server, der altid er tændt.
set -euo pipefail
OS="${HUB_OS:-$(uname -s)}"
DRY="${HUB_DRY_RUN:-0}"
if [ "$OS" = Darwin ]; then ENV_FILE="${HUB_ENV_FILE:-$HOME/Library/Application Support/Nova AI/nova-ai.env}"; else ENV_FILE="${HUB_ENV_FILE:-/etc/nova-ai.env}"; fi
[ -f "$ENV_FILE" ] || { echo "Fandt ikke $ENV_FILE. Kør først ./deploy/install.sh"; exit 1; }
SUDO=""; [ -w "$ENV_FILE" ] || SUDO="sudo"

TS="${HUB_TAILSCALE:-$(command -v tailscale || true)}"
[ -n "$TS" ] || { [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ] && TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale; }
[ -n "$TS" ] || { echo "Tailscale er ikke installeret her. Hent det på https://tailscale.com/download og log ind, og kør så scriptet igen."; exit 1; }

READ=""; [ -r "$ENV_FILE" ] || READ="sudo"          # /etc/nova-ai.env kan kun læses af root, hvis den er fra en ældre installation
getvar() { $READ grep "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- || true; }
setvar() { local tmp; tmp="$(mktemp)"; { $READ grep -v "^$1=" "$ENV_FILE" || true; echo "$1=$2"; } > "$tmp"; $SUDO sh -c "cat '$tmp' > '$ENV_FILE'"; rm -f "$tmp"; }
service_loaded() {
  if [ "$OS" = Darwin ]; then launchctl print "gui/$(id -u)/ai.hub.dashboard" >/dev/null 2>&1; else systemctl is-active --quiet nova-ai 2>/dev/null; fi
}
RESTARTED=0
restart() {
  [ "$DRY" = 1 ] && return 0
  if service_loaded; then
    if [ "$OS" = Darwin ]; then launchctl kickstart -k "gui/$(id -u)/ai.hub.dashboard" || true; else sudo systemctl restart nova-ai || true; fi
    RESTARTED=1
  else
    echo "• Hubben kører ikke som tjeneste her (autostart er ikke sat op). Indstillingen er gemt og bruges, næste gang hubben startes:"
    if [ "$OS" = Darwin ]; then echo "    ./deploy/run-mac.sh        (lad Terminal-vinduet stå åbent)"; else echo "    sudo systemctl start hub"; fi
  fi
}
PORT="$(getvar HUB_PORT)"; PORT="${PORT:-8080}"
url_of() { grep -oE 'https://[A-Za-z0-9.-]+\.ts\.net[^ ]*' | head -1 || true; }
funnel_url() { "$TS" funnel status 2>/dev/null | url_of; }
serve_url() { "$TS" serve status 2>/dev/null | url_of; }

# Eksisterende Tailscale-adresser: én linje "<https-port> <målport>" pr. adresse (fx "443 8787", hvis Nova ligger på hovedadressen)
mapped() {
  { "$TS" serve status 2>/dev/null; "$TS" funnel status 2>/dev/null; } | awk '
    /^https:\/\// { n = split($1, a, ":"); hp = (n >= 3) ? a[3] : 443; sub(/[^0-9].*/, "", hp) }
    /\|-- \/ .*proxy/ { t = $0; sub(/.*:/, "", t); sub(/[^0-9].*/, "", t); print hp, t }' | sort -u
}
# Hubben får port 443, hvis den er fri (eller allerede er hubbens), ellers 8443, og ellers 10000. Det er de porte, Funnel tillader.
pick_https() {
  HTTPS=443; local m; m="$(mapped)"
  if echo "$m" | awk -v p="$PORT" '$1 == 443 && $2 != p { f = 1 } END { exit !f }'; then
    HTTPS=8443
    echo "$m" | awk -v p="$PORT" '$1 == 8443 && $2 != p { f = 1 } END { exit !f }' && HTTPS=10000
    echo "• Hovedadressen (port 443) bruges allerede af en anden tjeneste, fx din Nova. Den bliver ikke rørt. Dashboardet får port $HTTPS i stedet."
  fi
  setvar HUB_TS_HTTPS "$HTTPS"
}

# Kører en Tailscale-kommando. Afviser Tailscale den ("Access denied": ændringer kræver root eller `tailscale set --operator`),
# køres den igen med sudo, og resten af scriptet bruger sudo.
USE_SUDO=0
tsrun() {
  local out rc
  if [ "$USE_SUDO" = 1 ]; then sudo "$TS" "$@" 2>&1; return $?; fi
  out="$("$TS" "$@" 2>&1)"; rc=$?
  if [ $rc -ne 0 ] && echo "$out" | grep -qiE 'access denied|permission denied|operator'; then
    USE_SUDO=1; sudo "$TS" "$@" 2>&1; return $?
  fi
  echo "$out"; return $rc
}

need_login() { "$TS" status >/dev/null 2>&1 || { echo "Tailscale er ikke logget ind. Kør: $TS up"; exit 1; }; }
save_host() { [ -n "$(getvar HUB_HOST_BEFORE_PUBLIC)" ] || setvar HUB_HOST_BEFORE_PUBLIC "$(getvar HUB_HOST)"; setvar HUB_HOST 127.0.0.1; }   # hubben åbner kun lokalt
wait_hub() { { [ "$DRY" = 1 ] || [ "$RESTARTED" = 0 ]; } || for _ in $(seq 1 30); do curl -fs -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done; }
tailscale_help() {
  echo "✗ Tailscale kunne ikke slå det til:"; echo "$1" | sed 's/^/    /'
  if echo "$1" | grep -qiE 'access denied|permission denied|operator'; then
    echo; echo "Tailscale kræver administratorrettigheder til det. Prøv:  sudo $TS funnel --bg --https=${HTTPS:-443} $PORT"
    echo "Eller giv din bruger lov én gang og kør scriptet igen:  sudo $TS set --operator=\$USER"; return
  fi
  echo; echo "Det skal tillades én gang i din Tailscale-konto:"
  echo "  1. https://login.tailscale.com/admin/dns   → slå 'HTTPS Certificates' til"
  echo "  2. For internetadgang (on/open): https://login.tailscale.com/admin/acls → tillad Funnel (se linjen i fejlen ovenfor)"
  echo "  Kør så scriptet igen."
}
funnel_on() {
  pick_https
  set +e; OUT="$(tsrun funnel --bg --https="$HTTPS" "$PORT")"; RC=$?
  [ $RC -ne 0 ] && [ "$HTTPS" = 443 ] && { OUT="$(tsrun funnel "$PORT" on)"; RC=$?; }        # ældre Tailscale-versioner
  set -e; [ $RC -eq 0 ] || { tailscale_help "$OUT"; exit 1; }
  URL="$(echo "$OUT" | url_of)"; URL="${URL:-$(funnel_url)}"
}
serve_on() {
  pick_https
  set +e; tsrun funnel --https="$HTTPS" off >/dev/null 2>&1; OUT="$(tsrun serve --bg --https="$HTTPS" "$PORT")"; RC=$?
  [ $RC -ne 0 ] && [ "$HTTPS" = 443 ] && { OUT="$(tsrun serve https / "http://127.0.0.1:$PORT")"; RC=$?; }
  set -e; [ $RC -eq 0 ] || { tailscale_help "$OUT"; exit 1; }
  URL="$(echo "$OUT" | url_of)"; URL="${URL:-$(serve_url)}"
}
phone_link() { [ -n "${URL:-}" ] && echo "  Til telefonen (gemmer adressen): https://dani-gh1.github.io/Nova-Ai/?server=${URL%/}"; return 0; }

case "${1:-status}" in
  private)
    need_login
    setvar HUB_PASSWORD ""; setvar HUB_PUBLIC 0; setvar HUB_OPEN 0; save_host; restart; wait_hub; serve_on
    echo "✓ Dashboardet kan nu åbnes fra hvor som helst, men kun fra dine egne enheder med Tailscale. Ingen adgangskode."
    echo; echo "  Adresse: ${URL:-(se: $TS serve status)}"; phone_link
    echo; echo "Installér Tailscale-appen på telefonen og log ind med samme konto, så åbner adressen overalt. Andre kan ikke åbne den." ;;
  on)
    need_login
    PW="$(getvar HUB_PASSWORD)"; NEWPW=""
    if [ "${#PW}" -lt 12 ]; then NEWPW="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 22)"; setvar HUB_PASSWORD "$NEWPW"; fi
    save_host; setvar HUB_PUBLIC 1; setvar HUB_OPEN 0; restart; wait_hub; funnel_on
    echo "✓ Dashboardet er nu åbent fra hele internettet, med HTTPS og adgangskode."
    echo; echo "  Adresse:     ${URL:-(se: $TS funnel status)}"; echo "  Brugernavn:  valgfrit"; echo "  Adgangskode: ${NEWPW:-$(getvar HUB_PASSWORD)}"; phone_link
    echo; echo "Efter 5 forkerte forsøg spærres adressen i et minut, derefter længere." ;;
  open)
    if [ "${2:-}" != "--yes" ]; then
      echo "Det her åbner dashboardet for HELE internettet uden adgangskode. Alle, der finder adressen, kan se dine papirhandler og strategier"
      echo "(kun papirhandel, ingen rigtige penge, og ingen kan ændre noget). Adresser på .ts.net kan findes via certifikatlister."
      echo "Vil du kun selv kunne åbne det fra hvor som helst, så brug i stedet:  ./deploy/public.sh private"
      echo; echo "Er du sikker, så kør:  ./deploy/public.sh open --yes"; exit 1
    fi
    need_login
    setvar HUB_PASSWORD ""; save_host; setvar HUB_PUBLIC 1; setvar HUB_OPEN 1; restart; wait_hub; funnel_on
    echo "✓ Dashboardet er nu åbent for alle uden adgangskode (kun læsning)."
    echo; echo "  Adresse: ${URL:-(se: $TS funnel status)}"; phone_link
    echo; echo "Hastighedsgrænse: højst 240 forespørgsler i minuttet pr. besøgende. Slå det fra igen med: ./deploy/public.sh off" ;;
  off)
    H="$(getvar HUB_TS_HTTPS)"
    if [ -n "$H" ]; then                                   # kun den adresse, hubben selv har oprettet (rører aldrig fx Novas)
      set +e; tsrun funnel --https="$H" off >/dev/null 2>&1; tsrun serve --https="$H" off >/dev/null 2>&1; set -e
    fi
    setvar HUB_PUBLIC 0; setvar HUB_OPEN 0
    BEFORE="$(getvar HUB_HOST_BEFORE_PUBLIC)"; [ -n "$BEFORE" ] && setvar HUB_HOST "$BEFORE"
    restart
    echo "✓ Adgang udefra er slået fra. Dashboardet kan kun åbnes som før (${BEFORE:-kun på denne maskine})." ;;
  status)
    MODE="fra"; [ "$(getvar HUB_PUBLIC)" = 1 ] && MODE="internettet, $([ "$(getvar HUB_OPEN)" = 1 ] && echo "uden adgangskode" || echo "med adgangskode")"
    [ "$MODE" = fra ] && [ -n "$(serve_url)" ] && MODE="privat (kun dine egne enheder med Tailscale)"
    echo "Adgang: $MODE   Vært: $(getvar HUB_HOST)   Port: $PORT"
    H="$(getvar HUB_TS_HTTPS)"; echo "Tailscale-port: ${H:-ikke sat}"
    echo "Alle Tailscale-adresser på denne maskine:"; mapped | sed 's/^/   https-port /; s/ \([0-9]*\)$/ → lokal port \1/' ;;
  *) echo "Brug: $0 private | on | open --yes | off | status"; exit 1 ;;
esac
