#!/usr/bin/env bash
# Slå adgangskoden til dashboardet til eller fra og genstart hubben.
#   ./deploy/password.sh off     ingen adgangskode (alle, der kan nå hubben, kan se dashboardet)
#   ./deploy/password.sh local   ingen adgangskode, og kun denne maskine kan åbne dashboardet (127.0.0.1)
#   ./deploy/password.sh on      ny tilfældig adgangskode, åben for netværket
#   ./deploy/password.sh show    vis indstillingen
set -euo pipefail
OS="${HUB_OS:-$(uname -s)}"
if [ "$OS" = Darwin ]; then ENV_FILE="${HUB_ENV_FILE:-$HOME/Library/Application Support/Nova AI/nova-ai.env}"; else ENV_FILE="${HUB_ENV_FILE:-/etc/nova-ai.env}"; fi
[ -f "$ENV_FILE" ] || { echo "Fandt ikke $ENV_FILE. Kør først ./deploy/install.sh"; exit 1; }
SUDO=""; [ -w "$ENV_FILE" ] || SUDO="sudo"
READ=""; [ -r "$ENV_FILE" ] || READ="sudo"

setvar() {   # setvar NAVN VÆRDI  (erstatter eller tilføjer linjen)
  local tmp; tmp="$(mktemp)"
  { $READ grep -v "^$1=" "$ENV_FILE" || true; echo "$1=$2"; } > "$tmp"
  $SUDO sh -c "cat '$tmp' > '$ENV_FILE'"; rm -f "$tmp"
}
service_loaded() {
  if [ "$OS" = Darwin ]; then launchctl print "gui/$(id -u)/ai.hub.dashboard" >/dev/null 2>&1; else systemctl is-active --quiet nova-ai 2>/dev/null; fi
}
DRY="${HUB_DRY_RUN:-0}"
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
host() { $READ grep '^HUB_HOST=' "$ENV_FILE" | cut -d= -f2; }
pw() { $READ grep '^HUB_PASSWORD=' "$ENV_FILE" | cut -d= -f2-; }

public() { [ "$($READ grep '^HUB_PUBLIC=' "$ENV_FILE" | cut -d= -f2)" = 1 ] && [ "$($READ grep '^HUB_OPEN=' "$ENV_FILE" | cut -d= -f2)" != 1 ]; }
case "${1:-show}" in
  off|local) if public; then echo "✗ Dashboardet er åbent for hele internettet, så adgangskoden kan ikke slås fra. Kør først: ./deploy/public.sh off"; exit 1; fi ;;
esac
case "${1:-show}" in
  off)   setvar HUB_PASSWORD ""; restart
         echo "✓ Adgangskoden er slået fra."
         [ "$(host)" = 0.0.0.0 ] && echo "  Obs: alle på samme netværk kan nu se dashboardet. Kun denne maskine: ./deploy/password.sh local" ;;
  local) setvar HUB_PASSWORD ""; setvar HUB_HOST 127.0.0.1; restart
         echo "✓ Ingen adgangskode, og kun denne maskine kan åbne dashboardet (http://localhost:8080). Telefonen kan ikke nå det." ;;
  on)    NEW="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)"
         setvar HUB_PASSWORD "$NEW"; setvar HUB_HOST 0.0.0.0; restart
         echo "✓ Adgangskode slået til. Brugernavn: valgfrit   Adgangskode: $NEW" ;;
  show)  echo "Vært: $(host)"
         [ -n "$(pw)" ] && echo "Adgangskode: $(pw)" || echo "Ingen adgangskode (slået fra)" ;;
  *)     echo "Brug: $0 off | local | on | show"; exit 1 ;;
esac
