"""Plan/actual matching: link completed running activities to planned workout sessions.

Runs after every Garmin sync (``bot.jobs._tick_for_user``) and is idempotent —
it only touches workouts whose status is still ``planned``. Manual ``skipped``
statuses are never overwritten.

Matching rules
--------------
* Only run-type workouts (easy / long / tempo / intervals / race) are matched —
  rest, cross, and strength sessions are ignored.
* Candidate activities: running ``ActivityRecord`` rows within ±1 day of the
  planned date that are not yet linked to another session (``completed_activity_id``
  already set on a different workout).
* Scoring: exact-date match is preferred; ties broken by closest distance.
* Outcome:
  - |Δdist| ≤ DIST_PARTIAL_THRESH of the planned km → ``done``
  - |Δdist| > DIST_PARTIAL_THRESH → ``partial`` (completed but off-plan)
  - No match and date < today → ``missed`` (only today's workout may still sync)
"""
import datetime as dt
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActivityRecord, PlannedWorkout, WorkoutStatus
from app.garmin import repository

logger = logging.getLogger("matching")

# |actual − planned| / planned > this threshold → ``partial`` instead of ``done``.
DIST_PARTIAL_THRESH = 0.25

# Workout types we try to match against running activities.
_PLAN_RUN_TYPES = {"easy", "long", "tempo", "intervals", "race"}

# Activity type substring that identifies a running activity.
_ACT_RUN_SUBSTR = "run"


def _extract_target_pace(w: PlannedWorkout) -> Optional[float]:
    """Return the midpoint target pace (min/km) from the workout's structured steps,
    or None when not available. Looks for the first run/tempo step with a pace range."""
    for s in (w.steps or []):
        if not isinstance(s, dict):
            continue
        if s.get("kind") in ("run", "tempo"):
            p = s.get("pace_min_km")
            if isinstance(p, (list, tuple)) and len(p) == 2:
                try:
                    return (float(p[0]) + float(p[1])) / 2
                except (TypeError, ValueError):
                    pass
        # recurse into repeat groups
        for inner in s.get("steps") or []:
            if not isinstance(inner, dict):
                continue
            if inner.get("kind") in ("run", "tempo"):
                p = inner.get("pace_min_km")
                if isinstance(p, (list, tuple)) and len(p) == 2:
                    try:
                        return (float(p[0]) + float(p[1])) / 2
                    except (TypeError, ValueError):
                        pass
    return None


async def _get_unlinked_running_activities(
    session: AsyncSession, user_id: int, date_from: str, date_to: str
) -> list:
    """Running activities for this user in [date_from, date_to] that haven't been
    linked to a planned workout yet."""
    rows = (
        await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.user_id == user_id,
                ActivityRecord.type.contains(_ACT_RUN_SUBSTR),
                ActivityRecord.date.is_not(None),
                ActivityRecord.date >= date_from,
                ActivityRecord.date <= date_to,
            )
        )
    ).scalars().all()

    # Collect activity ids already claimed by another planned workout (so two workouts
    # on the same day don't both grab the same activity).
    claimed = (
        await session.execute(
            select(PlannedWorkout.completed_activity_id).where(
                PlannedWorkout.user_id == user_id,
                PlannedWorkout.completed_activity_id.is_not(None),
            )
        )
    ).scalars().all()
    claimed_ids = set(claimed)

    return [a for a in rows if a.id not in claimed_ids]


async def match_activities(session: AsyncSession, user_id: int) -> dict:
    """Match completed running activities to planned workouts for this user.

    Idempotent: only workouts with ``status == 'planned'`` are touched; already-matched
    or manually skipped workouts are left unchanged.

    Returns ``{"done": n, "partial": n, "missed": n}`` for logging.
    """
    plan = await repository.get_active_plan(session, user_id)
    if plan is None:
        return {"done": 0, "partial": 0, "missed": 0}

    today = dt.date.today()
    today_s = today.isoformat()

    # Only run-type workouts that are still in planned state and have passed (≤ today).
    workouts = [
        w for w in await repository.list_workouts(session, plan.id)
        if w.status == WorkoutStatus.PLANNED
        and (w.type or "").lower() in _PLAN_RUN_TYPES
        and w.date <= today_s
        and w.date >= (plan.start_date or "")
    ]
    if not workouts:
        return {"done": 0, "partial": 0, "missed": 0}

    min_date = min(w.date for w in workouts)
    max_date = max(w.date for w in workouts)
    range_from = (dt.date.fromisoformat(min_date) - dt.timedelta(days=1)).isoformat()
    range_to = (dt.date.fromisoformat(max_date) + dt.timedelta(days=1)).isoformat()

    activities = await _get_unlinked_running_activities(session, user_id, range_from, range_to)

    used_ids: set = set()
    done = partial = missed = 0

    for w in sorted(workouts, key=lambda x: x.date):
        w_date = dt.date.fromisoformat(w.date)

        candidates = [
            a for a in activities
            if a.id not in used_ids
            and a.date is not None
            and abs((dt.date.fromisoformat(a.date) - w_date).days) <= 1
        ]

        if not candidates:
            # Activity hasn't synced yet; only today's workout may still sync.
            if w.date < today_s:
                w.status = WorkoutStatus.MISSED
                missed += 1
                logger.debug(f"MATCH missed plan_id={plan.id} workout={w.id} date={w.date}")
            continue

        def _score(a: ActivityRecord):
            date_diff = abs((dt.date.fromisoformat(a.date) - w_date).days)
            dist_diff = abs((a.dist_km or 0.0) - (w.dist_km or 0.0))
            return (date_diff, dist_diff)

        best = min(candidates, key=_score)
        used_ids.add(best.id)

        plan_dist = w.dist_km or 0.0
        actual_dist = best.dist_km or 0.0
        if plan_dist > 0:
            delta_pct = abs(actual_dist - plan_dist) / plan_dist
        else:
            delta_pct = 0.0

        # Actual pace in min/km (None when duration or distance is missing).
        actual_pace: Optional[float] = None
        if best.dur_min and best.dist_km and best.dist_km > 0:
            actual_pace = best.dur_min / best.dist_km

        w.completed_activity_id = best.id
        w.match_info = {
            "dist_delta_km": round(actual_dist - plan_dist, 2),
            "actual_dist_km": best.dist_km,
            "activity_date": best.date,
            "actual_pace_minkm": round(actual_pace, 2) if actual_pace is not None else None,
            "plan_pace_minkm": _extract_target_pace(w),
        }

        if delta_pct > DIST_PARTIAL_THRESH:
            w.status = WorkoutStatus.PARTIAL
            partial += 1
        else:
            w.status = WorkoutStatus.DONE
            done += 1

        logger.info(
            f"MATCH {w.status} plan_id={plan.id} workout={w.id} date={w.date} "
            f"Δdist={w.match_info['dist_delta_km']:+.2f}km activity={best.id}"
        )

    await session.commit()
    return {"done": done, "partial": partial, "missed": missed}
