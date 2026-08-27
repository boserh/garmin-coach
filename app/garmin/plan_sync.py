"""Reconcile the Garmin-Connect calendar with the user's active plan — a rolling
window like Runna's. Orchestration only: ``workout_export`` converts, ``client`` does
the POST/DELETE, ``repository`` reads/writes. The caller must have a user provider
bound (``user_runtime``); we log in defensively (``login`` is idempotent).

Two passes:

* **forward** — create + schedule the active plan's upcoming ``planned`` runs that fall
  in the next ``days`` and aren't pushed yet (idempotent: BOTH ids stored means skip; only
  ``garmin_workout_id`` means the push was interrupted and resumes at the schedule step).
* **cleanup** — remove from Garmin everything we pushed that no longer belongs: a past
  date, a non-``planned`` status (skipped), or a workout whose plan is no longer the
  active one (archived, or superseded by a regenerated plan). Only ever touches workouts
  we created (by stored id) — never the user's manual/Runna workouts.
"""
import datetime as dt
import logging
from typing import Optional

from fastapi.concurrency import run_in_threadpool

from app import plankind
from app.garmin import client, repository, workout_export
from app.garmin.providers import get_provider

logger = logging.getLogger("garmin")

# The rolling push window every caller uses (all default ``days=`` to this) — a named
# constant so /plan's badge logic (OPS-09: "in window but not pushed yet" vs "out of
# window, nothing expected") can share the exact same cutoff instead of a second literal.
PLAN_SYNC_WINDOW_DAYS = 14

# rest/cross sessions carry no structure — don't push them to the watch. "strength" is
# excluded here too: it only becomes pushable via garmin_template_id/strength_plan below,
# never by falling into workout_export.build_workout's run/cycling branch.
_SKIP_TYPES = {"rest", "cross", "strength"}


def _runnable(w) -> bool:
    """A type that builds from ``steps`` via ``workout_export.build_workout`` — a run OR
    (EP-10 phase 3) a cycling session; the sport itself is picked inside ``build_workout``."""
    return (w.type or "").lower() not in _SKIP_TYPES


def _calendar_stale(w, active_id, today: str) -> bool:
    """True when a pushed workout should be removed from the Garmin calendar.

    Keep rules (NOT stale):
    * Same active plan, future-or-today date, status=planned → keep (upcoming).
    * Same active plan, date==today, status done/partial → keep until tomorrow so the
      watch history still shows the completed session during the day.
    Anything else (wrong plan, past, skipped, missed) is stale and gets cleaned up."""
    if w.plan_id != active_id:
        return True
    if w.date < today:
        return True
    # date >= today from here
    if w.status == "planned":
        return False
    if w.status in ("done", "partial") and w.date == today:
        return False  # just completed today — leave on calendar
    return True


def _pushable(w) -> bool:
    """A session we send to the watch: a run/ride built from ``steps``, or a strength session
    that has something to build from (a template to clone, or a generated ``strength_plan``).

    The **type** decides which — never the mere presence of a strength column. This used to
    read "runnable OR has a template", so a leftover ``garmin_template_id`` on a day the
    athlete had turned into an easy run made that run push as a cloned strength workout (and
    made a ``rest`` note pushable at all). ``app.plankind`` is the same rule on the write
    side; this is it on the way out."""
    if plankind.is_strength(w.type):
        return bool(w.strength_plan or w.garmin_template_id)
    return _runnable(w)


def fully_pushed(w) -> bool:
    """Both halves of a push landed: the workout exists on Garmin AND it sits on a calendar
    date. A row with a workout id but no schedule id is HALF pushed — ``push_workout`` was
    interrupted between the two Garmin calls — and must be picked up again (it resumes at
    the schedule step rather than creating a second copy). Before this, "pushed" meant
    ``garmin_workout_id is not None``, so a half push was never finished: the session
    silently never reached the watch."""
    return w.garmin_workout_id is not None and w.garmin_schedule_id is not None


async def select_forward(session, plan_id: int, *, days: int = 14, only_date: str = None):
    """The forward-pass selection: a plan's upcoming, pushable, not-yet-pushed sessions
    within the next ``days`` (or exactly ``only_date`` if given). The single source for
    "what counts as pushable and in-window", shared by the daily ``sync_plan_to_garmin``
    and the manual CLI ``push-plan`` so the run/strength/skip rules never drift apart."""
    upcoming = await repository.list_workouts(session, plan_id, upcoming_only=True)
    if only_date:
        in_window = lambda w: w.date == only_date  # noqa: E731
    else:
        end = (dt.date.today() + dt.timedelta(days=days)).isoformat()
        in_window = lambda w: w.date <= end  # noqa: E731
    return [w for w in upcoming
            if in_window(w) and _pushable(w) and not fully_pushed(w)]


async def select_pushed(session, plan_id: int, *, only_date: str = None):
    """The remove-pass selection: workouts of one plan that WE pushed (have a stored
    ``garmin_workout_id``), optionally narrowed to a single ``only_date``. Shared by the
    CLI ``unpush-plan`` so the "only touch what we created" rule lives in one place."""
    return [w for w in await repository.list_workouts(session, plan_id)
            if w.garmin_workout_id is not None
            and (only_date is None or w.date == only_date)]


async def push_workout(session, w, errors: Optional[list] = None):
    """Create + schedule one workout, store its Garmin ids, commit. Returns the id (or
    None if a strength session had nothing to build from).

    ``w.type`` picks the branch, not the columns: a ``strength`` session builds from its
    ``strength_plan``, or clones the saved workout ``garmin_template_id`` names, into our own
    copy; everything else builds from ``steps``. See ``app.plankind`` for why the type has to
    be what decides.

    ``errors`` (OPS-09): when given, every failure appends ``{"workout_id", "step":
    "push", "msg"}`` to it and returns None instead of raising — the daily
    ``sync_plan_to_garmin`` passes a list so one session's push failure doesn't abort the
    rest of the batch. Callers that don't pass it (CLI ``push-plan``) keep the old
    behaviour: an unexpected exception still propagates.

    **Two commits, not one.** The workout id is persisted as soon as Garmin returns it,
    BEFORE the session is scheduled. Committing only at the end left a window in which the
    workout existed on Garmin while no row knew its id: a crash/restart/timeout there
    orphaned it forever — the next run pushed a second copy (a duplicate on the calendar),
    and ``unpush-plan``, which only ever touches stored ids, could never clean the first
    one up. Seen live 2026-08-27: two identical strength sessions on one date, the older
    one untracked. A row left half pushed (id, no schedule id) is picked up by
    ``select_forward`` again and RESUMES at the schedule step."""
    resume = w.garmin_workout_id is not None and w.garmin_schedule_id is None
    payload = None
    if not resume and not plankind.is_strength(w.type) and plankind.foreign_columns(w):
        # Belt and braces behind app.plankind: the type is the athlete's intent, so a row
        # that still carries strength content goes to the watch as the run it says it is.
        logger.warning(f"GARMIN push: {w.date} is a {w.type} session but carries "
                       f"{', '.join(plankind.foreign_columns(w))} — pushing it as a run")
    if resume:
        pass   # nothing to build: the workout is already on Garmin, only the date is missing
    elif plankind.is_strength(w.type) and w.strength_plan:
        sp = w.strength_plan
        # EP-03: the week suffix must survive even when the session carries its own
        # ``name`` (a progression's every week does) — otherwise every week of a
        # progression pushes under the identical workout name on the watch.
        base = sp.get("name") or w.description or "Силова"
        name = f"{workout_export.STRENGTH_MARK} {base}" + (f" · W{w.week}" if w.week else "")
        payload = workout_export.build_strength_workout(
            name, sp.get("blocks") or [], warmup_s=sp.get("warmup_s") or 0)
    elif plankind.is_strength(w.type) and w.garmin_template_id:
        raw = await run_in_threadpool(client.fetch_workout_full, w.garmin_template_id)
        if not raw:
            msg = f"template {w.garmin_template_id} unavailable"
            logger.warning(f"GARMIN push: {msg} — skip")
            if errors is not None:
                errors.append({"workout_id": w.id, "step": "push", "msg": msg})
            return None
        name = (f"{workout_export.STRENGTH_MARK} {w.description or 'Силова'}"
                + (f" · W{w.week}" if w.week else ""))
        payload = workout_export.clone_workout(raw, name)
        if w.exercise_edits:
            n = workout_export.apply_exercise_edits(payload, w.exercise_edits)
            logger.info(f"GARMIN push: applied {n} exercise edit(s) to {w.date}")
    elif plankind.is_strength(w.type):
        # Nothing to build it from. ``_pushable`` already filters these out; reaching here
        # means a caller skipped that check, and the run fallback below would put a bare
        # lap-button *run* on the watch under a strength session's name.
        msg = "strength session with neither a template nor a strength_plan"
        logger.warning(f"GARMIN push: {w.date} {msg} — skip")
        if errors is not None:
            errors.append({"workout_id": w.id, "step": "push", "msg": msg})
        return None
    else:
        payload = workout_export.build_workout(w)
    try:
        if resume:
            wid = w.garmin_workout_id
            logger.info(f"GARMIN push: resuming {w.date} — workout {wid} already created, "
                        f"scheduling only")
        else:
            created = await run_in_threadpool(client.create_workout, payload)
            wid = created.get("workoutId") if isinstance(created, dict) else None
            if not wid:
                msg = f"create_workout returned no workoutId: {created!r}"
                logger.error(f"GARMIN push FAILED workout={w.id} date={w.date}: {msg}")
                if errors is not None:
                    errors.append({"workout_id": w.id, "step": "push", "msg": msg})
                return None
            # Own it before scheduling — see the docstring. From here on the workout is
            # ours to delete even if everything below fails.
            w.garmin_workout_id = wid
            await session.commit()
        sched = await run_in_threadpool(client.schedule_workout, wid, w.date)
    except Exception as exc:
        body = None
        resp = getattr(exc, "response", None) or getattr(
            getattr(exc, "error", None), "response", None
        )
        if resp is not None:
            body = getattr(resp, "text", None)
        logger.error(f"GARMIN push FAILED workout={w.id} date={w.date} "
                     f"payload={payload!r} response_body={body!r}")
        logger.exception(f"GARMIN push FAILED workout={w.id} date={w.date}")
        if errors is not None:
            errors.append({"workout_id": w.id, "step": "push", "msg": str(exc)[:300]})
            return None
        raise
    w.garmin_schedule_id = sched.get("workoutScheduleId") if isinstance(sched, dict) else None
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


async def sync_plan_to_garmin(session, user_id: int, *, days: int = PLAN_SYNC_WINDOW_DAYS) -> dict:
    """Reconcile the calendar with the user's plan (cleanup + forward). Requires a bound
    user provider. Returns ``{"pushed": n, "removed": n, "errors": [...]}``.

    OPS-09: a failing push no longer aborts the rest of the batch — ``push_workout``
    collects failures into ``errors`` instead. The full summary (including a failing
    run's) is persisted to ``bot_state`` under the active plan's id so ``/plan`` can show
    "last sync: ok / ⚠️ N errors" instead of the result being silently discarded."""
    await run_in_threadpool(get_provider().login)
    today = dt.date.today().isoformat()
    active = await repository.get_active_plan(session, user_id)
    active_id = active.id if active else None

    # cleanup: anything WE pushed that no longer belongs in the calendar.
    # Keep today's just-completed (done/partial) workouts on the calendar until tomorrow
    # so the watch history still shows the session for the rest of the day.
    stale = [w for w in await repository.list_pushed_workouts(session, user_id)
             if _calendar_stale(w, active_id, today)]
    removed = 0
    for w in stale:
        await remove_workout(session, w)
        removed += 1

    # forward: active plan's upcoming, in-window, unpushed, pushable sessions.
    pushed = 0
    errors: list = []
    if active_id is not None:
        for w in await select_forward(session, active_id, days=days):
            if await push_workout(session, w, errors=errors):
                pushed += 1
        await repository.set_plan_sync_summary(
            session, user_id, active_id, pushed, removed, errors
        )

    if pushed or removed or errors:
        logger.info(f"GARMIN sync user={user_id}: +{pushed} pushed, -{removed} removed, "
                    f"{len(errors)} error(s)")
    return {"pushed": pushed, "removed": removed, "errors": errors}


async def unpush_all(session, user_id: int) -> int:
    """Remove every workout we pushed for this user (across all plans) from the Garmin
    calendar and clear the stored ids. Used when the sync toggle is turned off. Requires
    a bound user provider."""
    await run_in_threadpool(get_provider().login)
    pushed = await repository.list_pushed_workouts(session, user_id)
    for w in pushed:
        await remove_workout(session, w)
    if pushed:
        logger.info(f"GARMIN unpush-all user={user_id}: removed {len(pushed)}")
    return len(pushed)


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
        if w.status == "planned" and today <= w.date <= end and _pushable(w):
            if await push_workout(session, w):
                pushed += 1
        else:
            logger.info(
                f"GARMIN edit-sync SKIP workout={w.id} date={w.date} status={w.status} "
                f"type={w.type} in_window={today <= w.date <= end} pushable={_pushable(w)}"
            )
    logger.info(f"GARMIN edit-sync user={user_id}: +{pushed} pushed, -{removed} removed "
                f"(touched {len(workouts)})")
    return {"pushed": pushed, "removed": removed}
