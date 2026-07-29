"""Relative day labels (app.daterel) + their wiring into the daily report.

The bug these cover: the analyst narrated a run from two days ago as "вчора" and
announced TOMORROW's plan session as today's. Both are date arithmetic the model was
being asked to do over a JSON of ISO dates — so the labels are now computed in Python
and asserted here, and the prompt only has to read them.
"""
import datetime as dt
import json
from unittest.mock import patch

import pytest

from app import daterel
from app.analysis import reports, service

# ---- pure labels -------------------------------------------------------------

@pytest.mark.parametrize("delta, expect", [
    (0, "сьогодні"), (-1, "вчора"), (-2, "позавчора"), (1, "завтра"), (2, "післязавтра"),
])
def test_label_near_days(delta, expect):
    today = dt.date(2026, 7, 29)
    got = daterel.label(today + dt.timedelta(days=delta), today)
    assert got.startswith(expect + " (")


def test_label_far_days_counts_in_both_directions():
    today = dt.date(2026, 7, 29)                      # Wednesday
    assert daterel.label("2026-07-24", today) == "5 дн тому (пт)"
    assert daterel.label("2026-08-03", today) == "через 5 дн (пн)"


def test_label_carries_the_weekday():
    # The weekday is a second anchor: "вчора (вт)" survives a mis-read delta.
    assert daterel.label("2026-07-28", dt.date(2026, 7, 29)) == "вчора (вт)"


def test_label_none_on_junk_or_missing_date():
    assert daterel.label(None, dt.date(2026, 7, 29)) is None
    assert daterel.label("not-a-date", dt.date(2026, 7, 29)) is None
    assert daterel.label("2026-07-29", None) is None


def test_parse_accepts_iso_datetime_string():
    assert daterel.parse("2026-07-29T08:35:00") == dt.date(2026, 7, 29)


def test_today_context_spells_out_the_anchor():
    assert daterel.today_context("2026-07-29") == {
        "today": "2026-07-29", "today_weekday": "середа"}


# ---- annotate ----------------------------------------------------------------

def test_annotate_labels_every_dated_dict():
    items = [{"date": "2026-07-28", "x": 1}, {"date": "2026-07-29", "x": 2}]
    out = daterel.annotate(items, "2026-07-29")
    assert [i["day"] for i in out] == ["вчора (вт)", "сьогодні (ср)"]


def test_annotate_never_mutates_the_input():
    # The payload is shared with the dedup cache and the 30s per-user memo (PERF-05) —
    # an in-place label would leak into a later request's data.
    items = [{"date": "2026-07-28"}]
    daterel.annotate(items, "2026-07-29")
    assert items == [{"date": "2026-07-28"}]


def test_annotate_passes_through_undated_and_non_dict_entries():
    items = [{"no_date": 1}, "рядок", {"date": "junk"}]
    assert daterel.annotate(items, "2026-07-29") == items


def test_annotate_ignores_a_non_list():
    assert daterel.annotate(None, "2026-07-29") is None


# ---- wiring into the report prompt -------------------------------------------

def _capture_prompt(payload, **kw):
    """Run analyze_with_stats against a stubbed Anthropic client; return the user JSON."""
    sent = {}

    class _Msg:
        stop_reason = "end_turn"
        usage = None
        content = [type("B", (), {"type": "text", "text": "звіт"})()]

    class _Client:
        class messages:
            @staticmethod
            def create(**call):
                sent.update(json.loads(call["messages"][0]["content"]))
                return _Msg()

    with patch.object(reports, "_get_client", lambda _k: _Client()):
        reports.analyze_with_stats(payload, **kw)
    return sent


_PAYLOAD = {
    "daily": [{"date": "2026-07-27"}, {"date": "2026-07-28"}, {"date": "2026-07-29"}],
    "recent_activities": [{"date": "2026-07-27", "type": "running", "dist_km": 4.7}],
    "planned_runs": [{"date": "2026-08-02", "title": "long"}],
}


def test_report_context_labels_daily_activities_and_planned():
    sent = _capture_prompt(_PAYLOAD, today="2026-07-29")
    assert sent["today"] == "2026-07-29" and sent["today_weekday"] == "середа"
    assert [d["day"] for d in sent["data"]["daily"]] == [
        "позавчора (пн)", "вчора (вт)", "сьогодні (ср)"]
    # the screenshot's bug: a run from позавчора must not be labelled "вчора"
    assert sent["data"]["recent_activities"][0]["day"] == "позавчора (пн)"
    assert sent["data"]["planned_runs"][0]["day"] == "через 4 дн (нд)"


def test_tomorrows_plan_session_is_labelled_tomorrow_not_today():
    # plan_today is a *window* (today+tomorrow); when today's session is absent (no
    # session, or already done/skipped) the remaining entry is tomorrow's — the exact
    # case the bot narrated as "сьогодні за планом".
    sent = _capture_prompt(
        _PAYLOAD, today="2026-07-29",
        plan_today=[{"date": "2026-07-30", "type": "easy", "dist_km": 2.4}],
    )
    assert sent["plan_today"][0]["day"] == "завтра (чт)"


def test_previous_report_gets_its_own_relative_label():
    sent = _capture_prompt(_PAYLOAD, today="2026-07-29",
                           previous_report={"date": "2026-07-27", "text": "старий звіт"})
    assert sent["previous_report"]["day"] == "позавчора (пн)"
    assert sent["previous_report"]["text"] == "старий звіт"


def test_report_context_labels_records():
    sent = _capture_prompt(_PAYLOAD, today="2026-07-29",
                           records=[{"kind": "run_5k", "date": "2026-07-28"}])
    assert sent["records"][0]["day"] == "вчора (вт)"


def test_analyze_does_not_mutate_the_caller_payload():
    payload = {"daily": [{"date": "2026-07-29"}], "recent_activities": [],
               "planned_runs": []}
    _capture_prompt(payload, today="2026-07-29")
    assert payload["daily"] == [{"date": "2026-07-29"}]


def test_activity_analysis_labels_the_activity_day_outside_the_cached_payload():
    sent = {}

    class _Msg:
        usage = None
        content = [type("B", (), {"type": "text", "text": "розбір"})()]

    class _Client:
        class messages:
            @staticmethod
            def create(**call):
                sent.update(json.loads(call["messages"][0]["content"]))
                return _Msg()

    activity = {"date": dt.date.today().isoformat(), "type": "running"}
    with patch.object(reports, "_get_client", lambda _k: _Client()):
        reports.analyze_activity_with_stats(activity)
    assert sent["activity_day"].startswith("сьогодні (")
    # the label must stay OUT of `activity` — that dict is the dedup-cache key, and a
    # daily-changing label inside it would mean a paid re-run every midnight.
    assert "day" not in sent["activity"]


# ---- the user's own timezone decides "today" ---------------------------------

def test_user_tz_falls_back_on_a_bad_zone():
    from types import SimpleNamespace

    from app.core import tz

    assert tz.user_tz(SimpleNamespace(timezone="Europe/Kyiv")).key == "Europe/Kyiv"
    assert tz.user_tz(SimpleNamespace(timezone="Mars/Olympus")) is tz.DEFAULT_TZ
    assert tz.user_tz(SimpleNamespace(timezone=None)) is tz.DEFAULT_TZ


def test_user_today_is_that_users_local_date():
    from types import SimpleNamespace

    from app.core import tz

    user = SimpleNamespace(timezone="Pacific/Kiritimati")   # UTC+14
    assert tz.user_today(user) == dt.datetime.now(tz.user_tz(user)).date()



async def test_run_analysis_uses_the_passed_today_for_plan_window_and_labels(
        session, monkeypatch):
    """A user whose local date is ahead of the process's must get THEIR day: the plan
    window and every label are built from the ``today`` the caller passes down."""
    from app.garmin import repository
    from app.garmin.schemas import PlanWorkout

    U1 = 741
    user_today = dt.date.today() + dt.timedelta(days=1)   # stand-in for "ahead of process"
    await repository.create_plan(
        session, U1, goal="general", goal_label="Загальна форма", target_date=None,
        start_date=user_today.isoformat(), days_per_week=3, intensity="moderate",
        intake={}, summary="",
        workouts=[PlanWorkout(date=user_today.isoformat(), week=1, type="easy",
                              dist_km=5.0, description="легкий")],
    )

    captured = {}

    def fake_analyze(payload, question="", deep=False, kind=None, previous_report=None,
                     api_key=None, weather=None, plan_today=None, fitness=None,
                     records=None, norm=None, subjective=None, health_alerts=None,
                     fueling=None, today=None):
        captured.update(plan_today=plan_today, today=today)
        return "звіт", service.CallStats(kind=kind or "report", model="m")

    monkeypatch.setattr(reports, "analyze_with_stats", fake_analyze)
    await service.run_analysis(
        session, {"daily": [], "recent_activities": [], "planned_runs": []},
        user_id=U1, question="q", today=user_today,
    )

    assert captured["today"] == user_today.isoformat()
    # the session on the user's own today is in the window and reads as today's
    assert captured["plan_today"][0]["date"] == user_today.isoformat()
    assert daterel.label(captured["plan_today"][0]["date"],
                         captured["today"]).startswith("сьогодні")
