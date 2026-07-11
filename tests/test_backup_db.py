"""OPS-02 · backup script: consistent copy + rotation + a real restore check.

The restore check (open the backup, read the rows back) is the AC's "backup is
actually readable" test — the one without which everything else is theatre.
"""
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from scripts import backup_db


@pytest.mark.parametrize(
    "url,expected",
    [
        ("sqlite+aiosqlite:///./garmin.db", "garmin.db"),
        ("sqlite:///rel.db", "rel.db"),
        ("sqlite:////var/data/garmin.db", "/var/data/garmin.db"),
    ],
)
def test_sqlite_path_from_url(url, expected):
    assert backup_db.sqlite_path_from_url(url) == Path(expected)


def test_rejects_non_sqlite_url():
    with pytest.raises(ValueError):
        backup_db.sqlite_path_from_url("postgresql+asyncpg://u@h/db")


def _make_db(path: Path, rows: int) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
    con.commit()
    con.close()


def test_backup_is_consistent_and_readable(tmp_path):
    src = tmp_path / "garmin.db"
    _make_db(src, 5)
    dest = tmp_path / "out.db"
    backup_db.make_backup(src, dest)

    # restore check: the copy opens and holds every row
    con = sqlite3.connect(str(dest))
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 5
    con.close()


def test_backup_overwrites_same_day(tmp_path):
    src = tmp_path / "garmin.db"
    _make_db(src, 1)
    dest = tmp_path / "out.db"
    backup_db.make_backup(src, dest)
    backup_db.make_backup(src, dest)  # second run same target must not raise
    assert dest.exists()


def test_run_writes_dated_file(tmp_path, monkeypatch):
    src = tmp_path / "garmin.db"
    _make_db(src, 3)
    monkeypatch.setattr(
        backup_db.settings, "DATABASE_URL", f"sqlite:///{src}", raising=False
    )
    out = tmp_path / "backups"
    dest = backup_db.run(out, on_date=date(2026, 7, 11))
    assert dest.name == "garmin-2026-07-11.db"
    assert dest.exists()


def _touch_backup(d: Path, iso: str) -> Path:
    p = d / f"garmin-{iso}.db"
    p.write_bytes(b"x")
    return p


def test_rotation_keeps_dailies_and_weeklies(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    # 14 consecutive days ending 2026-07-14
    made = [
        _touch_backup(d, date(2026, 7, day).isoformat()) for day in range(1, 15)
    ]
    backup_db.rotate(d, daily=7, weekly=4)
    kept = {p.name for p in d.glob("garmin-*.db")}

    # the 7 most recent days survive
    for day in range(8, 15):
        assert f"garmin-2026-07-{day:02d}.db" in kept
    # older-than-7-days dailies are pruned unless they're a kept weekly
    # (4 weekly slots keep the most-recent file of each of the last 4 ISO weeks)
    assert len(kept) <= 7 + 4
    assert len(kept) < len(made)  # something got pruned
