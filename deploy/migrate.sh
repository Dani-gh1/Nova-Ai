#!/usr/bin/env bash
# Flyt hele hubben (kode, alle botter, deres data og Nova) fra en gammel server til en ny. Køres på din Mac:
#
#   ./deploy/migrate.sh <gammel-server> <ny-server> [ssh-nøgle]
#   fx  ./deploy/migrate.sh 89.168.96.169 152.70.1.2
#
# Nøglen er som standard ~/.ssh/oracle, og brugeren ubuntu (sæt HUB_SSH_USER for en anden).
# Den gamle server stoppes, mens data kopieres, så intet går tabt, og den startes IKKE igen bagefter
# (ellers ville hver bot have to papirkonti). Dens data bliver liggende som en ekstra kopi.
# HUB_DRY_RUN=1 viser kun, hvad der ville ske.
set -euo pipefail
OLD="${1:-}"; NEW="${2:-}"; KEY="${3:-$HOME/.ssh/oracle}"
U="${HUB_SSH_USER:-ubuntu}"
DRY="${HUB_DRY_RUN:-0}"
SSH="${HUB_SSH:-ssh}"
[ -n "$OLD" ] && [ -n "$NEW" ] || { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
[ "$OLD" != "$NEW" ] || { echo "✗ Den gamle og den nye server er den samme ($OLD)."; exit 1; }
[ "$DRY" = 1 ] || [ -r "$KEY" ] || { echo "✗ Fandt ikke ssh-nøglen $KEY. Angiv den som tredje argument."; exit 1; }

# De 15 botter, der blev slået fra på den lille server. På den nye skal alle 22 køre.
NEW_BOTS="ATLASGLOBAL ORIONMID NOVACLASSIC BONDAI METALAI ENERGIAI TEMAAI SMALLCAPAI UDBYTTEAI DANSKAI REGNSKABAI STAKINGAI DEFIAI AITOKENAI VALUTAAI"

on() {   # on <vært> <kommando>  (kører på serveren; -t så sudo og Tailscale-login kan spørge)
  local host="$1"; shift
  if [ "$DRY" = 1 ]; then echo "  [$host] $*"; return 0; fi
  "$SSH" -t -i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "$U@$host" "$@"
}
quiet() {   # som on, men uden terminal (til output, der skal læses af scriptet)
  local host="$1"; shift
  if [ "$DRY" = 1 ]; then echo "  [$host] $*" >&2; echo "${DRY_ANSWER:-}"; return 0; fi
  "$SSH" -i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes "$U@$host" "$@"
}
step() { echo; echo "== $*"; }

step "1/8 Tjekker forbindelsen til begge servere"
for h in "$OLD" "$NEW"; do
  quiet "$h" true >/dev/null || { echo "✗ Kan ikke logge ind på $h med $KEY. Tjek adressen og nøglen: ssh -i $KEY $U@$h"; exit 1; }
  echo "✓ $h"
done
MEM="$(quiet "$NEW" "free -m | awk '/^Mem:/ {print \$2}'")"
echo "• Den nye server har ${MEM:-?} MB hukommelse."
if [ -n "$MEM" ] && [ "$MEM" -lt 6000 ]; then
  echo "! Det er for lidt til alle 22 botter (ca. 6 GB eller mere anbefales). Vælg Ampere A1 med flere GB, eller fortsæt på eget ansvar."
fi

HOME_OLD="$(quiet "$OLD" 'echo $HOME')"; HOME_OLD="${HOME_OLD:-/home/$U}"
NOVA_DIR="$(quiet "$OLD" "grep '^HUB_UPDATE_NOVA_DIR=' /etc/nova-ai.env 2>/dev/null | cut -d= -f2-")"; NOVA_DIR="${NOVA_DIR:-$HOME_OLD/Coins}"
NOVA_UNIT="$(quiet "$OLD" "systemctl show -p FragmentPath --value desk.service 2>/dev/null")"
echo "• Hubben: $HOME_OLD/Nova-AI   Nova: $NOVA_DIR${NOVA_UNIT:+ (tjenesten desk: $NOVA_UNIT)}"

if [ "$DRY" != 1 ]; then
  echo
  echo "Den gamle server ($OLD) stoppes nu og startes ikke igen. Botterne holder pause, til den nye kører (typisk 10-20 min.)."
  read -r -p "Fortsæt? [j/N] " ok
  case "$ok" in j|J|ja|Ja|y|Y) ;; *) echo "Afbrudt. Intet er ændret."; exit 1 ;; esac
fi

step "2/8 Gør den nye server klar (git, Python, tidszoner)"
on "$NEW" "sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 tzdata curl >/dev/null && python3 --version"

step "3/8 Stopper den gamle server (kan tage op til 3 minutter, fordi botterne gør deres scanning færdig)"
on "$OLD" "sudo systemctl stop nova-ai-update.timer nova-ai; sudo systemctl stop desk 2>/dev/null; echo '✓ stoppet'"

step "4/8 Kopierer hubben, botterne, deres data og Nova direkte fra den gamle til den nye server"
PATHS="${HOME_OLD#/}/Nova-AI etc/nova-ai.env"
quiet "$OLD" "test -d '$NOVA_DIR'" >/dev/null 2>&1 && PATHS="$PATHS ${NOVA_DIR#/}"
[ -n "$NOVA_UNIT" ] && PATHS="$PATHS ${NOVA_UNIT#/}"
TAR="sudo tar -C / --exclude='*/__pycache__' --exclude='${HOME_OLD#/}/Nova-AI/backups' -czf - $PATHS"
if [ "$DRY" = 1 ]; then
  echo "  [$OLD] $TAR  |  [$NEW] sudo tar -C / -xzf -"
else
  "$SSH" -i "$KEY" -o BatchMode=yes "$U@$OLD" "$TAR" | "$SSH" -i "$KEY" -o BatchMode=yes "$U@$NEW" "sudo tar -C / -xzf -"
  echo "✓ kopieret"
fi

step "5/8 Tænder alle 22 botter på den nye server"
SEDX="-e '/^HUB_LITE=/d'"; for k in $NEW_BOTS; do SEDX="$SEDX -e '/^HUB_DISABLE_$k=/d'"; done      # fuld fart på den store server
on "$NEW" "sudo sed -i $SEDX /etc/nova-ai.env && echo '✓ de 15 nye botter er slået til'"

step "6/8 Starter Nova og installerer hubben"
if [ -n "$NOVA_UNIT" ]; then
  on "$NEW" "sudo systemctl daemon-reload && sudo systemctl enable --now desk && echo '✓ Nova kører'"
fi
on "$NEW" "cd ~/Nova-AI && git pull --ff-only -q; HUB_NOVA_DIR='$NOVA_DIR' ./deploy/install.sh"

step "7/8 Tailscale på den nye server (åbn linket, den viser, og log ind med samme konto som før)"
on "$NEW" "command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh; sudo tailscale up --hostname=hub-a1 && cd ~/Nova-AI && ./deploy/public.sh private"

step "8/8 Slår den gamle server fra for altid (data bliver liggende som en ekstra kopi)"
on "$OLD" "sudo systemctl disable nova-ai nova-ai-update.timer desk 2>/dev/null; true"
on "$NEW" "sleep 20; cd ~/Nova-AI && python3 -m hub status | grep -cE ' kører +port' | xargs -I{} echo '✓ {} botter kører allerede (resten starter en ad gangen de næste minutter)'; free -m"

echo
echo "Færdig. Dashboardet ligger nu på den nye server (adressen står ovenfor, hub-a1.<dit-net>.ts.net)."
echo "Vil du beholde den gamle adresse (instance-20261116-1234…), så kør på den GAMLE server:  sudo tailscale logout"
echo "og derefter på den NYE:  sudo tailscale set --hostname=instance-20261116-1234 && cd ~/Nova-AI && ./deploy/public.sh private"
echo "Når alt har kørt fint i et par dage, kan den gamle server slettes i Oracle Cloud."
