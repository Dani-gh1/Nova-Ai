#!/bin/bash
# Startes af launchd på en Mac: læser indstillingerne og holder Macen vågen, mens hubben kører.
CONF="${HUB_CONF_DIR:-$HOME/Library/Application Support/Nova AI}"
set -a
[ -f "$CONF/nova-ai.env" ] && . "$CONF/nova-ai.env"
set +a
cd "$(dirname "$0")/.." || exit 1
exec /usr/bin/caffeinate -i "${HUB_PY:-python3}" -m hub run
