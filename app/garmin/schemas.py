"""Pydantic models for the compact Garmin payload.

Field names and types mirror the dicts the old ``garmin_client`` produced exactly,
so the data sent to Claude (and the dedup cache key) is unchanged. ``extra="allow"``
keeps the models forgiving of any additional keys. These are API/payload schemas
only — the persistence models live in ``app.db.models``.
"""
from typing import List, Optional

from pydantic import BaseModel, ConfigDict


class DailySummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: str
    sleep_score: Optional[int] = None
    sleep_h: Optional[float] = None
    deep_h: Optional[float] = None
    rem_h: Optional[float] = None
    light_h: Optional[float] = None
    awake_h: Optional[float] = None
    hrv_avg: Optional[int] = None
    hrv_status: Optional[str] = None
    stress_avg: Optional[int] = None
    stress_max: Optional[int] = None
    bb_charged: Optional[int] = None
    bb_drained: Optional[int] = None
    extra: Optional[dict] = None   # scalar metrics we fetch but don't model as columns
    has_data: bool = False


class Activity(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: str
    type: Optional[str] = None
    dur_min: Optional[float] = None
    dist_km: Optional[float] = None
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    load: Optional[float] = None
    # `exercises` ({"active_sets": int, "sets": {exercise_name: count}}) is added
    # only for strength activities. Left as an `extra` field (not declared) so it
    # appears in the dump only when present — matching the original dict exactly.


class PlannedRun(BaseModel):
    model_config = ConfigDict(extra="allow")

    date: str
    title: Optional[str] = None
    workout_id: Optional[int] = None
    # {"steps": [{"dist_m": float, "pace_min_km": [fast, slow] | None}]}
    detail: Optional[dict] = None


class Payload(BaseModel):
    model_config = ConfigDict(extra="allow")

    generated: str
    window_days: int
    synced_today: bool
    last_data_date: Optional[str] = None
    daily: List[DailySummary]
    recent_activities: List[Activity]
    planned_runs: List[PlannedRun]


class PlanStep(BaseModel):
    """One step of a structured workout — mirrors the Runna ``planned_runs[].detail.steps``
    shape (``dist_m`` + ``pace_min_km`` range) extended with warmup/cooldown/recovery and
    ``repeat`` blocks. Carries both the human detail and a future Garmin-Connect workout
    export. Pace is a ``[fast, slow]`` range in DECIMAL min/km (e.g. [5.83, 6.17] = 5:50–6:10)."""

    model_config = ConfigDict(extra="ignore")

    kind: str                                  # warmup / run / recovery / cooldown / repeat
    dist_m: Optional[float] = None             # distance-based step
    dur_s: Optional[int] = None                # time-based step (e.g. intervals)
    pace_min_km: Optional[List[float]] = None  # [fast, slow] target range
    reps: Optional[int] = None                 # kind=repeat: how many times
    steps: Optional[List["PlanStep"]] = None   # kind=repeat: the repeated sub-steps
    note: Optional[str] = None                 # optional short label (українською)


class PlanWorkout(BaseModel):
    """One dated session in a generated training plan (Claude's structured output)."""

    model_config = ConfigDict(extra="ignore")

    date: str                       # ISO YYYY-MM-DD
    week: Optional[int] = None
    type: str                       # easy / long / tempo / intervals / rest / cross
    dist_km: Optional[float] = None
    description: str
    steps: Optional[List[PlanStep]] = None   # structured breakdown (Garmin-exportable)


class GeneratedPlan(BaseModel):
    """The structured plan Claude returns: an approach summary + dated workouts."""

    model_config = ConfigDict(extra="ignore")

    summary: str
    workouts: List[PlanWorkout]


class StrengthExercise(BaseModel):
    """One exercise inside a generated strength block. ``category`` is a Garmin category
    code (validated against the catalog); ``exercise`` an optional variant; ``weight_kg``
    in kilograms (omit/None = bodyweight)."""

    model_config = ConfigDict(extra="ignore")

    category: str
    exercise: Optional[str] = None
    reps: Optional[int] = None
    weight_kg: Optional[float] = None


class StrengthBlock(BaseModel):
    """One circuit/superset: its exercises repeated ``reps`` (sets) times with ``rest_s``
    between rounds."""

    model_config = ConfigDict(extra="ignore")

    reps: int = 1                    # number of sets (repeat-group iterations)
    rest_s: Optional[int] = None     # rest between rounds (seconds)
    exercises: List[StrengthExercise] = []


class StrengthSession(BaseModel):
    """A from-scratch strength workout (no template to clone) — built into a native Garmin
    strength DTO by ``workout_export.build_strength_workout``."""

    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    warmup_s: Optional[int] = None
    blocks: List[StrengthBlock] = []


class PlanOp(BaseModel):
    """One edit operation on a plan (Claude's structured output for a free-text tweak).
    ``date`` targets an existing workout (or the new one for ``add``)."""

    model_config = ConfigDict(extra="ignore")

    action: str                     # add / move / modify / skip / swap_exercise
    date: str                       # target workout date (ISO); for `add`, the new date
    to_date: Optional[str] = None   # `move` destination
    week: Optional[int] = None
    type: Optional[str] = None
    dist_km: Optional[float] = None
    description: Optional[str] = None
    steps: Optional[List[PlanStep]] = None   # structured breakdown for add/modify
    garmin_template_id: Optional[int] = None  # add/modify a strength day → saved workout to clone
    # add/modify a strength day generated FROM SCRATCH (preferred over a template clone):
    strength: Optional[StrengthSession] = None
    # swap_exercise (strength day): replace exercise `from_category` with `to_category`
    # (Garmin category codes), optionally a specific `exercise` variant and new `reps`.
    from_category: Optional[str] = None
    to_category: Optional[str] = None
    exercise: Optional[str] = None
    reps: Optional[int] = None


class PlanEdit(BaseModel):
    """Proposed changes to the active plan: a human-readable summary + operations.

    ``operations`` is always the user's literal request. When that request is risky
    (a big jump in distance/intensity, breaks ~10%/week, etc.) ``risky`` is set and a
    safer counter-proposal is offered via ``alt_summary``/``alt_operations`` — the bot
    then shows a third button so the user can take the suggestion instead."""

    model_config = ConfigDict(extra="ignore")

    summary: str
    operations: List[PlanOp]
    risky: bool = False
    alt_summary: Optional[str] = None
    alt_operations: Optional[List[PlanOp]] = None


PlanStep.model_rebuild()  # resolve the self-referential `steps` forward ref
