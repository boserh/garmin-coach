"""NF-10: `/goal` progress projection — pure Python, no DB/LLM."""
import datetime as dt

from app import goal


def _row(date, **kw):
    return {"date": date, **kw}


def _weekly_race_history(start, weeks, seconds_per_week, *, key="race_5k_s"):
    """One reading per week (Monday), so weekly_medians has exactly `weeks` buckets."""
    out = []
    d = dt.date.fromisoformat(start)
    for i in range(weeks):
        out.append(_row((d + dt.timedelta(weeks=i)).isoformat(), **{key: seconds_per_week[i]}))
    return out


# ---------- metric_for_goal ----------

def test_metric_for_goal_race_goals():
    assert goal.metric_for_goal("first_5k") == ("race_5k_s", "прогноз на 5 км", False)
    assert goal.metric_for_goal("faster_5k")[0] == "race_5k_s"
    assert goal.metric_for_goal("first_10k")[0] == "race_10k_s"
    assert goal.metric_for_goal("first_half")[0] == "race_half_s"


def test_metric_for_goal_falls_back_to_vo2max():
    assert goal.metric_for_goal("general") == ("vo2max", "VO2max (загальна форма)", True)
    assert goal.metric_for_goal(None)[0] == "vo2max"
    assert goal.metric_for_goal("unknown_goal")[0] == "vo2max"


# ---------- weekly_medians ----------

def test_weekly_medians_buckets_by_iso_week():
    history = [
        _row("2026-06-01", race_5k_s=1600), _row("2026-06-03", race_5k_s=1620),  # same week
        _row("2026-06-08", race_5k_s=1580),
    ]
    wm = goal.weekly_medians(history, "race_5k_s")
    assert len(wm) == 2
    assert wm[0]["median"] == 1620   # upper-median of [1600, 1620]
    assert wm[1]["median"] == 1580


def test_weekly_medians_ignores_missing_metric():
    history = [_row("2026-06-01", vo2max=48), _row("2026-06-02", race_5k_s=1600)]
    assert len(goal.weekly_medians(history, "race_5k_s")) == 1


def test_weekly_medians_backfill_gap_just_skips_a_week():
    history = [_row("2026-01-01", race_5k_s=1700), _row("2026-06-01", race_5k_s=1600)]
    wm = goal.weekly_medians(history, "race_5k_s")
    assert len(wm) == 2   # no interpolation, no crash on the gap


# ---------- project ----------

def test_project_none_under_min_weeks():
    history = _weekly_race_history("2026-06-01", 2, [1600, 1590])
    assert goal.project(history, metric_key="race_5k_s") is None


def test_project_improving_trend_5k():
    # 5 weeks, steadily improving (faster) by 10s/week
    history = _weekly_race_history("2026-06-01", 5, [1650, 1640, 1630, 1620, 1610])
    proj = goal.project(history, metric_key="race_5k_s", today=dt.date(2026, 7, 6))
    assert proj["current"] == 1610
    assert proj["slope_per_week"] == -10.0
    assert proj["n_weeks"] == 5


def test_project_within_horizon_produces_projection():
    history = _weekly_race_history("2026-06-01", 5, [1650, 1640, 1630, 1620, 1610])
    target_date = "2026-08-03"   # ~4 weeks after the last reading (2026-07-06)
    proj = goal.project(
        history, metric_key="race_5k_s", target_date=target_date, today=dt.date(2026, 7, 6))
    assert proj["weeks_to_target"] is not None
    assert proj["projected"] < proj["current"]   # still improving → projects faster


def test_project_beyond_far_horizon_has_no_projection():
    history = _weekly_race_history("2026-06-01", 5, [1650, 1640, 1630, 1620, 1610])
    far = (dt.date(2026, 7, 6) + dt.timedelta(weeks=goal.FAR_HORIZON_WEEKS + 4)).isoformat()
    proj = goal.project(
        history, metric_key="race_5k_s", target_date=far, today=dt.date(2026, 7, 6))
    assert proj["projected"] is None
    assert proj["weeks_to_target"] is None
    assert proj["target_date"] == far


def test_project_verdict_on_track_when_projection_beats_target():
    history = _weekly_race_history("2026-06-01", 5, [1650, 1640, 1630, 1620, 1610])
    proj = goal.project(
        history, metric_key="race_5k_s", target_date="2026-08-03", target_s=1500,
        today=dt.date(2026, 7, 6))
    # projects to ~1610 - 10*4 = 1570s, still above the 1500 target → not on_track
    assert proj["verdict"] in ("close", "behind")


def test_project_verdict_on_track_when_already_faster_than_target():
    history = _weekly_race_history("2026-06-01", 5, [1650, 1640, 1630, 1620, 1610])
    proj = goal.project(
        history, metric_key="race_5k_s", target_date="2026-08-03", target_s=1650,
        today=dt.date(2026, 7, 6))
    assert proj["verdict"] == "on_track"


def test_project_verdict_higher_better_metric():
    # VO2max rising 0.5/week, target is a HIGHER vo2max — higher_better flips the compare
    history = _weekly_race_history(
        "2026-06-01", 5, [44, 44.5, 45, 45.5, 46], key="vo2max")
    proj = goal.project(
        history, metric_key="vo2max", higher_better=True,
        target_date="2026-08-03", target_s=50, today=dt.date(2026, 7, 6))
    assert proj["verdict"] in ("close", "behind")   # still short of 50


def test_project_no_verdict_without_target_s():
    history = _weekly_race_history("2026-06-01", 5, [1650, 1640, 1630, 1620, 1610])
    proj = goal.project(
        history, metric_key="race_5k_s", target_date="2026-08-03", today=dt.date(2026, 7, 6))
    assert proj["verdict"] is None


def test_project_no_target_date_at_all():
    history = _weekly_race_history("2026-06-01", 5, [1650, 1640, 1630, 1620, 1610])
    proj = goal.project(history, metric_key="race_5k_s")
    assert proj["projected"] is None
    assert proj["target_date"] is None


# ---------- fmt_time ----------

def test_fmt_time_minutes_and_hours():
    assert goal.fmt_time(1610) == "26:50"
    assert goal.fmt_time(5400) == "1:30:00"
    assert goal.fmt_time(None) == "—"


def test_fmt_time_negative_shows_sign():
    assert goal.fmt_time(-30) == "-0:30"


# ---------- summary ----------

def test_summary_stable_trend_no_target():
    history = _weekly_race_history("2026-06-01", 5, [1610, 1610, 1611, 1610, 1610])
    proj = goal.project(history, metric_key="race_5k_s")
    text = goal.summary(proj, label="прогноз на 5 км")
    assert "стабільно" in text
    assert "Ціль без дати" in text


def test_summary_improving_trend_with_projection():
    history = _weekly_race_history("2026-06-01", 5, [1650, 1640, 1630, 1620, 1610])
    proj = goal.project(
        history, metric_key="race_5k_s", target_date="2026-08-03", today=dt.date(2026, 7, 6))
    text = goal.summary(proj, label="прогноз на 5 км")
    assert "покращення" in text
    assert "Проєкція" in text


def test_summary_vo2max_formats_as_plain_number_not_time():
    history = _weekly_race_history("2026-06-01", 5, [44, 44.5, 45, 45.5, 46], key="vo2max")
    proj = goal.project(history, metric_key="vo2max", higher_better=True)
    text = goal.summary(proj, label="VO2max (загальна форма)")
    assert "46" in text.split("\n")[0]
    assert "0:46" not in text and "46:00" not in text   # not mistakenly formatted as time


def test_summary_far_horizon_message():
    history = _weekly_race_history("2026-06-01", 5, [1650, 1640, 1630, 1620, 1610])
    far = (dt.date(2026, 7, 6) + dt.timedelta(weeks=goal.FAR_HORIZON_WEEKS + 4)).isoformat()
    proj = goal.project(
        history, metric_key="race_5k_s", target_date=far, today=dt.date(2026, 7, 6))
    text = goal.summary(proj, label="прогноз на 5 км")
    assert "задалеко" in text
