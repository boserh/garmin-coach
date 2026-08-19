"""OPS-02 · backup script: consistent copy + rotation + a real restore check.

The restore check (open the backup, read the rows back) is the AC's "backup is
actually readable" test — the one without which everything else is theatre.
"""
import json
import sqlite3
import subprocess
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


def test_run_writes_freshness_marker(tmp_path, monkeypatch):
    """OPS-08: a successful run writes last_ok.json — the source app.backup_status reads."""
    import json

    from app.backup_status import MARKER_NAME

    src = tmp_path / "garmin.db"
    _make_db(src, 1)
    monkeypatch.setattr(
        backup_db.settings, "DATABASE_URL", f"sqlite:///{src}", raising=False
    )
    out = tmp_path / "backups"
    dest = backup_db.run(out, on_date=date(2026, 7, 11))

    marker = json.loads((out / MARKER_NAME).read_text())
    assert marker["path"] == str(dest)
    assert marker["size"] > 0
    assert marker["rsync_ok"] is None  # no --rsync-dest given
    assert isinstance(marker["ts"], float)


def test_run_marks_rsync_failure_without_losing_local_success(tmp_path, monkeypatch):
    """A failed off-SD rsync must not hide that the local backup itself succeeded —
    the marker records both independently, and the failure still propagates (nonzero
    exit for cron) rather than being silently swallowed."""
    import json

    import pytest

    from app.backup_status import MARKER_NAME

    src = tmp_path / "garmin.db"
    _make_db(src, 1)
    monkeypatch.setattr(
        backup_db.settings, "DATABASE_URL", f"sqlite:///{src}", raising=False
    )

    def _boom(backup_dir, dest, **kwargs):
        raise RuntimeError("rsync unreachable")

    monkeypatch.setattr(backup_db, "_rsync", _boom)
    out = tmp_path / "backups"
    with pytest.raises(RuntimeError):
        backup_db.run(out, rsync_dest="user@host:/backups/", on_date=date(2026, 7, 11))

    marker = json.loads((out / MARKER_NAME).read_text())
    assert marker["rsync_ok"] is False
    assert "rsync unreachable" in marker["rsync_error"]  # the reason, not just the fact
    assert (out / "garmin-2026-07-11.db").exists()  # the local backup is still there


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


# --- the off-SD copy: why it failed, and which failures are worth a retry -------------
# `rsync_ok: false` with no reason is what sent an admin ssh-ing into the Pi to find out
# whether the stick was unplugged, full, or read-only.


def _cpe(code: int, stderr: str = "") -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(code, ["rsync"], output="", stderr=stderr)


def test_rsync_reason_reads_the_stderr_cause():
    exc = _cpe(23, 'rsync: [Receiver] mkdir "/mnt/backup/garmin" failed: '
                   "Read-only file system (30)\nrsync error: some files could not be "
                   "transferred (code 23) at main.c(1338)\n")
    reason = backup_db._rsync_reason(exc)
    assert "exit 23" in reason
    assert "Read-only file system" in reason


def test_rsync_reason_survives_a_silent_failure():
    assert backup_db._rsync_reason(_cpe(13)) == "rsync exit 13"


def test_rsync_reason_names_a_timeout_and_a_missing_binary():
    assert "timed out" in backup_db._rsync_reason(
        subprocess.TimeoutExpired(["rsync"], backup_db._RSYNC_TIMEOUT_S))
    assert "not installed" in backup_db._rsync_reason(FileNotFoundError(2, "no rsync"))


def test_rsync_retries_a_transient_failure_then_succeeds(tmp_path, monkeypatch):
    """A busy stick / dropped ssh loses the whole night's off-SD copy if one attempt is
    all we get — the timer only comes back tomorrow."""
    calls = []

    def _flaky(backup_dir, dest):
        calls.append(dest)
        if len(calls) == 1:
            raise _cpe(30, "rsync: connection unexpectedly closed")

    monkeypatch.setattr(backup_db, "_rsync_once", _flaky)
    monkeypatch.setattr(backup_db.time, "sleep", lambda s: None)
    backup_db._rsync(tmp_path, "user@host:/backups/")
    assert len(calls) == 2


def test_rsync_does_not_retry_a_permanent_failure(tmp_path, monkeypatch):
    """A missing destination repeats identically; retrying only delays the report."""
    calls = []

    def _boom(backup_dir, dest):
        calls.append(dest)
        raise _cpe(13, "rsync: change_dir /mnt/backup failed: No such file or directory")

    monkeypatch.setattr(backup_db, "_rsync_once", _boom)
    monkeypatch.setattr(backup_db.time, "sleep", lambda s: None)
    with pytest.raises(backup_db.RsyncFailed) as ei:
        backup_db._rsync(tmp_path, "/mnt/backup/garmin/")
    assert len(calls) == 1
    assert "No such file or directory" in str(ei.value)


def test_rsync_does_not_retry_an_unwritable_destination(tmp_path, monkeypatch):
    """The exit code alone lies: an unwritable mount and a flaky USB both exit 11, and
    the first one repeats forever. This is the real Pi failure — /mnt/backup not writable
    by the service user — retried twice for nothing before the cause was read."""
    calls = []

    def _boom(backup_dir, dest):
        calls.append(dest)
        raise _cpe(11, 'rsync: [Receiver] mkdir "/mnt/backup/garmin" failed: '
                       "Permission denied (13)\nrsync error: error in file IO (code 11) "
                       "at main.c(800) [Receiver=3.4.1]\n")

    monkeypatch.setattr(backup_db, "_rsync_once", _boom)
    monkeypatch.setattr(backup_db.time, "sleep", lambda s: None)
    with pytest.raises(backup_db.RsyncFailed) as ei:
        backup_db._rsync(tmp_path, "/mnt/backup/garmin/")
    assert len(calls) == 1
    assert "Permission denied" in str(ei.value)


def test_rsync_still_retries_a_genuine_io_blip(tmp_path, monkeypatch):
    """Same exit code, no permanent cause named → still worth a retry."""
    calls = []

    def _flaky(backup_dir, dest):
        calls.append(dest)
        if len(calls) == 1:
            raise _cpe(11, "rsync: write failed on \"/mnt/backup/garmin/x.db\": "
                           "Input/output error (5)")

    monkeypatch.setattr(backup_db, "_rsync_once", _flaky)
    monkeypatch.setattr(backup_db.time, "sleep", lambda s: None)
    backup_db._rsync(tmp_path, "user@host:/backups/")
    assert len(calls) == 2


def test_rsync_gives_up_after_the_retries(tmp_path, monkeypatch):
    calls = []

    def _boom(backup_dir, dest):
        calls.append(dest)
        raise _cpe(30, "timeout in data send/receive")

    monkeypatch.setattr(backup_db, "_rsync_once", _boom)
    monkeypatch.setattr(backup_db.time, "sleep", lambda s: None)
    with pytest.raises(backup_db.RsyncFailed):
        backup_db._rsync(tmp_path, "user@host:/backups/")
    assert len(calls) == backup_db._RSYNC_RETRIES + 1


def test_run_records_the_real_rsync_reason_in_the_marker(tmp_path, monkeypatch):
    """End to end: the marker /status and the morning DM read carries the cause."""
    import json

    from app.backup_status import MARKER_NAME

    src = tmp_path / "garmin.db"
    _make_db(src, 1)
    monkeypatch.setattr(
        backup_db.settings, "DATABASE_URL", f"sqlite:///{src}", raising=False
    )

    def _boom(backup_dir, dest):
        raise _cpe(23, "rsync: [sender] failed: Read-only file system (30)")

    monkeypatch.setattr(backup_db, "_rsync_once", _boom)
    monkeypatch.setattr(backup_db.time, "sleep", lambda s: None)
    out = tmp_path / "backups"
    with pytest.raises(backup_db.RsyncFailed):
        backup_db.run(out, rsync_dest="/mnt/backup/garmin/", on_date=date(2026, 7, 11))

    marker = json.loads((out / MARKER_NAME).read_text())
    assert marker["rsync_ok"] is False
    assert "Read-only file system" in marker["rsync_error"]


def test_successful_run_leaves_no_stale_reason(tmp_path, monkeypatch):
    import json

    from app.backup_status import MARKER_NAME

    src = tmp_path / "garmin.db"
    _make_db(src, 1)
    monkeypatch.setattr(
        backup_db.settings, "DATABASE_URL", f"sqlite:///{src}", raising=False
    )
    monkeypatch.setattr(backup_db, "_rsync_once", lambda backup_dir, dest: None)
    out = tmp_path / "backups"
    backup_db.run(out, rsync_dest="user@host:/backups/", on_date=date(2026, 7, 11))

    marker = json.loads((out / MARKER_NAME).read_text())
    assert marker["rsync_ok"] is True
    assert marker["rsync_error"] is None


# --- the false green: an unmounted mount point ---------------------------------------
# `findmnt /mnt/backup` on the Pi printed nothing while /mnt/backup existed as a plain
# root-owned directory on the SD card. Had it been writable, every "off-SD" backup would
# have been copied onto the very card the backups exist to survive — and /status would
# have been green throughout.


def test_local_dest_on_the_same_filesystem_is_not_an_off_sd_copy(tmp_path, monkeypatch):
    src = tmp_path / "garmin.db"
    _make_db(src, 1)
    monkeypatch.setattr(
        backup_db.settings, "DATABASE_URL", f"sqlite:///{src}", raising=False
    )
    monkeypatch.setattr(backup_db, "_rsync_once", lambda backup_dir, dest: None)
    dest = tmp_path / "fake-mountpoint"          # same tmpfs/disk as the backups
    dest.mkdir()

    out = tmp_path / "backups"
    with pytest.raises(backup_db.RsyncFailed) as ei:
        backup_db.run(out, rsync_dest=str(dest), on_date=date(2026, 7, 11))
    assert "not an off-SD copy" in str(ei.value)

    marker = json.loads((out / backup_db.MARKER_NAME).read_text())
    assert marker["rsync_ok"] is False
    # the verdict and the command to run survive the marker's length cap
    assert marker["rsync_error"].startswith("not an off-SD copy")
    assert "findmnt" in marker["rsync_error"]


def test_same_filesystem_check_is_not_retried(tmp_path, monkeypatch):
    """No amount of waiting mounts a USB stick."""
    calls = []
    monkeypatch.setattr(backup_db, "_rsync_once",
                        lambda backup_dir, dest: calls.append(dest))
    monkeypatch.setattr(backup_db.time, "sleep", lambda s: None)
    dest = tmp_path / "mnt"
    dest.mkdir()
    with pytest.raises(backup_db.RsyncFailed):
        backup_db._rsync(tmp_path, str(dest))
    assert len(calls) == 1


def test_a_genuinely_separate_disk_passes(tmp_path, monkeypatch):
    devices = {}

    def _dev(path):
        return devices.get(Path(path).name, 1)

    devices["mnt"] = 2                              # the USB is mounted → its own st_dev
    monkeypatch.setattr(backup_db, "_device_of", _dev)
    monkeypatch.setattr(backup_db, "_rsync_once", lambda backup_dir, dest: None)
    backup_db._rsync(tmp_path, str(tmp_path / "mnt"))   # no raise


def test_remote_destinations_skip_the_check(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_db, "_rsync_once", lambda backup_dir, dest: None)
    for dest in ("user@host:/backups/", "host:/backups/", "rsync://host/backups"):
        assert backup_db._is_local_dest(dest) is False
        backup_db._rsync(tmp_path, dest)            # no raise, no stat games


def test_allow_same_fs_opts_out(tmp_path, monkeypatch):
    """An escape hatch, so a deliberate same-disk copy isn't an unfixable wall."""
    monkeypatch.setattr(backup_db, "_rsync_once", lambda backup_dir, dest: None)
    dest = tmp_path / "mnt"
    dest.mkdir()
    backup_db._rsync(tmp_path, str(dest), check_off_sd=False)   # no raise


@pytest.mark.parametrize("dest,local", [
    ("/mnt/backup/garmin/", True),
    ("backups-copy/", True),
    ("./x", True),
    ("user@host:/backups/", False),
    ("host:/backups/", False),
    ("rsync://host/mod", False),
])
def test_local_dest_detection(dest, local):
    assert backup_db._is_local_dest(dest) is local
