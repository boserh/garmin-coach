"""OPS-02/OPS-03 · `alembic upgrade head` never runs on the live DB without a rollback copy.

The rule was written down and enforced in `scripts/migrate.sh` — and the path everybody
actually uses, `/deploy` → `restart_services.sh`, walked straight around it and migrated
with no backup at all. So the tests that matter here are the ones that pin the *decisions*:
nothing pending ⇒ don't copy the database at all (a deploy is not a reason to write to the
SD card), something pending ⇒ copy first, and a failed copy ⇒ no migration.

The pre-migration copy also has to stay out of the OPS-08 freshness marker: deploys are
frequent, and letting them refresh that marker would report a dead nightly backup as fine.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from scripts import backup_db, migrate


@pytest.fixture
def db(tmp_path, monkeypatch) -> Path:
    """A throwaway SQLite file wired in as DATABASE_URL, stamped at the current head."""
    path = tmp_path / "garmin.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    con.execute("INSERT INTO alembic_version VALUES (?)",
                (sorted(migrate.head_revisions())[0],))
    con.commit()
    con.close()
    monkeypatch.setattr(backup_db.settings, "DATABASE_URL", f"sqlite:///{path}")
    return path


# ---------- what counts as pending ----------

def test_head_revisions_reads_the_real_migration_scripts():
    """A single head — a second one means someone branched the history and `upgrade head`
    would fail; the check must see that, not paper over it."""
    heads = migrate.head_revisions()
    assert len(heads) == 1, f"alembic history has branched: {heads}"


def test_a_db_stamped_at_head_has_nothing_pending(db):
    assert migrate.has_pending(db) is False


def test_an_older_revision_is_pending(db):
    con = sqlite3.connect(db)
    con.execute("UPDATE alembic_version SET version_num = 'deadbeef1234'")
    con.commit()
    con.close()
    assert migrate.has_pending(db) is True


def test_a_missing_or_unstamped_db_is_pending(tmp_path, db):
    """Both mean "everything is pending", which is the safe answer."""
    con = sqlite3.connect(db)
    con.execute("DROP TABLE alembic_version")
    con.commit()
    con.close()
    assert migrate.has_pending(db) is True
    assert migrate.has_pending(tmp_path / "not-there.db") is True


# ---------- the deploy decision ----------

def test_deploy_mode_with_nothing_pending_touches_nothing(db, tmp_path, monkeypatch):
    """The common deploy: code changed, schema didn't. Copying the whole database anyway
    would be pure SD-card wear."""
    monkeypatch.setattr(migrate, "_alembic_upgrade",
                        lambda: pytest.fail("must not migrate"))
    monkeypatch.setattr(migrate, "pre_migration_backup",
                        lambda *a, **k: pytest.fail("must not copy the DB"))
    assert migrate.main(["--deploy", "--dir", str(tmp_path / "backups")]) == 0


def test_deploy_mode_backs_up_before_migrating(db, tmp_path, monkeypatch):
    order = []
    con = sqlite3.connect(db)
    con.execute("UPDATE alembic_version SET version_num = 'deadbeef1234'")
    con.commit()
    con.close()

    real_backup = migrate.pre_migration_backup
    monkeypatch.setattr(migrate, "pre_migration_backup",
                        lambda *a, **k: (order.append("backup"), real_backup(*a, **k))[1])
    monkeypatch.setattr(migrate, "_alembic_upgrade",
                        lambda: (order.append("upgrade"), 0)[1])

    backups = tmp_path / "backups"
    assert migrate.main(["--deploy", "--dir", str(backups)]) == 0
    assert order == ["backup", "upgrade"]
    assert list(backups.glob(f"{backup_db.PREMIGRATE_PREFIX}*.db"))


def test_a_failed_backup_stops_the_migration(db, tmp_path, monkeypatch):
    """No rollback copy, no migration — the whole point of the rule."""
    con = sqlite3.connect(db)
    con.execute("UPDATE alembic_version SET version_num = 'deadbeef1234'")
    con.commit()
    con.close()

    def boom(*a, **k):
        raise OSError("no space left on device")

    monkeypatch.setattr(migrate, "pre_migration_backup", boom)
    monkeypatch.setattr(migrate, "_alembic_upgrade",
                        lambda: pytest.fail("must not migrate without a backup"))
    assert migrate.main(["--deploy", "--dir", str(tmp_path / "backups")]) == 1


def test_a_first_install_migrates_instead_of_demanding_a_backup(tmp_path, monkeypatch):
    """A fresh host: alembic is about to CREATE the database. Refusing to migrate for want
    of a copy of a file that does not exist would block every deploy there."""
    monkeypatch.setattr(backup_db.settings, "DATABASE_URL",
                        f"sqlite:///{tmp_path / 'brand-new.db'}")
    monkeypatch.setattr(migrate, "pre_migration_backup",
                        lambda *a, **k: pytest.fail("nothing exists to back up"))
    monkeypatch.setattr(migrate, "_alembic_upgrade", lambda: 0)
    assert migrate.main(["--deploy", "--dir", str(tmp_path / "backups")]) == 0


def test_manual_mode_always_backs_up_even_with_nothing_pending(db, tmp_path, monkeypatch):
    """A human running migrate.sh asked for a backup; only the deploy path is allowed to
    decide there is nothing worth copying."""
    seen = []
    monkeypatch.setattr(backup_db, "run",
                        lambda d, **k: (seen.append(d), Path(d) / "x.db")[1])
    monkeypatch.setattr(migrate, "_alembic_upgrade", lambda: 0)
    assert migrate.main(["--dir", str(tmp_path / "backups")]) == 0
    assert seen


def test_postgres_url_migrates_with_a_loud_warning(tmp_path, monkeypatch, capsys):
    """PERF-03's future Postgres deployment has no VACUUM INTO copy to take — it must not
    silently skip the migration, and it must not silently skip the warning either."""
    monkeypatch.setattr(backup_db.settings, "DATABASE_URL",
                        "postgresql+asyncpg://u@h/db")
    monkeypatch.setattr(migrate, "_alembic_upgrade", lambda: 0)
    assert migrate.main(["--deploy"]) == 0
    assert "WITHOUT a backup" in capsys.readouterr().err


# ---------- the pre-migration copy itself ----------

def test_pre_migration_copy_is_a_real_readable_database(db, tmp_path):
    dest = backup_db.pre_migration_backup(tmp_path / "backups")
    assert dest.name.startswith(backup_db.PREMIGRATE_PREFIX)
    con = sqlite3.connect(dest)
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        con.close()


def test_pre_migration_copies_prune_themselves(db, tmp_path):
    """`rotate()` only matches the nightly name, so these need their own retention or they
    accumulate one per deploy, forever."""
    import datetime as dt

    backups = tmp_path / "backups"
    base = dt.datetime(2026, 8, 21, 9, 0, 0)
    for i in range(5):
        backup_db.pre_migration_backup(backups, keep=3,
                                       now=base + dt.timedelta(minutes=i))
    kept = sorted(p.name for p in backups.glob(f"{backup_db.PREMIGRATE_PREFIX}*.db"))
    assert len(kept) == 3
    assert kept[-1].endswith("T090400.db")     # the newest three, not the first three


def test_pre_migration_copy_never_touches_the_freshness_marker(db, tmp_path):
    """OPS-08: that marker answers "is the SCHEDULED backup alive, and still landing off
    the card". A deploy-time copy must not answer it — deploys would keep it green for
    weeks after the nightly timer died, and would erase a recorded rsync failure."""
    from app.backup_status import MARKER_NAME

    backups = tmp_path / "backups"
    backups.mkdir()
    stale = {"ts": 1.0, "path": "old", "size": 1, "rsync_ok": False,
             "rsync_error": "rsync exit 23: Read-only file system"}
    (backups / MARKER_NAME).write_text(json.dumps(stale))

    backup_db.pre_migration_backup(backups)

    assert json.loads((backups / MARKER_NAME).read_text()) == stale


def test_nightly_backup_is_not_displaced_by_a_deploy(db, tmp_path):
    """The daily copy is the one that survives the SD card; a deploy taking a copy seconds
    before the very migration it protects against must not overwrite it."""
    backups = tmp_path / "backups"
    nightly = backup_db.run(backups)
    before = nightly.read_bytes()

    backup_db.pre_migration_backup(backups)

    assert nightly.exists() and nightly.read_bytes() == before
