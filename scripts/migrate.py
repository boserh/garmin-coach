"""OPS-02/OPS-03 · the one safe way to run `alembic upgrade head` on the live DB.

Two entry points used to disagree about this. ``scripts/migrate.sh`` — the documented
human path — backs the database up first, because a bad migration on a live SQLite file
is the second most likely way to lose a year of history (SD corruption is the first).
``/deploy`` reached the same `alembic upgrade head` through
``scripts/restart_services.sh`` and took **no** backup at all: the rule existed, was
written down, and the path everybody actually uses walked around it.

So both now go through here:

    ./venv/bin/python -m scripts.migrate            # manual: always back up, then upgrade
    ./venv/bin/python -m scripts.migrate --deploy   # nothing pending → do nothing at all

``--deploy`` is the difference between "an operator asked for a migration" and "a deploy
is passing through". Most deploys carry no migration, and copying the whole database on
every one of them is pointless SD-card wear — so that mode first asks whether anything is
actually pending, and when the answer is no it neither copies nor migrates. When something
IS pending it takes a *pre-migration* copy (``backup_db.pre_migration_backup``: its own
name and retention, and deliberately not the nightly freshness marker) and only then
upgrades. A failed backup fails the whole step: no rollback copy, no migration.

The pending check reads ``alembic_version`` out of the SQLite file directly rather than
opening the app's async engine — this runs as a short-lived subprocess during a deploy,
often while the services are still up, and the one thing it must not do is take a write
lock or import half the app. A non-SQLite ``DATABASE_URL`` (a future Postgres deployment)
has no such shortcut and no ``backup_db`` support either, so it degrades to a plain
upgrade with a warning, which is exactly what it does today.
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.config import settings
from scripts.backup_db import pre_migration_backup, sqlite_path_from_url

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def stored_revision(db_path: Path) -> str | None:
    """The revision alembic recorded in the database, or ``None`` when there is nothing to
    read — a database file that doesn't exist yet, or one predating alembic. Both mean
    "everything is pending", which is the safe answer."""
    if not db_path.exists():
        return None
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.DatabaseError:
        return None          # no alembic_version table (or an unreadable file)
    finally:
        con.close()
    return rows[0][0] if len(rows) == 1 else None


def head_revisions() -> set[str]:
    """The revision id(s) ``upgrade head`` would land on, straight from the migration
    scripts (``alembic heads``). A set, because a branched history legitimately has more
    than one — and an unmerged branch must read as "pending", never as "up to date"."""
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    return set(script.get_heads())


def has_pending(db_path: Path) -> bool:
    """Whether ``alembic upgrade head`` would actually change anything."""
    current = stored_revision(db_path)
    return current is None or current not in head_revisions()


def _alembic_upgrade() -> int:
    print("==> alembic upgrade head", flush=True)
    return subprocess.call(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=str(REPO_ROOT)
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default="backups", help="backup directory (default: backups/)")
    ap.add_argument(
        "--deploy", action="store_true",
        help="deploy mode: skip everything when no migration is pending, and take a "
             "pre-migration backup (not a rotated nightly one) when there is",
    )
    args = ap.parse_args(argv)

    try:
        db_path = sqlite_path_from_url(settings.DATABASE_URL)
    except ValueError as exc:
        # Postgres (PERF-03): no VACUUM INTO copy to take here — pg_dump is a different
        # tool with different credentials. Say so loudly and still migrate, so this path
        # is no worse than the bare `alembic upgrade head` it replaced.
        print(f"WARNING: {exc}\nWARNING: migrating WITHOUT a backup.", file=sys.stderr)
        return _alembic_upgrade()

    if args.deploy and not has_pending(db_path):
        print("==> no pending migration — nothing to back up, nothing to upgrade")
        return 0

    if not db_path.exists():
        # A first install: alembic is about to CREATE the database. There is nothing to
        # roll back to, and refusing to migrate for want of a copy of a file that doesn't
        # exist would block every deploy on a fresh host.
        print(f"==> no database at {db_path} yet — nothing to back up")
        return _alembic_upgrade()

    print("==> backing up before migrating", flush=True)
    try:
        if args.deploy:
            dest = pre_migration_backup(Path(args.dir))
        else:
            from scripts.backup_db import run as nightly_backup

            dest = nightly_backup(Path(args.dir))
    except Exception as exc:  # noqa: BLE001 — a failed backup must stop the migration
        print(f"backup failed: {exc}\nrefusing to migrate without a rollback copy.",
              file=sys.stderr)
        return 1
    print(f"backup ok → {dest}", flush=True)

    code = _alembic_upgrade()
    if code == 0:
        print("==> done.", flush=True)
    else:
        print(f"==> alembic failed (code {code}). Restore {dest} if the DB is damaged.",
              file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
