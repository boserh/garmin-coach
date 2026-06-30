"""PlannedWorkout → Garmin workout JSON conversion (pure, no DB/network)."""
from types import SimpleNamespace

from app.garmin import workout_export as wx


def _w(**kw):
    base = dict(week=1, date="2026-07-02", type="easy", dist_km=5.0,
                description="легкий біг", steps=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_speed_conversion_matches_garmin():
    # 6:40/km == 6.6667 min/km → 2.5 m/s (verified against a real Runna workout)
    assert wx._speed(6 + 40 / 60) == 2.5                   # 6:40/km
    assert round(wx._speed(7 + 10 / 60), 5) == 2.32558     # 7:10/km


def test_name_marker_by_type():
    assert wx.workout_name(_w(week=3, type="intervals", dist_km=6.0)) == "⚡ Intervals 6km · W3"
    assert wx.workout_name(_w(week=1, type="easy", dist_km=3.5)) == "· Easy 3.5km · W1"
    assert wx.workout_name(_w(week=2, type="tempo", dist_km=8.0)) == "▲ Tempo 8km · W2"


def test_fallback_single_distance_step_when_no_steps():
    payload = wx.build_workout(_w(dist_km=5.0, steps=None))
    steps = payload["workoutSegments"][0]["workoutSteps"]
    assert payload["sportType"]["sportTypeKey"] == "running"
    assert len(steps) == 1
    s = steps[0]
    assert s["endCondition"]["conditionTypeKey"] == "distance"
    assert s["endConditionValue"] == 5000.0
    assert s["targetType"]["workoutTargetTypeKey"] == "no.target"


def test_pace_target_maps_fast_slow_to_speed():
    w = _w(steps=[{"kind": "run", "dist_m": 1500, "pace_min_km": [6 + 40 / 60, 7 + 10 / 60]}])
    step = wx.build_workout(w)["workoutSegments"][0]["workoutSteps"][0]
    assert step["stepType"]["stepTypeKey"] == "interval"
    assert step["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert step["targetValueOne"] == 2.5                   # fast bound (higher m/s)
    assert round(step["targetValueTwo"], 5) == 2.32558     # slow bound (lower m/s)


def test_repeat_group_and_continuous_step_order():
    w = _w(type="intervals", steps=[
        {"kind": "warmup", "dist_m": 1500},
        {"kind": "repeat", "reps": 5, "steps": [
            {"kind": "run", "dur_s": 180, "pace_min_km": [5.25, 5.4]},
            {"kind": "recovery", "dur_s": 120},
        ]},
        {"kind": "cooldown", "dist_m": 1500},
    ])
    steps = wx.build_workout(w)["workoutSegments"][0]["workoutSteps"]
    assert [s["type"] for s in steps] == [
        "ExecutableStepDTO", "RepeatGroupDTO", "ExecutableStepDTO"]
    warmup, rep, cooldown = steps
    assert warmup["stepOrder"] == 1
    # the repeat group is numbered, then its children continue the sequence
    assert rep["stepOrder"] == 2
    assert rep["numberOfIterations"] == 5
    assert rep["endCondition"]["conditionTypeKey"] == "iterations"
    run, recov = rep["workoutSteps"]
    assert run["stepOrder"] == 3 and recov["stepOrder"] == 4
    assert run["endCondition"]["conditionTypeKey"] == "time"
    assert run["endConditionValue"] == 180.0
    assert cooldown["stepOrder"] == 5
    assert cooldown["stepType"]["stepTypeKey"] == "cooldown"
