"""EP-05 · Race pack — pre-race pacing/fueling/checklist synthesis.

Every ingredient already exists elsewhere: race-time predictions/VO2max/endurance in
``DailyMetric.extra`` (see ``app.goal``), the target date/distance implied by a plan's
``goal`` (``TrainingPlan.target_date`` is already a typed ISO string — see
``app.db.models.TrainingPlan``), weather via ``app.weather``, and the taper itself is
already baked into the generated plan's last sessions. What was missing (EP-05 "phase 0")
was a typed **target distance**: ``TrainingPlan.goal`` names a race ("first_10k") but
nothing mapped it to a km number a pacing calc can use — that's :data:`GOAL_DISTANCE_KM`
below, the sibling of ``app.goal.GOAL_METRIC`` (which maps a goal to the Garmin
*prediction* metric, not a fixed distance).

Pure Python, zero LLM, zero network (mirrors ``compare.py``/``wrapped.py``/``goal.py``'s
shape): this module only decides WHETHER a plan has a race pack to give and assembles the
narration context; all pacing/fueling numbers are Claude's job (``SYSTEM_RACE``, Opus) —
it forwards what Garmin/the plan already computed, never invents its own.
"""
import datetime as dt
from typing import Optional

from app import goal as goal_mod

# Auto-send the race pack exactly this many days before target_date (bot/jobs.py's daily
# plan_sync_job checks this once a day — the guard is per-plan, not per-date, so a missed
# tick doesn't lose the trigger, but it also never fires twice for the same plan).
TRIGGER_DAYS = 7

# Only fold a forecast into the pack when the race is this close — Open-Meteo's daily
# forecast is unreliable much further out (same reasoning as EP-13's decision window).
WEATHER_WINDOW_DAYS = 7

# /plan shows the last generated pack as a standing block while the race is this close.
PLAN_BLOCK_DAYS = 14

# plan.goal -> target race distance, km. Deliberately separate from goal.GOAL_METRIC
# (which maps to the *prediction metric* Garmin tracks, not a fixed distance) — the
# open-ended "general" goal has neither a distance nor a race date, so it has no pack.
GOAL_DISTANCE_KM = {
    "first_5k": 5.0,
    "faster_5k": 5.0,
    "first_10k": 10.0,
    "first_half": 21.0975,
}


def distance_for_goal(goal: Optional[str]) -> Optional[float]:
    """This goal's race distance in km, or None (open-ended/unrecognised goals)."""
    return GOAL_DISTANCE_KM.get(goal or "")


def has_target(plan) -> bool:
    """True when a plan carries both a race date and a distance we can pace — the two
    things a race pack needs. An open-ended (``general``) plan, or no plan at all, has
    neither."""
    return bool(plan and plan.target_date and distance_for_goal(plan.goal))


def days_to_target(target_date: Optional[str], today: Optional[dt.date] = None) -> Optional[int]:
    """Whole days from ``today`` to ``target_date`` (may be negative for a past date), or
    None when ``target_date`` is missing/unparsable."""
    if not target_date:
        return None
    try:
        return (dt.date.fromisoformat(target_date) - (today or dt.date.today())).days
    except ValueError:
        return None


def build_context(plan, fitness: Optional[dict], recent_sessions: list,
                   forecast_day: Optional[dict]) -> dict:
    """Assemble the narration context for :func:`app.analysis.reports.run_race_plan`.
    ``recent_sessions`` are the plan's own upcoming sessions through race day (its taper —
    the model is told to reference them, not invent a different one); ``forecast_day`` is
    the target date's forecast row (only present within :data:`WEATHER_WINDOW_DAYS`)."""
    metric_key, _label, _higher_better = goal_mod.metric_for_goal(plan.goal)
    return {
        "goal": plan.goal,
        "goal_label": plan.goal_label,
        "target_date": plan.target_date,
        "target_dist_km": distance_for_goal(plan.goal),
        "target_metric": metric_key,
        "days_left": days_to_target(plan.target_date),
        "fitness": fitness,
        "recent_sessions": recent_sessions,
        "weather": forecast_day,
    }
