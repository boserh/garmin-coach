#!/usr/bin/env bash
# OPS-02 · back up FIRST, then run alembic. A failed migration on a live DB is the
# second most likely way to lose data (SD corruption is the first) — never run a bare
# `alembic upgrade head` on the Pi without a fresh copy to roll back to.
#
# The steps themselves live in scripts/migrate.py, because /deploy needs the same rule
# and a second copy of it in shell is how the two drifted apart in the first place (the
# deploy path migrated with no backup at all). This stays as the documented human entry
# point; every flag is passed straight through (`--deploy`, `--dir`).
set -euo pipefail
cd "$(dirname "$0")/.."

exec ./venv/bin/python -m scripts.migrate "$@"
