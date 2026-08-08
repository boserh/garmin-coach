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
# systemd-run gives this script a WorkingDirectory of "/" by default, not REPO_ROOT —
# alembic needs to find alembic.ini, so cd there explicitly rather than relying on
# whatever cwd we happened to inherit (silently failed with "No 'script_location' key
# found in configuration" before this line existed).
cd "$REPO_ROOT"
# This script runs as root (see sudoers-garmin-deploy) — drop back to the checkout's own
# owner (whichever user the services actually run as) so alembic doesn't leave root-owned
# files in the repo/DB. Derived rather than hardcoded: a hardcoded "pi" here silently broke
# every /deploy on a host where the real user is something else (set -e aborts right here,
# before the restart below ever runs — see OPS-03 postmortem).
REPO_OWNER="$(stat -c '%U' "$REPO_ROOT")"
sudo -u "$REPO_OWNER" "$REPO_ROOT/venv/bin/python" -m alembic upgrade head
/bin/systemctl reload garmin-web.service
# garmin-mcp (NF-08 http transport) is OPTIONAL: it needs the `mcp` extra and an
# MCP_PUBLIC_URL, so most hosts don't run it. Restart it only where the unit actually
# exists — `set -e` would otherwise abort the whole deploy here, before the line below
# ever restarts the bots. Without this the deploy pulled new code that the MCP process
# then kept not running.
if /bin/systemctl cat garmin-mcp.service >/dev/null 2>&1; then
  /bin/systemctl restart --no-block garmin-mcp.service
fi
exec /bin/systemctl restart --no-block garmin-bot.service garmin-admin-bot.service
