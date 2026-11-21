#!/usr/bin/env bash
# Installerer desk som systemd-services. Kør som root fra repo-roden: sudo bash deploy/install.sh
# Starter IKKE services - det gør du selv med systemctl (se README).
set -euo pipefail
python3 -c 'import sys; assert sys.version_info >= (3, 11), "kræver Python 3.11+"'
id desk >/dev/null 2>&1 || useradd --system --home /opt/desk --shell /usr/sbin/nologin desk
mkdir -p /opt/desk /etc/desk
cp -r desk deploy README.md CLAUDE.md /opt/desk/
mkdir -p /opt/desk/data /opt/desk/reports
[ -f /etc/desk/desk.env ] || cp deploy/desk.env.example /etc/desk/desk.env
chmod 600 /etc/desk/desk.env
chown -R desk:desk /opt/desk
cp deploy/desk.service deploy/desk-dashboard.service /etc/systemd/system/
systemctl daemon-reload
echo "Installeret. Start med: systemctl enable --now desk desk-dashboard"
