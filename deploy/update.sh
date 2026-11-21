#!/usr/bin/env bash
# Henter nye versioner af hubben, botterne og (hvis sat) Nova fra GitHub.
# Hver opdatering testes først. Fejler testene, rulles den tilbage, og intet genstartes.
# Køres af systemd-timeren som root; selve git-arbejdet køres som ejeren af mappen.
set -uo pipefail

HUB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OS="${HUB_OS:-$(uname -s)}"
DRY="${HUB_DRY_RUN:-0}"
if [ "$OS" = Darwin ]; then OWNER="$(stat -f %Su "$HUB_DIR")"; else OWNER="$(stat -c %U "$HUB_DIR")"; fi
RUN_USER="${HUB_USER:-$OWNER}"
TO=""; command -v timeout >/dev/null 2>&1 && TO="timeout 180"      # macOS har ikke timeout som standard

as_user() { if [ "$(id -u)" -eq 0 ] && [ "$RUN_USER" != root ]; then runuser -u "$RUN_USER" -- "$@"; else "$@"; fi; }
say() { echo "[nova-ai-update] $*"; }

# Kører testene i en ren kopi af den committede kode (git archive), aldrig i selve botten. Nogle botters tests bruger
# botternes rigtige datamappe, og de kunne ellers slette eller fylde den rigtige papirbog med syntetiske handler.
run_tests() {
  local d="$1" tmp rc
  tmp="$(mktemp -d)"; chown "$RUN_USER" "$tmp" 2>/dev/null || true
  as_user git -C "$d" archive HEAD | tar -x -C "$tmp" || { rm -rf "$tmp"; return 1; }
  (cd "$tmp" && as_user $TO python3 -m unittest discover -s tests -t . >/dev/null 2>&1); rc=$?
  rm -rf "$tmp"; return $rc
}

# update_dir <mappe> [gren]  ->  returnerer 0 = uændret, 1 = opdateret og testet, 2 = fejlede og rullet tilbage
update_dir() {
  local d="$1" branch="${2:-}" before after
  [ -d "$d/.git" ] || return 0
  before="$(as_user git -C "$d" rev-parse HEAD)" || return 0
  as_user git -C "$d" checkout -q -- runs/state.json 2>/dev/null || true    # Novas bog ændres lokalt af botten
  # statistikken må aldrig nulstilles af en opdatering: holder en ændring en EPOCH-konstant, hentes den ikke (HUB_ALLOW_RESET=1 tillader det)
  if [ "${HUB_ALLOW_RESET:-0}" != 1 ] && as_user git -C "$d" fetch -q origin ${branch:+"$branch"} 2>/dev/null; then
    if as_user git -C "$d" diff HEAD FETCH_HEAD --unified=0 -- '*.py' ':(exclude)tests/*' 2>/dev/null \
       | grep -qiE '^\+[[:space:]]*[A-Za-z_]*epoch[A-Za-z_]*[[:space:]]*(:[^=]*)?=[[:space:]]*([0-9]+|"[^"]*"|'"'"'[^'"'"']*'"'"')[[:space:]]*(#.*)?$'; then
      say "opdateringen af $d ville nulstille statistikken – holdt tilbage (HUB_ALLOW_RESET=1 for at tillade det)"; return 0
    fi
  fi
  if [ -n "$branch" ]; then as_user git -C "$d" pull -q --ff-only origin "$branch" 2>/dev/null
  else as_user git -C "$d" pull -q --ff-only 2>/dev/null; fi || { say "kunne ikke hente $d (springer over)"; return 0; }
  after="$(as_user git -C "$d" rev-parse HEAD)"
  [ "$before" = "$after" ] && return 0
  if [ -d "$d/tests" ] && ! run_tests "$d"; then
    say "NYE ÆNDRINGER I $d BESTOD IKKE TESTENE – ruller tilbage til ${before:0:8}"
    as_user git -C "$d" reset -q --hard "$before"
    return 2
  fi
  say "opdateret $d: ${before:0:8} -> ${after:0:8}"
  return 1
}

restart() {
  say "genstarter $1"
  [ "$DRY" = 1 ] && return 0
  if [ "$OS" = Darwin ]; then launchctl kickstart -k "gui/$(id -u)/ai.hub.dashboard" || true; else systemctl restart "$1"; fi
}

(cd "$HUB_DIR" && as_user env HUB_EXTRA_BACKUP="${HUB_UPDATE_NOVA_DIR:-}" python3 -m hub backup >/dev/null 2>&1) || true     # kopi af alle botters data før noget hentes
hub_changed=0
hub_before="$(as_user git -C "$HUB_DIR" rev-parse HEAD 2>/dev/null)"
update_dir "$HUB_DIR"; [ $? -eq 1 ] && hub_changed=1
# Nova kører som sin egen tjeneste fra bots/Coins i dette repo: genstart den kun, hvis dens kode er ændret
if [ "$hub_changed" = 1 ] && [ -n "${HUB_NOVA_SERVICE:-}" ] && \
   [ -n "$(as_user git -C "$HUB_DIR" diff --name-only "$hub_before" HEAD -- bots/Coins 2>/dev/null)" ]; then
  restart "$HUB_NOVA_SERVICE"
fi
for d in "$HUB_DIR"/bots/*/; do
  [ -L "${d%/}" ] && continue                      # symlinks (fx Nova) opdateres for sig selv nedenfor
  update_dir "$d"; [ $? -eq 1 ] && hub_changed=1
done
[ "$hub_changed" = 1 ] && restart nova-ai

if [ -n "${HUB_UPDATE_NOVA_DIR:-}" ]; then
  update_dir "$HUB_UPDATE_NOVA_DIR" "${HUB_UPDATE_NOVA_BRANCH:-}"; [ $? -eq 1 ] && restart "${HUB_UPDATE_NOVA_SERVICE:-desk}"
fi
exit 0
