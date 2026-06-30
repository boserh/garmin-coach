"""Convert a stored ``PlannedWorkout`` into a Garmin-Connect workout JSON.

This is the WRITE counterpart to ``client.fetch_workout_detail`` (which reads a
Runna/Garmin workout). Decoded from a real Runna workout, Garmin's step model is:

* ``workoutSegments[0].workoutSteps`` — a list of ``ExecutableStepDTO`` and, for
  intervals, ``RepeatGroupDTO`` (its children nested under ``workoutSteps``).
* ``stepType.stepTypeId``: warmup=1, cooldown=2, interval=3, recovery=4, rest=5,
  repeat=6.
* ``endCondition``: distance=3 (``endConditionValue`` in **metres**), time=2
  (seconds), iterations=7 (a repeat group), lap.button=1 (press-lap, no fixed end).
* ``targetType``: no.target=1; pace.zone=6 with ``targetValueOne``/``Two`` as
  **speed in m/s** — One is the faster bound (higher m/s), Two the slower.

Our ``PlanStep.pace_min_km`` is ``[fast, slow]`` in decimal min/km, so the
conversion is ``speed = 1000 / (min_km * 60)`` (verified: 6:40/km → 2.5 m/s).
The module is pure (no DB, no network) so it unit-tests trivially; the push
orchestration lives in ``app.cli``.
"""
from typing import List, Optional

_RUN_SPORT = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}

# our PlanStep.kind → (stepTypeId, stepTypeKey)
_STEP_TYPE = {
    "warmup": (1, "warmup"),
    "cooldown": (2, "cooldown"),
    "run": (3, "interval"),
    "interval": (3, "interval"),
    "recovery": (4, "recovery"),
    "rest": (5, "rest"),
}
_DEFAULT_STEP = (3, "interval")

_COND_DISTANCE = {"conditionTypeId": 3, "conditionTypeKey": "distance"}
_COND_TIME = {"conditionTypeId": 2, "conditionTypeKey": "time"}
_COND_LAP = {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}
_COND_ITER = {"conditionTypeId": 7, "conditionTypeKey": "iterations"}
_KM_UNIT = {"unitId": 2, "unitKey": "kilometer", "factor": 100000.0}

_TARGET_NONE = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}
_TARGET_PACE = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"}


def _speed(pace_min_km: float) -> float:
    """min/km (decimal) → m/s — Garmin's pace.zone target unit."""
    return round(1000.0 / (pace_min_km * 60.0), 7)


def _exec_step(step: dict, order: int) -> dict:
    type_id, type_key = _STEP_TYPE.get(step.get("kind"), _DEFAULT_STEP)
    out: dict = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": type_id, "stepTypeKey": type_key},
        "description": step.get("note"),
    }
    dist, dur = step.get("dist_m"), step.get("dur_s")
    if dist:
        out["endCondition"] = dict(_COND_DISTANCE)
        out["endConditionValue"] = float(dist)
        out["preferredEndConditionUnit"] = dict(_KM_UNIT)
    elif dur:
        out["endCondition"] = dict(_COND_TIME)
        out["endConditionValue"] = float(dur)
    else:
        out["endCondition"] = dict(_COND_LAP)  # press lap to advance

    pace = step.get("pace_min_km")
    if pace and len(pace) == 2 and all(pace):
        fast, slow = pace
        out["targetType"] = dict(_TARGET_PACE)
        out["targetValueOne"] = _speed(fast)   # faster bound (higher m/s)
        out["targetValueTwo"] = _speed(slow)   # slower bound (lower m/s)
    else:
        out["targetType"] = dict(_TARGET_NONE)
    return out


def _build_steps(steps: List[dict]) -> List[dict]:
    """Convert our flat/nested PlanStep dicts to Garmin steps, numbering ``stepOrder``
    continuously across the tree (a repeat group is numbered, then its children)."""
    counter = [0]

    def nxt() -> int:
        counter[0] += 1
        return counter[0]

    def conv(step: dict) -> dict:
        if step.get("kind") == "repeat":
            order = nxt()                                  # the group's own order
            children = [conv(c) for c in (step.get("steps") or [])]
            reps = int(step.get("reps") or 1)
            return {
                "type": "RepeatGroupDTO",
                "stepOrder": order,
                "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
                "numberOfIterations": reps,
                "smartRepeat": False,
                "endCondition": dict(_COND_ITER),
                "endConditionValue": float(reps),
                "workoutSteps": children,
            }
        return _exec_step(step, nxt())

    return [conv(s) for s in steps]


# A leading per-type emoji so the session type reads at a glance in Garmin's list (and
# so our workouts are visibly not Runna's). All single-codepoint (no variation selector
# / ZWJ) so they render on the watch. Unknown types fall back to the runner.
_TYPE_MARK = {
    "easy": "🌿",
    "recovery": "💧",
    "long": "🗻",
    "tempo": "🔥",
    "intervals": "⚡",
    "race": "🏁",
    "rest": "😴",
    "cross": "🚴",
}


def workout_name(w) -> str:
    """A short, emoji-marked name: ``🔥 Tempo 8km · W2`` / ``🌿 Easy 3.5km · W1``."""
    mark = _TYPE_MARK.get((w.type or "").lower(), "🏃")
    name = f"{mark} {(w.type or 'Run').capitalize()}"
    if w.dist_km:
        name += f" {w.dist_km:g}km"
    if w.week:
        name += f" · W{w.week}"
    return name[:80]   # Garmin caps the workout name length


def build_workout(w) -> dict:
    """Build the Garmin create-workout payload from a ``PlannedWorkout``.

    Uses the structured ``steps`` when present; otherwise falls back to a single
    distance step of ``dist_km`` with no pace target (a plain easy run)."""
    steps: Optional[List[dict]] = w.steps
    if not steps:
        dist_m = (w.dist_km or 0) * 1000
        steps = [{"kind": "run", "dist_m": dist_m}] if dist_m else [{"kind": "run"}]
    return {
        "workoutName": workout_name(w),
        "description": w.description or None,
        "sportType": dict(_RUN_SPORT),
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": dict(_RUN_SPORT),
            "workoutSteps": _build_steps(steps),
        }],
    }
