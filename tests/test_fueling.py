"""NF-11 heat/duration fueling advisor: the pure calculator (app.fueling) on synthetic
sessions/forecasts, plus its wiring into run_analysis (only for TODAY's key session)."""
import datetime as dt

from app import fueling

_LONG_HOT = {"date": "2026-07-22", "t_min_c": 20, "t_max_c": 35, "feels_max_c": 34,
             "precip_mm": 0, "precip_prob_pct": 5, "wind_max_kmh": 8, "summary": "спекотно",
             "hourly": [{"h": 6, "t_c": 22, "feels_c": 22, "precip_pct": 0, "wind_kmh": 5},
                        {"h": 18, "t_c": 30, "feels_c": 33, "precip_pct": 0, "wind_kmh": 8}]}

_LONG_COOL = {"date": "2026-07-22", "t_min_c": 10, "t_max_c": 18, "feels_max_c": 17,
              "precip_mm": 0, "precip_prob_pct": 5, "wind_max_kmh": 8, "summary": "прохолодно"}

_SHORT_EASY_SESSION = {"date": "2026-07-22", "type": "easy", "dist_km": 5.0}
_LONG_SESSION = {"date": "2026-07-22", "type": "long", "dist_km": 20.0}
_TEMPO_SESSION = {"date": "2026-07-22", "type": "tempo", "dist_km": 8.0}


# ---- estimate_minutes -------------------------------------------------------

def test_estimate_minutes_from_dist_km_and_anchor_pace():
    assert fueling.estimate_minutes({"dist_km": 10.0}, anchor_pace=6.0) == 60


def test_estimate_minutes_falls_back_to_type_when_no_dist_or_steps():
    assert fueling.estimate_minutes({"type": "long"}) == fueling._TYPE_MINUTES["long"]


def test_estimate_minutes_none_when_nothing_to_go_on():
    assert fueling.estimate_minutes({"type": "unknown_type"}) is None


def test_estimate_minutes_from_steps_with_repeat():
    session = {"steps": [
        {"kind": "warmup", "dist_m": 1000, "pace_min_km": None},
        {"kind": "repeat", "reps": 4, "steps": [
            {"kind": "run", "dist_m": 1000, "pace_min_km": [5.0, 5.2]},
            {"kind": "recovery", "dur_s": 60, "pace_min_km": None},
        ]},
    ]}
    # warmup: 1km @ 6.5 default = 6.5min; 4x(1km@5.1min + 1min) = 4*6.1 = 24.4min
    minutes = fueling.estimate_minutes(session)
    assert 30 <= minutes <= 32


# ---- advise: acceptance criteria --------------------------------------------

def test_advise_none_for_short_easy_session():
    assert fueling.advise(_SHORT_EASY_SESSION, _LONG_HOT) is None


def test_advise_none_without_forecast():
    assert fueling.advise(_LONG_SESSION, None) is None


def test_advise_none_without_session():
    assert fueling.advise(None, _LONG_HOT) is None


def test_advise_long_in_heat_has_fluid_carbs_and_slot():
    result = fueling.advise(_LONG_SESSION, _LONG_HOT, heat_feels_c=30.0)
    assert result is not None
    assert result["hot"] is True
    assert result["fluid_ml_h"] == fueling.FLUID_ML_H_HOT
    assert result["carbs_g_h"] == fueling.CARB_G_H
    assert any("електроліти" in n for n in result["notes"])
    assert any("слот" in n for n in result["notes"])


def test_advise_long_in_cool_has_duration_notes_only():
    result = fueling.advise(_LONG_SESSION, _LONG_COOL, heat_feels_c=30.0)
    assert result is not None
    assert result["hot"] is False
    assert result["fluid_ml_h"] == fueling.FLUID_ML_H_MILD
    assert result["carbs_g_h"] == fueling.CARB_G_H
    assert not any("електроліти" in n or "слот" in n for n in result["notes"])


def test_advise_none_for_non_heavy_type():
    easy_but_long = {"date": "2026-07-22", "type": "easy", "dist_km": 20.0}
    assert fueling.advise(easy_but_long, _LONG_HOT) is None


def test_advise_short_tempo_below_duration_floor_is_none():
    short_tempo = {"date": "2026-07-22", "type": "tempo", "dist_km": 3.0}
    assert fueling.advise(short_tempo, _LONG_HOT, anchor_pace=5.0) is None


def test_advise_fluid_only_between_60_and_90_minutes():
    # ~65 min tempo: fluid guidance, but too short for carbs
    session = {"date": "2026-07-22", "type": "tempo", "dist_km": 10.0}
    result = fueling.advise(session, _LONG_COOL, anchor_pace=6.5)
    assert result is not None
    assert result["fluid_ml_h"] is not None
    assert result["carbs_g_h"] is None


# ---- run_analysis wiring -----------------------------------------------------

async def test_run_analysis_includes_fueling_for_todays_heavy_session(session, monkeypatch):
    from app.analysis import reports, service
    from app.garmin import repository
    from app.garmin.schemas import PlanWorkout

    U1 = 501
    today = dt.date.today().isoformat()
    await repository.create_plan(
        session, U1, goal="faster_5k", goal_label="Швидше 5 км", target_date=None,
        start_date=today, days_per_week=3, intensity="moderate", intake={}, summary="",
        workouts=[PlanWorkout(date=today, week=1, type="long", dist_km=20.0,
                              description="довгий")],
    )

    captured = {}

    def fake_analyze(payload, question="", deep=False, kind=None, previous_report=None,
                      api_key=None, weather=None, plan_today=None, fitness=None,
                      records=None, norm=None, subjective=None, health_alerts=None,
                      fueling=None):
        captured["fueling"] = fueling
        return "звіт", service.CallStats(kind=kind or "report", model="m")

    monkeypatch.setattr(reports, "analyze_with_stats", fake_analyze)
    payload = {"daily": [], "recent_activities": [], "planned_runs": [],
              "synced_today": True, "has_data": True}
    await service.run_analysis(
        session, payload, user_id=U1, question="q", weather=_LONG_HOT,
    )

    assert captured["fueling"] is not None
    assert captured["fueling"]["hot"] is True


async def test_run_analysis_omits_fueling_without_weather(session, monkeypatch):
    from app.analysis import reports, service
    from app.garmin import repository
    from app.garmin.schemas import PlanWorkout

    U1 = 502
    today = dt.date.today().isoformat()
    await repository.create_plan(
        session, U1, goal="faster_5k", goal_label="Швидше 5 км", target_date=None,
        start_date=today, days_per_week=3, intensity="moderate", intake={}, summary="",
        workouts=[PlanWorkout(date=today, week=1, type="long", dist_km=20.0,
                              description="довгий")],
    )

    captured = {}

    def fake_analyze(payload, question="", deep=False, kind=None, previous_report=None,
                      api_key=None, weather=None, plan_today=None, fitness=None,
                      records=None, norm=None, subjective=None, health_alerts=None,
                      fueling=None):
        captured["fueling"] = fueling
        return "звіт", service.CallStats(kind=kind or "report", model="m")

    monkeypatch.setattr(reports, "analyze_with_stats", fake_analyze)
    payload = {"daily": [], "recent_activities": [], "planned_runs": [],
              "synced_today": True, "has_data": True}
    await service.run_analysis(session, payload, user_id=U1, question="q")   # no weather

    assert captured["fueling"] is None
