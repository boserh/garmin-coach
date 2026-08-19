"""OPS-08 · backup freshness: marker parsing + the warn/guard cadence."""
import json
import time

from app import backup_status


def test_missing_marker_is_honest_none(tmp_path):
    st = backup_status.read_status(tmp_path)
    assert st == {"age_hours": None, "rsync_ok": None, "rsync_error": None}


def test_corrupt_marker_is_none(tmp_path):
    (tmp_path / backup_status.MARKER_NAME).write_text("not json")
    st = backup_status.read_status(tmp_path)
    assert st["age_hours"] is None


def test_fresh_marker_reports_age_and_rsync(tmp_path):
    now = time.time()
    marker = {"ts": now - 3600, "path": "x", "size": 10, "rsync_ok": True}
    (tmp_path / backup_status.MARKER_NAME).write_text(json.dumps(marker))
    st = backup_status.read_status(tmp_path, now=now)
    assert st["age_hours"] == 1.0
    assert st["rsync_ok"] is True


def test_rsync_ok_missing_is_none(tmp_path):
    marker = {"ts": time.time(), "path": "x", "size": 10}
    (tmp_path / backup_status.MARKER_NAME).write_text(json.dumps(marker))
    st = backup_status.read_status(tmp_path)
    assert st["rsync_ok"] is None


def test_should_warn_known_stale_is_daily():
    # over threshold, never warned yet → warn
    assert backup_status.should_warn(80.0, None, "2026-07-31", 3) is True
    # already warned today → silent
    assert backup_status.should_warn(80.0, "2026-07-31", "2026-07-31", 3) is False
    # warned yesterday, still stale → warn again today (daily cadence)
    assert backup_status.should_warn(80.0, "2026-07-30", "2026-07-31", 3) is True


def test_should_warn_known_fresh_is_silent():
    assert backup_status.should_warn(2.0, None, "2026-07-31", 3) is False


def test_should_warn_missing_marker_is_every_warn_days():
    # never warned → warn once
    assert backup_status.should_warn(None, None, "2026-07-31", 3) is True
    # warned yesterday → too soon, stay silent (unlike the known-stale daily cadence)
    assert backup_status.should_warn(None, "2026-07-30", "2026-07-31", 3) is False
    # warned 3 days ago → due again
    assert backup_status.should_warn(None, "2026-07-28", "2026-07-31", 3) is True


def test_should_warn_disabled_threshold():
    assert backup_status.should_warn(1000.0, None, "2026-07-31", 0) is False


def test_marker_carries_the_rsync_reason(tmp_path):
    """`rsync_ok: false` alone sends the admin to ssh; the reason is what they act on."""
    marker = {"ts": time.time(), "path": "x", "size": 1, "rsync_ok": False,
              "rsync_error": "rsync exit 23: rsync: [Receiver] mkdir "
                             "\"/mnt/backup/garmin\" failed: Read-only file system (30)"}
    (tmp_path / backup_status.MARKER_NAME).write_text(json.dumps(marker))
    st = backup_status.read_status(tmp_path)
    assert st["rsync_ok"] is False
    assert "Read-only file system" in st["rsync_error"]


def test_rsync_reason_is_absent_not_empty(tmp_path):
    """Markers written before the reason existed, and successful runs, have no reason —
    that must read as None, never as an empty string the UI would then render."""
    for extra in ({}, {"rsync_error": None}, {"rsync_error": "   "}, {"rsync_error": 7}):
        marker = {"ts": time.time(), "path": "x", "size": 1, "rsync_ok": True, **extra}
        (tmp_path / backup_status.MARKER_NAME).write_text(json.dumps(marker))
        assert backup_status.read_status(tmp_path)["rsync_error"] is None


def test_rsync_reason_is_trimmed(tmp_path):
    marker = {"ts": time.time(), "path": "x", "size": 1, "rsync_ok": False,
              "rsync_error": "e" * 5000}
    (tmp_path / backup_status.MARKER_NAME).write_text(json.dumps(marker))
    assert len(backup_status.read_status(tmp_path)["rsync_error"]) == 200


def test_should_warn_rsync_fires_while_the_local_backup_is_fresh():
    """The gap this closes: an hour-old local backup keeps should_warn silent forever
    while the off-SD copy has been failing for months."""
    assert backup_status.should_warn(1.0, None, "2026-07-31", 3) is False
    assert backup_status.should_warn_rsync(False, None, "2026-07-31", 3) is True


def test_should_warn_rsync_cadence_is_every_warn_days():
    # warned yesterday → too soon (a degraded off-SD copy is not a daily nag)
    assert backup_status.should_warn_rsync(False, "2026-07-30", "2026-07-31", 3) is False
    # warned 3 days ago → due again
    assert backup_status.should_warn_rsync(False, "2026-07-28", "2026-07-31", 3) is True


def test_should_warn_rsync_silent_when_ok_or_unconfigured():
    assert backup_status.should_warn_rsync(True, None, "2026-07-31", 3) is False
    # None = no --rsync-dest at all: a deployment choice, not a failure
    assert backup_status.should_warn_rsync(None, None, "2026-07-31", 3) is False
    assert backup_status.should_warn_rsync(False, None, "2026-07-31", 0) is False
