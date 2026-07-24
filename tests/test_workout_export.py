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


def test_clone_workout_strips_ids_keeps_structure():
    raw = {"workoutId": 931013083, "ownerId": 5, "author": {"x": 1}, "workoutName": "Day 1",
           "sportType": {"sportTypeKey": "strength_training"},
           "workoutSegments": [{"segmentId": 9, "workoutSteps": [
               {"stepId": 1, "stepType": {"stepTypeKey": "warmup"}},
               {"stepId": 2, "childStepId": 1, "type": "RepeatGroupDTO",
                "workoutSteps": [{"stepId": 3, "childStepId": 1, "exerciseName": "SQUAT"}]}]}]}
    c = wx.clone_workout(raw, "🏋️ Day 1 · W2")
    assert c["workoutName"] == "🏋️ Day 1 · W2"
    assert "workoutId" not in c and "ownerId" not in c and "author" not in c
    assert c["sportType"]["sportTypeKey"] == "strength_training"     # structure kept
    seg = c["workoutSegments"][0]
    assert "segmentId" not in seg
    steps = seg["workoutSteps"]
    assert all("stepId" not in s and "childStepId" not in s for s in steps)
    nested = steps[1]["workoutSteps"][0]
    assert "stepId" not in nested and nested["exerciseName"] == "SQUAT"   # exercise preserved


def test_name_marker_by_type():
    assert wx.workout_name(_w(week=3, type="intervals", dist_km=6.0)) == "⚡ Intervals 6km · W3"
    assert wx.workout_name(_w(week=1, type="easy", dist_km=3.5)) == "🌿 Easy 3.5km · W1"
    assert wx.workout_name(_w(week=2, type="tempo", dist_km=8.0)) == "🔥 Tempo 8km · W2"


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


# ---------- EP-10 phase 3: cycling sessions ----------

def test_cycling_workout_uses_cycling_sport_type():
    payload = wx.build_workout(_w(type="cycling", dist_km=40.0, steps=None))
    assert payload["sportType"] == {"sportTypeId": 2, "sportTypeKey": "cycling",
                                    "displayOrder": 2}
    assert payload["workoutSegments"][0]["sportType"]["sportTypeKey"] == "cycling"


def test_cycling_fallback_step_has_no_pace_target():
    payload = wx.build_workout(_w(type="cycling", dist_km=40.0, steps=None))
    step = payload["workoutSegments"][0]["workoutSteps"][0]
    assert step["endCondition"]["conditionTypeKey"] == "distance"
    assert step["endConditionValue"] == 40000.0
    assert step["targetType"]["workoutTargetTypeKey"] == "no.target"


def test_cycling_step_with_hr_zone_target():
    w = _w(type="cycling", steps=[
        {"kind": "warmup", "dur_s": 600},
        {"kind": "ride", "dur_s": 3600, "hr_zone": 2, "note": "легка вело"},
    ])
    steps = wx.build_workout(w)["workoutSegments"][0]["workoutSteps"]
    assert steps[0]["stepType"]["stepTypeKey"] == "warmup"
    ride = steps[1]
    assert ride["stepType"]["stepTypeKey"] == "interval"     # unmapped kind → default
    assert ride["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert ride["zoneNumber"] == 2


def test_running_workout_still_uses_running_sport_type():
    payload = wx.build_workout(_w(type="easy", dist_km=5.0, steps=None))
    assert payload["sportType"]["sportTypeKey"] == "running"


def test_name_marker_cycling():
    assert wx.workout_name(_w(week=2, type="cycling", dist_km=40.0)) == "🚴 Cycling 40km · W2"


def test_hr_zone_target_for_easy_step():
    # easy/recovery running targets a heart-rate zone, not a pace range
    w = _w(type="easy", steps=[{"kind": "run", "dist_m": 4000, "hr_zone": 2}])
    step = wx.build_workout(w)["workoutSegments"][0]["workoutSteps"][0]
    assert step["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert step["zoneNumber"] == 2
    assert "targetValueOne" not in step


def test_hr_zone_takes_precedence_over_pace():
    # if both are (mistakenly) present, the effort zone wins — never a double target
    w = _w(steps=[{"kind": "run", "dist_m": 4000, "pace_min_km": [6.75, 7.0], "hr_zone": 2}])
    step = wx.build_workout(w)["workoutSegments"][0]["workoutSteps"][0]
    assert step["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert "targetValueOne" not in step


def test_out_of_range_hr_zone_falls_back_to_no_target():
    w = _w(steps=[{"kind": "run", "dist_m": 4000, "hr_zone": 9}])
    step = wx.build_workout(w)["workoutSegments"][0]["workoutSteps"][0]
    assert step["targetType"]["workoutTargetTypeKey"] == "no.target"


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


def test_build_strength_workout_structure():
    import app.garmin.workout_export as wx
    dto = wx.build_strength_workout("🏋️ Ноги", [
        {"reps": 3, "rest_s": 90, "exercises": [
            {"category": "squat", "exercise": "goblet_squat", "reps": 12, "weight_kg": 20},
            {"category": "LUNGE", "exercise": None, "reps": 10, "weight_kg": None}]},
        {"reps": 4, "rest_s": 60, "exercises": [
            {"category": "DEADLIFT", "exercise": "ROMANIAN_DEADLIFT", "reps": 8,
             "weight_kg": 40}]},
    ], warmup_s=300)
    assert dto["sportType"]["sportTypeId"] == 5  # strength_training
    steps = dto["workoutSegments"][0]["workoutSteps"]
    # warmup, group, lap-rest, group
    assert [s["type"] for s in steps] == [
        "ExecutableStepDTO", "RepeatGroupDTO", "ExecutableStepDTO", "RepeatGroupDTO"]
    assert steps[0]["stepType"]["stepTypeKey"] == "warmup"
    assert steps[2]["endCondition"]["conditionTypeKey"] == "lap.button"  # between groups
    g1 = steps[1]
    assert g1["numberOfIterations"] == 3
    squat, lunge, rest = g1["workoutSteps"]
    assert squat["category"] == "SQUAT" and squat["exerciseName"] == "GOBLET_SQUAT"
    assert squat["endCondition"]["conditionTypeKey"] == "reps"
    assert squat["endConditionValue"] == 12.0
    assert squat["weightValue"] == 20.0 and squat["weightUnit"]["unitKey"] == "kilogram"
    assert lunge["weightValue"] == -1.0  # bodyweight
    assert rest["stepType"]["stepTypeKey"] == "rest" and rest["endConditionValue"] == 90.0
    # continuous stepOrder across the whole tree
    orders = []
    def collect(sts):
        for s in sts:
            orders.append(s["stepOrder"])
            if s["type"] == "RepeatGroupDTO":
                collect(s["workoutSteps"])
    collect(steps)
    assert orders == list(range(1, len(orders) + 1))


def test_read_blocks_preserves_supersets_sets_and_rest():
    """read_blocks recovers the block/superset structure read_exercises flattens away."""
    raw = {"workoutSegments": [{"workoutSteps": [
        {"type": "RepeatGroupDTO", "stepType": {"stepTypeKey": "repeat"},
         "numberOfIterations": 3, "workoutSteps": [
            {"stepType": {"stepTypeKey": "interval"}, "category": "HIP_RAISE",
             "exerciseName": "HANGING_LEG_RAISE",
             "endCondition": {"conditionTypeKey": "reps"}, "endConditionValue": 15.0,
             "weightValue": -1.0},
            {"stepType": {"stepTypeKey": "interval"}, "category": "PLANK",
             "exerciseName": "STRAIGHT_ARM_PLANK",
             "endCondition": {"conditionTypeKey": "reps"}, "endConditionValue": 30.0,
             "weightValue": -1.0},
            {"stepType": {"stepTypeKey": "rest"}, "endConditionValue": 90.0},
         ]},
        {"stepType": {"stepTypeKey": "rest"}},   # lap-button separator between groups
        {"type": "RepeatGroupDTO", "stepType": {"stepTypeKey": "repeat"},
         "numberOfIterations": 4, "workoutSteps": [
            {"stepType": {"stepTypeKey": "interval"}, "category": "BACK_EXTENSION",
             "endCondition": {"conditionTypeKey": "reps"}, "endConditionValue": 15.0,
             "weightValue": 10.0},
         ]},
    ]}]}
    blocks = wx.read_blocks(raw)
    assert len(blocks) == 2
    assert blocks[0]["reps"] == 3 and blocks[0]["rest_s"] == 90
    assert [e["category"] for e in blocks[0]["exercises"]] == ["HIP_RAISE", "PLANK"]
    assert blocks[0]["exercises"][0]["weight_kg"] is None    # -1 = bodyweight
    assert blocks[1]["reps"] == 4 and blocks[1]["exercises"][0]["weight_kg"] == 10.0


def test_build_then_read_blocks_roundtrips():
    blocks = [
        {"reps": 3, "rest_s": 90, "exercises": [
            {"category": "SQUAT", "exercise": "GOBLET_SQUAT", "reps": 12, "weight_kg": 22.0},
            {"category": "LUNGE", "exercise": None, "reps": 10, "weight_kg": None},
        ]},
        {"reps": 4, "rest_s": None, "exercises": [
            {"category": "BACK_EXTENSION", "exercise": None, "reps": 15, "weight_kg": 10.0},
        ]},
    ]
    dto = wx.build_strength_workout("Day 1", blocks)
    assert wx.read_blocks(dto) == blocks
