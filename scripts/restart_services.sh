#!/usr/bin/env bash
# OPS-03 · fixed, argument-less restart target for the sudoers NOPASSWD grant used by
# /deploy (app/deploy.py). Keeping the unit names baked in here — instead of passing
# them as sudo arguments — means the sudoers rule can whitelist this exact script path
# rather than pattern-matching a systemctl command line.
#
# --no-block: systemctl queues the restart job and returns immediately instead of
# waiting for it to finish. garmin-bot.service is what's running this script, so
# waiting would mean waiting on our own process being killed as part of the restart.
#
# garmin-web gets `reload`, not `restart`: that sends SIGHUP to the gunicorn master,
# which rolls new workers in and drains old ones one at a time while the listen socket
# stays open the whole time — the dashboard/API never 502s during a deploy. `reload`
# does NOT run the unit's ExecStartPre, so migrate explicitly first. garmin-bot /
# garmin-admin-bot are single-process Telegram long-pollers with no request to drop —
# a plain restart (few seconds offline, auto-reconnects) is fine for those.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# This script runs as root (see sudoers-garmin-deploy) — `sudo -u pi` drops back to the
# service's own user so alembic doesn't leave root-owned files in the pi-owned repo/DB.
sudo -u pi "$REPO_ROOT/venv/bin/python" -m alembic upgrade head
/bin/systemctl reload garmin-web.service
exec /bin/systemctl restart --no-block garmin-bot.service garmin-admin-bot.service
