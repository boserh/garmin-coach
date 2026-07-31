"""OPS-08 · backup freshness: marker parsing + the warn/guard cadence."""
import json
import time

from app import backup_status


def test_missing_marker_is_honest_none(tmp_path):
    st = backup_status.read_status(tmp_path)
    assert st == {"age_hours": None, "rsync_ok": None}


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
