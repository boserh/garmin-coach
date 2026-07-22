#!/usr/bin/env bash
# OPS-03 · fixed, argument-less restart target for the sudoers NOPASSWD grant used by
# /deploy (app/deploy.py). Keeping the unit names baked in here — instead of passing
# them as sudo arguments — means the sudoers rule can whitelist this exact script path
# rather than pattern-matching a systemctl command line.
#
# --no-block: systemctl queues the restart job and returns immediately instead of
# waiting for it to finish. garmin-bot.service is what's running this script, so
# waiting would mean waiting on our own process being killed as part of the restart.
set -euo pipefail
exec /bin/systemctl restart --no-block garmin-bot.service garmin-web.service
