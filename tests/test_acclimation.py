"""NF-31 — heat & altitude acclimation as context, not as a guess.

The ACs this guards: a watch without the fields → every section absent and nothing breaks;
the pace adjustment is a deterministic table; a class change is mentioned once, not daily;
the race pack without acclimation data is exactly what it was (same context, same key)."""
import datetime as dt

from app import acclimation as acc
from app import fueling
from app.analysis.cache import _RACE_KEY_FIELDS, _as_dict, _cache_key, _context_cache_key
from app.analysis.client import CallStats

TODAY = dt.date(2026, 10, 28)


def _rows(values_by_offset, key=acc.HEAT_KEY):
    return [{"date": (TODAY - dt.timedelta(days=d)).isoformat(), "extra": {key: v}}
            for d, v in sorted(values_by_offset.items(), reverse=True)]


# ---------- extraction from Garmin's DTO ----------

def test_training_status_extraction_finds_nested_blocks():
    from app.garmin import service

    dto = {
        "mostRecentVO2Max": {"heatAltitudeAcclimation": {
            "heatAcclimationPercentage": 42, "altitudeAcclimation": 1200,
            "heatTrend": "ACCLIMATIZING", "altitudeTrend": None}},
        "mostRecentTrainingStatus": {"latestTrainingStatusData": {
            "3456789": {"trainingStatusFeedbackPhrase": "PRODUCTIVE_1"}}},
        "mostRecentTrainingLoadBalance": {"metricsTrainingLoadBalanceDTOMap": {
            "3456789": {"trainingBalanceFeedbackPhrase": "BALANCED"}}},
    }
    assert service.training_status_metrics(dto) == {
        "heat_acclimation_pct": 42, "altitude_acclimation_m": 1200,
        "heat_trend": "acclimatizing", "training_status": "productive_1",
        "load_balance": "balanced"}


def test_old_watch_yields_nothing():
    from app.garmin import service

    assert service.training_status_metrics({}) == {}
    assert service.training_status_metrics(None) == {}
    assert service.training_status_metrics({"userId": 1, "mostRecentVO2Max": {
        "generic": {"vo2MaxValue": 50}}}) == {}
    assert acc.status([{"date": "2026-10-28", "extra": {"resting_hr": 50}}]) is None
    assert acc.race_context(None, {"feels_max_c": 30}, 28) is None
    assert acc.weather_context(None) is None
    assert acc.change_context(None, "heat:none|alt:-") is None


def test_analyst_never_reads_the_raw_fields():
    payload = {"daily": [{"date": "2026-10-28", "extra": {
        "heat_acclimation_pct": 40, "altitude_acclimation_m": 900, "training_status": "x",
        "load_balance": "y", "heat_trend": "z", "resting_hr": 48}}],
        "recent_activities": [], "planned_runs": []}
    assert _as_dict(payload)["daily"][0]["extra"] == {"resting_hr": 48}


# ---------- classes, loss, the table ----------

def test_classes():
    assert [acc.heat_class(v) for v in (0, 29, 30, 69, 70, 100)] == [
        "none", "none", "partial", "partial", "acclimated", "acclimated"]
    assert [acc.altitude_class(v) for v in (0, 799, 800, 2000)] == [
        "none", "none", "partial", "acclimated"]
    assert acc.heat_class(None) is None and acc.altitude_class("x") is None


def test_loss_of_altitude_after_the_mountains():
    rows = _rows({10: 2400, 8: 2300, 1: 1500, 0: 1400}, key=acc.ALT_KEY)
    st = acc.status(rows, today=TODAY)
    assert st["altitude_class"] == "partial" and st["lost_altitude"] is True
    assert st["heat_class"] is None


def test_heat_loss_needs_a_real_drop():
    assert acc.status(_rows({9: 80, 0: 70}), today=TODAY)["lost_heat"] is False
    assert acc.status(_rows({9: 80, 0: 55}), today=TODAY)["lost_heat"] is True


def test_pace_table_is_deterministic():
    assert acc.heat_pace_pct(12, "none") is None              # cool day: nothing to adjust
    assert acc.heat_pace_pct(27, "none") == 5.0
    assert acc.heat_pace_pct(27, "partial") == 3.8
    assert acc.heat_pace_pct(27, "acclimated") == 2.5
    assert acc.heat_pace_pct(33, "none") == 8.0
    assert acc.heat_pace_pct(27, None) is None
    # same input, same answer — a table, not an improvisation
    assert {acc.heat_pace_pct(24.9, "partial") for _ in range(5)} == {2.2}


# ---------- "mention once" ----------

def test_change_context_first_reading_is_a_silent_baseline():
    st = acc.status(_rows({0: 20}), today=TODAY)
    assert acc.change_context(st, None) is None
    assert acc.change_context(st, acc.guard_value(st)) is None


def test_change_context_announces_a_class_change():
    st = acc.status(_rows({9: 80, 0: 50}), today=TODAY)
    ctx = acc.change_context(st, "heat:acclimated|alt:-")
    assert ctx["lost_heat"] is True
    assert "втрачається" in ctx["changes"][0] and "частково" in ctx["changes"][0]


async def test_daily_report_mentions_a_change_once(session, monkeypatch):
    from app.analysis import reports
    from app.db.models import DailyMetric
    from app.garmin import repository

    seen = []

    def fake_analyze(payload, question="", deep=False, kind=None, previous_report=None,
                     api_key=None, weather=None, plan_today=None, fitness=None,
                     records=None, norm=None, subjective=None, health_alerts=None,
                     fueling=None, today=None, intensity_ctx=None,
                     athlete_profile=None, away_ctx=None, plan_recent=None,
                     body_weight=None, acclimation=None):
        seen.append(acclimation)
        return f"звіт {len(seen)}", CallStats(kind=kind or "report", model="m")

    monkeypatch.setattr(reports, "analyze_with_stats", fake_analyze)
    uid = 3101
    today = dt.date.today()
    session.add(DailyMetric(user_id=uid, date=(today - dt.timedelta(days=1)).isoformat(),
                            hrv_avg=50, extra={acc.HEAT_KEY: 20}))
    await session.commit()
    empty = {"daily": [], "recent_activities": [], "planned_runs": []}

    await reports.run_analysis(session, empty, user_id=uid)        # baseline: silent
    assert seen[-1] is None
    assert await repository.get_state(session, uid, reports.ACCLIM_GUARD_KEY) == \
        "heat:none|alt:-"

    session.add(DailyMetric(user_id=uid, date=today.isoformat(), hrv_avg=51,
                            extra={acc.HEAT_KEY: 75}))
    await session.commit()
    await reports.run_analysis(session, empty, user_id=uid, question="перший")
    assert seen[-1] and "адаптований" in seen[-1]["changes"][0]
    await reports.run_analysis(session, empty, user_id=uid, question="другий")
    assert seen[-1] is None                                         # once, not daily


def test_cache_key_includes_acclimation_only_when_present():
    base = _cache_key({}, "q", "m")
    assert _cache_key({}, "q", "m", acclimation=None) == base
    assert _cache_key({}, "q", "m", acclimation={"changes": ["x"]}) != base


# ---------- race pack ----------

def test_race_context_adjusts_pace_and_advises_when_unacclimated():
    st = acc.status(_rows({0: 10}), today=TODAY)
    ctx = acc.race_context(st, {"feels_max_c": 29}, heat_feels_c=28)
    assert ctx["pace_adjust_pct"] == 5.0 and ctx["race_feels_c"] == 29
    assert "ту саму пору доби" in ctx["advice"]
    st = acc.status(_rows({0: 90}), today=TODAY)
    ctx = acc.race_context(st, {"feels_max_c": 29}, heat_feels_c=28)
    assert ctx["pace_adjust_pct"] == 2.5 and "advice" not in ctx
    # no forecast yet (race > 7 days out): the class only, no invented number
    assert "pace_adjust_pct" not in acc.race_context(st, None, heat_feels_c=28)


def test_race_key_unchanged_without_acclimation():
    from app.analysis.cache import _race_cache_key

    ctx = {"goal": "first_10k", "target_date": "2026-11-20", "target_dist_km": 10.0,
           "fitness": {"vo2max": 50}, "recent_sessions": [], "weather": None}
    assert _race_cache_key(ctx, "m") == _context_cache_key("race", ctx, "m", _RACE_KEY_FIELDS)
    assert _race_cache_key({**ctx, "acclimation": {"heat_class": "none"}}, "m") != \
        _race_cache_key(ctx, "m")


async def test_race_pack_without_data_is_exactly_as_before(session, monkeypatch):
    from types import SimpleNamespace

    from app.analysis import reports
    from app.garmin import repository

    captured = {}

    def fake_race(context, api_key=None):
        captured["ctx"] = context
        return "pack", CallStats(kind="race", model="m")

    monkeypatch.setattr(reports, "race_plan_with_stats", fake_race)
    uid = 3102
    today = dt.date.today()
    await repository.create_plan(
        session, uid, goal="first_10k", goal_label="10 км", start_date=today.isoformat(),
        target_date=(today + dt.timedelta(days=20)).isoformat(), days_per_week=3,
        intensity="moderate", intake={}, summary="", workouts=[SimpleNamespace(
            date=(today + dt.timedelta(days=2)).isoformat(), week=1, type="easy",
            dist_km=6.0, description="", steps=None)])
    await reports.run_race_plan(session, user_id=uid)
    assert "acclimation" not in captured["ctx"]


# ---------- fueling + weather ----------

def _long_session():
    return {"type": "long", "dist_km": 16.0}


def test_fueling_raises_fluid_for_the_unacclimated_on_a_hot_day():
    hot = {"feels_max_c": 31}
    base = fueling.advise(_long_session(), hot, heat_feels_c=28)
    assert base["fluid_ml_h"] == fueling.FLUID_ML_H_HOT and "unacclimated" not in base
    assert fueling.advise(_long_session(), hot, heat_feels_c=28,
                          acclimation_class="acclimated") == base
    un = fueling.advise(_long_session(), hot, heat_feels_c=28, acclimation_class="none")
    assert un["fluid_ml_h"] == acc.FLUID_ML_H_HOT_UNACCLIMATED and un["unacclimated"]
    assert any("не адаптований" in n for n in un["notes"])
    # a cool day: acclimation changes nothing
    cool = {"feels_max_c": 15}
    assert fueling.advise(_long_session(), cool, heat_feels_c=28, acclimation_class="none") \
        == fueling.advise(_long_session(), cool, heat_feels_c=28)


def test_weather_context_carries_only_what_exists():
    st = acc.status(_rows({0: 45}), today=TODAY)
    assert acc.weather_context(st) == {"heat_class": "partial", "heat_pct": 45}
