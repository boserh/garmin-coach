"""Reconcile the Garmin-Connect calendar with the user's active plan — a rolling
window like Runna's. Orchestration only: ``workout_export`` converts, ``client`` does
the POST/DELETE, ``repository`` reads/writes. The caller must have a user provider
bound (``user_runtime``); we log in defensively (``login`` is idempotent).

Two passes:

* **forward** — create + schedule the active plan's upcoming ``planned`` runs that fall
  in the next ``days`` and aren't pushed yet (idempotent: a stored ``garmin_workout_id``
  means skip).
* **cleanup** — remove from Garmin everything we pushed that no longer belongs: a past
  date, a non-``planned`` status (skipped), or a workout whose plan is no longer the
  active one (archived, or superseded by a regenerated plan). Only ever touches workouts
  we created (by stored id) — never the user's manual/Runna workouts.
"""
import datetime as dt
import logging

from fastapi.concurrency import run_in_threadpool

from app.garmin import client, repository, workout_export
from app.garmin.providers import get_provider

logger = logging.getLogger("garmin")

# rest/cross sessions aren't runs — don't push them to the watch.
_SKIP_TYPES = {"rest", "cross", "strength"}


def _runnable(w) -> bool:
    return (w.type or "").lower() not in _SKIP_TYPES


async def push_workout(session, w) -> int:
    """Create + schedule one workout, store its Garmin ids, commit. Returns the id."""
    payload = workout_export.build_workout(w)
    created = await run_in_threadpool(client.create_workout, payload)
    wid = created.get("workoutId")
    sched = await run_in_threadpool(client.schedule_workout, wid, w.date)
    w.garmin_workout_id = wid
    w.garmin_schedule_id = sched.get("workoutScheduleId")
    await session.commit()
    return wid


async def remove_workout(session, w) -> bool:
    """Delete one pushed workout from Garmin (also clears its schedule) and null the
    stored ids. Tolerates an already-deleted workout. Returns True if Garmin confirmed
    the delete, False if it was already gone."""
    wid = w.garmin_workout_id
    deleted = True
    try:
        await run_in_threadpool(client.delete_workout, wid)
    except Exception as e:
        deleted = False
        logger.info(f"GARMIN unpush: workout {wid} already gone ({type(e).__name__})")
    w.garmin_workout_id = None
    w.garmin_schedule_id = None
    await session.commit()
    return deleted


async def sync_plan_to_garmin(session, user_id: int, *, days: int = 14) -> dict:
    """Reconcile the calendar with the user's plan (cleanup + forward). Requires a bound
    user provider. Returns ``{"pushed": n, "removed": n}``."""
    await run_in_threadpool(get_provider().login)
    today = dt.date.today().isoformat()
    active = await repository.get_active_plan(session, user_id)
    active_id = active.id if active else None

    # cleanup: anything WE pushed that no longer belongs in the calendar.
    stale = [w for w in await repository.list_pushed_workouts(session, user_id)
             if w.plan_id != active_id or w.date < today or w.status != "planned"]
    removed = 0
    for w in stale:
        await remove_workout(session, w)
        removed += 1

    # forward: active plan's upcoming, in-window, unpushed, runnable sessions.
    pushed = 0
    if active_id is not None:
        end = (dt.date.today() + dt.timedelta(days=days)).isoformat()
        for w in await repository.list_workouts(session, active_id, upcoming_only=True):
            if w.date <= end and _runnable(w) and w.garmin_workout_id is None:
                await push_workout(session, w)
                pushed += 1

    if pushed or removed:
        logger.info(f"GARMIN sync user={user_id}: +{pushed} pushed, -{removed} removed")
    return {"pushed": pushed, "removed": removed}


async def resync_workouts(session, user_id: int, workouts, *, days: int = 14) -> dict:
    """Mirror an edit onto the calendar — only the touched sessions, not the whole plan.
    For each: drop its old Garmin copy (move changed the date, modify the content), then
    re-push if it's still an upcoming in-window run (skip/past/rest just get removed). The
    daily ``sync_plan_to_garmin`` is the full reconciler; this is the cheap per-edit path.
    Requires a bound user provider."""
    await run_in_threadpool(get_provider().login)
    today = dt.date.today().isoformat()
    end = (dt.date.today() + dt.timedelta(days=days)).isoformat()
    pushed = removed = 0
    for w in workouts:
        if w.garmin_workout_id is not None:
            await remove_workout(session, w)
            removed += 1
        if w.status == "planned" and today <= w.date <= end and _runnable(w):
            await push_workout(session, w)
            pushed += 1
    if pushed or removed:
        logger.info(f"GARMIN edit-sync user={user_id}: +{pushed} pushed, -{removed} removed")
    return {"pushed": pushed, "removed": removed}
