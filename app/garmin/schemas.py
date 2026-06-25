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


class PlanWorkout(BaseModel):
    """One dated session in a generated training plan (Claude's structured output)."""

    model_config = ConfigDict(extra="ignore")

    date: str                       # ISO YYYY-MM-DD
    week: Optional[int] = None
    type: str                       # easy / long / tempo / intervals / rest / cross
    dist_km: Optional[float] = None
    description: str


class GeneratedPlan(BaseModel):
    """The structured plan Claude returns: an approach summary + dated workouts."""

    model_config = ConfigDict(extra="ignore")

    summary: str
    workouts: List[PlanWorkout]
