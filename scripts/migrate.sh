#!/usr/bin/env bash
# OPS-02 · back up FIRST, then run alembic. A failed migration on a live DB is the
# second most likely way to lose data (SD corruption is the first) — never run a bare
# `alembic upgrade head` on the Pi without a fresh copy to roll back to.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> backing up before migrating"
./venv/bin/python -m scripts.backup_db --dir backups

echo "==> alembic upgrade head"
./venv/bin/python -m alembic upgrade head

echo "==> done. If the upgrade misbehaved, restore the latest backups/garmin-*.db."
