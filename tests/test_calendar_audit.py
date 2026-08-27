"""The calendar audit: what Garmin holds that our plan rows don't account for (pure)."""
import datetime as dt
from types import SimpleNamespace

from app.garmin import calendar_audit


def _row(rid, date, *, wid=None, sched=None, typ="easy"):
    return SimpleNamespace(id=rid, date=date, type=typ,
                           garmin_workout_id=wid, garmin_schedule_id=sched)


def test_half_pushed_row_is_reported():
    found = calendar_audit.audit(
        [_row(1, "2026-08-27", wid=111)], [{"id": 111, "name": "🌿 Easy"}], [])
    assert found["half_pushed"] == [
        {"row_id": 1, "date": "2026-08-27", "type": "easy", "workout_id": 111}]
    assert found["missing_on_garmin"] == []


def test_row_pointing_at_a_deleted_workout_is_reported():
    found = calendar_audit.audit([_row(1, "2026-08-27", wid=111, sched=222)], [], [])
    assert [r["workout_id"] for r in found["missing_on_garmin"]] == [111]
    assert found["half_pushed"] == []


def test_untracked_workout_reported_only_when_the_name_looks_like_ours():
    saved = [{"id": 111, "name": "🏋️ Day 1"},      # ours: carries a push mark
             {"id": 222, "name": "Runna Tempo"},   # the athlete's own — never ours to touch
             {"id": 333, "name": "🌿 Easy 5km · W2"}]
    found = calendar_audit.audit([_row(1, "2026-08-27", wid=333, sched=9)], saved, [])
    assert [r["workout_id"] for r in found["untracked_workouts"]] == [111]


def test_untracked_scheduled_flags_whether_the_workout_still_exists():
    """The live 2026-08-27 shape: a calendar entry whose workout was deleted underneath it —
    Garmin refuses to unschedule that, so the audit's job is to make it visible."""
    items = [
        {"itemType": "workout", "date": "2026-08-27", "workoutId": 111, "title": "🏋️  Day 1"},
        {"itemType": "workout", "date": "2026-08-27", "workoutId": 999, "title": "🏋️ Day 1"},
        {"itemType": "activity", "date": "2026-08-27", "workoutId": 555, "title": "🌿 ran"},
    ]
    found = calendar_audit.audit([_row(1, "2026-08-27", wid=999, sched=9)],
                                 [{"id": 999, "name": "🏋️ Day 1"}], items)
    assert found["untracked_scheduled"] == [
        {"workout_id": 111, "date": "2026-08-27", "title": "🏋️  Day 1", "exists": False}]


def test_agreeing_calendar_reports_nothing():
    found = calendar_audit.audit(
        [_row(1, "2026-08-27", wid=111, sched=222)],
        [{"id": 111, "name": "🌿 Easy 5km"}],
        [{"itemType": "workout", "date": "2026-08-27", "workoutId": 111, "title": "🌿 Easy"}])
    assert not any(found.values())


def test_calendar_months_covers_this_month_and_the_next_zero_based():
    assert calendar_audit.calendar_months(dt.date(2026, 8, 27)) == [(2026, 7), (2026, 8)]
    assert calendar_audit.calendar_months(dt.date(2026, 12, 5)) == [(2026, 11), (2027, 0)]
