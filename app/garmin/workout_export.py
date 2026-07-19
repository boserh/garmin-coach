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
import re
from typing import List, Optional

_RUN_SPORT = {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1}


def clean_workout_name(name) -> str:
    """Tidy a Garmin workout name: drop the trailing ' manual' marker Garmin tacks onto
    hand-entered workouts, so 'Day 1 manual' reads as 'Day 1'."""
    return re.sub(r"\s+manual$", "", (name or "").strip(), flags=re.IGNORECASE).strip()

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
# HR-zone target: watch holds the user's zone HR bounds, we just name the zone (1-5).
# NB: unlike pace.zone above, this DTO shape is NOT yet verified field-for-field against
# a real saved HR-zone workout — verify before trusting a live push (used by easy/long/
# recovery steps that carry hr_zone; their pace hint, if any, rides in `note` → `description`
# below instead of a pace.zone target).
_TARGET_HR_ZONE = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"}


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
    zone = step.get("hr_zone")
    if isinstance(zone, int) and 1 <= zone <= 5:
        # effort target (easy/recovery) — the watch supplies the zone's HR bounds
        out["targetType"] = dict(_TARGET_HR_ZONE)
        out["zoneNumber"] = zone
    elif pace and len(pace) == 2 and all(pace):
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


def clone_workout(raw: dict, name: str) -> dict:
    """Turn a saved Garmin workout DTO into a create-payload for our own copy: strip the
    server-assigned ids and ownership, keep the structure (steps/exercises), set ``name``.
    Used to schedule a strength template (Day 1/Day 2) without touching the original."""
    import copy
    tpl = copy.deepcopy(raw)
    for k in ("workoutId", "ownerId", "updatedDate", "createdDate", "author",
              "trainingPlanId", "consumer", "consumerName", "consumerImageURL",
              "consumerWebsiteURL", "workoutProvider", "workoutSourceId",
              "atpPlanId", "shared", "sharedWithUsers"):
        tpl.pop(k, None)
    tpl["workoutName"] = name[:80]
    for seg in tpl.get("workoutSegments", []) or []:
        seg.pop("segmentId", None)
        for st in seg.get("workoutSteps", []) or []:
            _strip_step_ids(st)
    return tpl


def _strip_step_ids(step: dict) -> None:
    step.pop("stepId", None)
    step.pop("childStepId", None)
    for sub in step.get("workoutSteps", []) or []:   # repeat groups nest steps
        _strip_step_ids(sub)


def apply_exercise_edits(payload: dict, edits: list) -> int:
    """Mutate a cloned strength workout in place: for each edit swap the first exercise
    step whose ``category`` matches ``from`` to ``to`` (+ optional ``exercise`` variant and
    ``reps``). One edit consumes one step; unmatched edits are skipped. Returns the number
    of steps changed. Weight is intentionally left untouched (unit unverified)."""
    pending = [e for e in (edits or []) if e.get("from") and e.get("to")]
    if not pending:
        return 0
    used = [False] * len(pending)
    changed = 0

    def walk(steps: list) -> None:
        nonlocal changed
        for st in steps or []:
            walk(st.get("workoutSteps"))  # repeat groups nest their steps
            cat = (st.get("category") or "").upper()
            if not cat:
                continue
            for i, e in enumerate(pending):
                if used[i] or cat != (e["from"] or "").upper():
                    continue
                st["category"] = (e["to"] or "").upper()
                ex = (e.get("exercise") or "").upper()
                st["exerciseName"] = ex or None
                reps = e.get("reps")
                end = st.get("endCondition") or {}
                if reps and end.get("conditionTypeKey") == "reps":
                    st["endConditionValue"] = float(reps)
                used[i] = True
                changed += 1
                break

    for seg in payload.get("workoutSegments", []) or []:
        walk(seg.get("workoutSteps", []) or [])
    return changed


_STRENGTH_SPORT = {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 5}
_COND_REPS = {"conditionTypeId": 10, "conditionTypeKey": "reps"}
_KG_UNIT = {"unitId": 8, "unitKey": "kilogram", "factor": 1000.0}


def _strength_step(order: int, *, kind, cat=None, ex=None, reps=None, dur_s=None,
                   weight_kg=None) -> dict:
    """One strength ExecutableStepDTO, mirroring the real Garmin shape: exercises end on
    reps + carry category/exerciseName/weightValue (kg; -1.0 = bodyweight); rests end on
    time (or lap.button when no duration). No pace/target (no.target)."""
    type_id, type_key = _STEP_TYPE.get(kind, (3, "interval"))
    out: dict = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": type_id, "stepTypeKey": type_key},
        "description": None,
        "targetType": dict(_TARGET_NONE),
    }
    if reps:
        out["endCondition"] = dict(_COND_REPS)
        out["endConditionValue"] = float(reps)
    elif dur_s:
        out["endCondition"] = dict(_COND_TIME)
        out["endConditionValue"] = float(dur_s)
    else:
        out["endCondition"] = dict(_COND_LAP)
        out["endConditionValue"] = 0.0
    if cat:
        out["category"] = cat.upper()
        out["exerciseName"] = ex.upper() if ex else None
        out["weightValue"] = float(weight_kg) if weight_kg else -1.0
        out["weightUnit"] = dict(_KG_UNIT)
    return out


def build_strength_workout(name: str, blocks: list, *, warmup_s: int = 0) -> dict:
    """Build a from-scratch Garmin **strength** workout DTO (sportType 5). ``blocks`` is a
    list of ``{reps, rest_s, exercises: [{category, exercise, reps, weight_kg}]}`` — each
    becomes a RepeatGroupDTO (its exercises + a trailing rest), with a lap-button rest
    between groups, mirroring Garmin's own strength layout. ``stepOrder`` is continuous
    across the tree. NB: unverified on-watch — validate with a test push before relying."""
    counter = [0]

    def nxt() -> int:
        counter[0] += 1
        return counter[0]

    steps: list = []
    if warmup_s:
        steps.append(_strength_step(nxt(), kind="warmup", dur_s=warmup_s))
    for bi, block in enumerate(blocks or []):
        if bi:  # a press-lap rest between groups, as Garmin lays them out
            steps.append(_strength_step(nxt(), kind="rest"))
        order = nxt()  # the group's own order, then its children
        children = []
        for ex in block.get("exercises") or []:
            children.append(_strength_step(
                nxt(), kind="interval", cat=ex.get("category"), ex=ex.get("exercise"),
                reps=ex.get("reps"), weight_kg=ex.get("weight_kg")))
        rest_s = block.get("rest_s")
        if rest_s:
            children.append(_strength_step(nxt(), kind="rest", dur_s=rest_s))
        iters = int(block.get("reps") or 1)
        steps.append({
            "type": "RepeatGroupDTO",
            "stepOrder": order,
            "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
            "numberOfIterations": iters,
            "smartRepeat": False,
            "endCondition": dict(_COND_ITER),
            "endConditionValue": float(iters),
            "workoutSteps": children,
        })
    return {
        "workoutName": name[:80],
        "sportType": dict(_STRENGTH_SPORT),
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": dict(_STRENGTH_SPORT),
            "workoutSteps": steps,
        }],
    }


def read_exercises(raw: dict) -> list:
    """Extract a strength workout's exercise list from its Garmin DTO, in order:
    ``[{category, exercise, reps}]`` (reps only when the step ends on reps). Used to show
    the LLM a template's contents so it can adapt them toward a requested focus."""
    out: list = []

    def walk(steps: list) -> None:
        for st in steps or []:
            walk(st.get("workoutSteps"))  # repeat groups nest their steps
            cat = (st.get("category") or "").upper()
            if not cat:
                continue
            end = st.get("endCondition") or {}
            reps = st.get("endConditionValue") if end.get("conditionTypeKey") == "reps" else None
            out.append({
                "category": cat,
                "exercise": (st.get("exerciseName") or None),
                "reps": int(reps) if reps else None,
            })

    for seg in raw.get("workoutSegments", []) or []:
        walk(seg.get("workoutSteps", []) or [])
    return out


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
