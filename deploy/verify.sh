#!/usr/bin/env bash
# Svarer på fire spørgsmål om serveren og RETTER det, der ikke er i orden:
#   1. Kører dashboardet 24/7?                  2. Kører alle botter uden problem?
#   3. Er det uafhængigt af dine enheder?       4. Kan alle komme ind, hvor de end er?
# Kør på serveren:   ./deploy/verify.sh            (kun tjek uden at rette:  ./deploy/verify.sh --no-fix)
# Adgang: standard er åbent for alle uden adgangskode (som du bad om). Vil du have adgangskode:  VERIFY_ACCESS=on ./deploy/verify.sh
#         kun dine egne enheder:  VERIFY_ACCESS=private ./deploy/verify.sh
set -uo pipefail
HUB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OS="${HUB_OS:-$(uname -s)}"
FIX=1; [ "${1:-}" = "--no-fix" ] && FIX=0
SYSTEMCTL="${HUB_SYSTEMCTL:-systemctl}"; CURL="${HUB_CURL:-curl}"
PUBLIC_CMD="${HUB_PUBLIC_CMD:-$HUB_DIR/deploy/public.sh}"
TS="${HUB_TAILSCALE:-$(command -v tailscale || true)}"
ENV_FILE="${HUB_ENV_FILE:-/etc/nova-ai.env}"
ACCESS="${VERIFY_ACCESS:-open}"
UNIT_FILE="${HUB_UNIT_DIR:-/etc/systemd/system}/nova-ai.service"; BOT_WAIT="${HUB_BOT_WAIT:-60}"
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"; [ -n "${HUB_NO_SUDO:-}" ] && SUDO=""
READ=""; [ -r "$ENV_FILE" ] || READ="$SUDO"
getvar() { $READ grep "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true; }
PORT="$(getvar HUB_PORT)"; PORT="${PORT:-8080}"; PASS="$(getvar HUB_PASSWORD)"

ok()  { echo "  ✓ $*"; }
bad() { echo "  ✗ $*"; }
note() { echo "    $*"; }
# check <tekst> <test> [<rettelse>] [<vent i sekunder>]: kører testen, og hvis den fejler, forsøges rettelsen og testen igen
check() {
  local text="$1" test="$2" fix="${3:-}" wait="${4:-3}" i
  if eval "$test" >/dev/null 2>&1; then ok "$text"; return 0; fi
  if [ "$FIX" = 1 ] && [ -n "$fix" ]; then
    echo "  … $text: mangler, retter ($fix)"
    eval "$fix" >/dev/null 2>&1
    for i in $(seq 1 "$wait"); do eval "$test" >/dev/null 2>&1 && { ok "$text (rettet)"; return 0; }; sleep 1; done
  fi
  bad "$text"; return 1
}
healthy() { "$CURL" -fs -m 5 "http://127.0.0.1:$PORT/healthz" | grep -q ok; }
overview() { local a=(); [ -n "$PASS" ] && a=(-u "x:$PASS"); "$CURL" -fs -m 40 ${a[@]+"${a[@]}"} "http://127.0.0.1:$PORT/api/overview"; }
unit_ok() { $SYSTEMCTL is-enabled "$1" 2>/dev/null | grep -q '^enabled' && $SYSTEMCTL is-active --quiet "$1" 2>/dev/null; }

R1=ja; R2=ja; R3=ja; R4=ja; URL=""

echo; echo "1. Kører dashboardet 24/7?"
if [ "$OS" = Darwin ]; then
  bad "Hubben kører på en Mac. En Mac går i dvale og har lukkede låg. Kør scriptet på serveren."; R1=nej
else
  check "Hubben starter automatisk ved boot" "$SYSTEMCTL is-enabled nova-ai | grep -q '^enabled'" "$SUDO $SYSTEMCTL enable nova-ai" || R1=nej
  check "Hubben kører som tjeneste (uafhængigt af et Terminal-vindue)" "$SYSTEMCTL is-active --quiet nova-ai" "$SUDO $SYSTEMCTL restart nova-ai" 20 || R1=nej
  check "Systemet genstarter hubben, hvis den går ned" "grep -qs 'Restart=always' $UNIT_FILE" "" || R1=nej
  check "Hubben svarer" "healthy" "" 30 || R1=nej
  check "Opdatering hvert kvarter er slået til (nye rettelser kommer af sig selv)" "$SYSTEMCTL is-enabled nova-ai-update.timer | grep -q '^enabled'" "$SUDO $SYSTEMCTL enable --now nova-ai-update.timer" || R1=nej
fi

echo; echo "2. Kører alle botter uden problem?"
eval_bots() {   # skriver en linje pr. bot og returnerer antal botter med problemer
  overview | (cd "$HUB_DIR" && python3 -m hub.verifybots)
}
OUT="$(eval_bots)"; RC=$?
if [ $RC -ne 0 ] && [ "$FIX" = 1 ] && [ "$R1" = ja ]; then
  echo "$OUT"; echo "  … nogle botter mangler: henter nyeste kode og genstarter hubben, venter et minut"
  $SUDO $SYSTEMCTL restart nova-ai >/dev/null 2>&1; sleep "$BOT_WAIT"; OUT="$(eval_bots)"; RC=$?
fi
echo "$OUT"
if [ $RC -ne 0 ]; then
  R2=nej; echo "    Det ser ikke ud til at være noget, jeg kan rette uden at se logfilen. Kør:  python3 -m hub report   og send mig udskriften."
  echo "    Mistænker du netværket/spærring?  python3 -m hub doctor"
fi

echo; echo "3. Er det uafhængigt af dine enheder?"
if [ "$OS" = Darwin ]; then bad "Hubben kører på din Mac, så den afhænger af den."; R3=nej
else
  check "Hubben ligger på serveren og kører under systemd, ikke i din Mac eller telefon" "$SYSTEMCTL is-active --quiet nova-ai" "" || R3=nej
  if [ -n "$TS" ]; then
    check "Tailscale kører som tjeneste på serveren og starter ved boot" "$SYSTEMCTL is-enabled tailscaled | grep -q '^enabled' && $SYSTEMCTL is-active --quiet tailscaled" "$SUDO $SYSTEMCTL enable --now tailscaled" || R3=nej
    check "Serveren er logget ind på Tailscale" "$TS status" "" || R3=nej
  else
    bad "Tailscale er ikke installeret på serveren (https://tailscale.com/download)"; R3=nej
  fi
  note "Dashboardet afhænger kun af serveren og Tailscale. Din Mac og telefon kan være slukkede."
fi

echo; echo "4. Kan alle komme ind på dashboardet, hvor de end er?"
if [ -z "$TS" ]; then bad "Tailscale mangler, så der er ingen adresse udefra"; R4=nej
else
  is_open() { [ "$(getvar HUB_PUBLIC)" = 1 ] && [ "$(getvar HUB_OPEN)" = 1 ] && [ -z "$(getvar HUB_PASSWORD)" ]; }
  is_pw()   { [ "$(getvar HUB_PUBLIC)" = 1 ] && [ "$(getvar HUB_OPEN)" != 1 ] && [ -n "$(getvar HUB_PASSWORD)" ]; }
  case "$ACCESS" in
    open)    check "Åbent for alle uden adgangskode" "is_open" "$PUBLIC_CMD open --yes" 90 || R4=nej ;;
    on)      check "Åbent for alle med adgangskode" "is_pw" "$PUBLIC_CMD on" 90 || R4=nej ;;
    private) check "Åbent for dine egne enheder" "[ \"\$(getvar HUB_PUBLIC)\" != 1 ]" "$PUBLIC_CMD private" 90 || R4=nej
             note "Private adresser kan kun åbnes af dine egne enheder, ikke af andre." ;;
  esac
  H="$(getvar HUB_TS_HTTPS)"; H="${H:-443}"
  HOST="$("$TS" status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
  if [ -n "$HOST" ]; then URL="https://$HOST"; [ "$H" != 443 ] && URL="$URL:$H"; fi
  if [ "$ACCESS" != private ]; then
    check "Tailscale Funnel er slået til for dashboardets adresse (så den virker uden for dine egne enheder)" "\"$TS\" funnel status | grep -E \"$HOST(:$H)? \" | grep -qi 'funnel on'" "" || {
      R4=nej; note "Funnel skal tillades én gang i din Tailscale-konto: https://login.tailscale.com/admin/dns (slå HTTPS til) og https://login.tailscale.com/admin/acls (tillad Funnel). Kør så scriptet igen." ; }
  fi
  if [ -n "$URL" ]; then check "Adressen svarer: $URL" "\"$CURL\" -fs -m 20 \"$URL/healthz\" | grep -q ok" "" 5 || R4=nej; else bad "Kunne ikke finde serverens adresse"; R4=nej; fi
fi

echo; echo "Svar:"
yn() { [ "$1" = ja ] && echo "JA " || echo "NEJ"; }
echo "  1. Dashboardet kører 24/7:                 $(yn $R1)"
echo "  2. Alle botter kører uden problem:         $(yn $R2)"
echo "  3. Uafhængigt af dine enheder:             $(yn $R3)"
echo "  4. Alle kan komme ind, hvor de end er:     $(yn $R4)"
if [ -n "$URL" ]; then echo; echo "  Adresse:        $URL"; echo "  Til telefonen:  https://dani-gh1.github.io/Nova-AI/?server=$URL"; fi
[ "$R1$R2$R3$R4" = jajajaja ]
