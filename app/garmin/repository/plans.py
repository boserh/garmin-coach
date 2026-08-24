"""TrainingPlan + PlannedWorkout reads/writes: active plan, workouts, compliance,
step-match, plan creation/archival/extension, strength days and plan-op apply. Split
out of the flat ``repository.py`` (B1)."""
import datetime as dt
import logging
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import longrun, plansteps
from app.db.models import (
    ActivityRecord,
    PlannedWorkout,
    TrainingPlan,
    WorkoutStatus,
)
from app.garmin import exercises
from app.garmin.repository.core import _dump_steps

logger = logging.getLogger("api")

# ---------- TRAINING PLAN ----------

# NF-30: a plan in the return-to-run protocol is "paused", NOT archived — it keeps its future
# sessions and stays the user's current plan, so /plan still shows it and the protocol can hand
# it back afterwards. Anything that asks for "the current plan" must therefore see it.
CURRENT_PLAN_STATUSES = ("active", "paused")


def _consistent(dist_km, steps, *, steps_given: bool, where: str):
    """Store a headline distance and its structured steps only in agreement — see
    ``app.plansteps``. ``steps`` is already ``_dump_steps``-serialized.

    Both columns are model output written independently, and an adaptation that eases a
    session ("зменшую лонг до 5 км") returns a `modify` with ``dist_km`` and no ``steps``,
    which used to leave the original 6000 m step in place: the header said 5.0 km while the
    workout pushed to Garmin was still the full one. Every mismatch is logged — the numbers
    are supposed to agree at the source (the prompts demand it), so one showing up here means
    a prompt regressed, not just a row to patch. That claim only holds because a session whose
    steps are partly prescribed in TIME is not a mismatch at all and never reaches this
    warning (``plansteps.describes_distance``) — its metres are unknown here, so the headline
    stands as written."""
    gap = plansteps.mismatch(dist_km, steps)
    if gap is not None and gap > plansteps.TOLERANCE:
        logger.warning(
            "PLAN dist/steps mismatch (%s): dist_km=%s steps=%.0fm (%.0f%%) — %s",
            where, dist_km, plansteps.total_dist_m(steps) or 0.0, gap * 100,
            "steps win" if steps_given else "steps rescaled",
        )
    return plansteps.reconcile(dist_km, steps, steps_given=steps_given)


async def _relabel_long_runs(session: AsyncSession, plan_id: int,
                             dates) -> List[PlannedWorkout]:
    """Demote every ``type="long"`` row that does not earn the label, across each ISO week
    the given dates touch. Returns the rows changed (so a caller re-syncs them to Garmin —
    the type is part of the pushed workout's name).

    This runs on WRITE, next to ``_consistent``, and for the same reason: the label is
    model output produced one session at a time, while what makes it true is a property of
    the whole week. See ``app.longrun`` for the rule.
    """
    weeks = {w for w in (longrun.iso_week(d) for d in dates) if w}
    if not weeks:
        return []
    days = [dt.date.fromisoformat(d) for d in dates if longrun.iso_week(d)]
    mondays = [d - dt.timedelta(days=d.weekday()) for d in days]
    lo, hi = min(mondays), max(mondays) + dt.timedelta(days=6)
    await session.flush()   # rows added in this transaction must be visible below
    rows = (
        await session.execute(
            select(PlannedWorkout).where(
                PlannedWorkout.plan_id == plan_id,
                PlannedWorkout.date >= lo.isoformat(),
                PlannedWorkout.date <= hi.isoformat(),
            )
        )
    ).scalars().all()

    by_week: dict = {}
    for r in rows:
        by_week.setdefault(longrun.iso_week(r.date), []).append(r)

    changed: List[PlannedWorkout] = []
    for week in weeks:
        week_rows = by_week.get(week) or []
        unearned = longrun.unearned_long_dates(week_rows)
        for r in week_rows:
            if r.date in unearned and (r.type or "").lower() == longrun.LONG:
                logger.warning(
                    "PLAN long-run relabel (%s): %s %.1f km is not this week's long run "
                    "\u2014 stored as %s", week, r.date, r.dist_km or 0.0,
                    longrun.DEMOTED_TYPE,
                )
                r.type = longrun.DEMOTED_TYPE
                changed.append(r)
    return changed


async def get_active_plan(session: AsyncSession, user_id: int):
    """This user's current TrainingPlan (active or NF-30-paused), or None."""
    return (
        await session.execute(
            select(TrainingPlan)
            .where(TrainingPlan.user_id == user_id,
                   TrainingPlan.status.in_(CURRENT_PLAN_STATUSES))
            .order_by(TrainingPlan.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def set_plan_paused(session: AsyncSession, plan, paused: bool) -> None:
    """Pause (NF-30) or resume one plan. A pause is deliberately NOT an archive: the sessions
    stay, the plan stays current, and the protocol's own sessions are written into the same
    plan alongside them."""
    plan.status = "paused" if paused else "active"


async def list_plans(session: AsyncSession, user_id: int, status: Optional[str] = None):
    """This user's plans (newest first); optionally filtered by status."""
    stmt = select(TrainingPlan).where(TrainingPlan.user_id == user_id)
    if status:
        stmt = stmt.where(TrainingPlan.status == status)
    return (await session.execute(stmt.order_by(TrainingPlan.id.desc()))).scalars().all()


async def get_plan(session: AsyncSession, user_id: int, plan_id: int):
    """One plan by id, scoped to the user (active or archived)."""
    return (
        await session.execute(
            select(TrainingPlan).where(
                TrainingPlan.id == plan_id, TrainingPlan.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def list_workouts(
    session: AsyncSession, plan_id: int, *, upcoming_only: bool = False,
    today: Optional[dt.date] = None,
) -> List[PlannedWorkout]:
    """Workouts of a plan, oldest first. ``upcoming_only`` keeps today+ planned ones —
    where "today" is the athlete's own date (ST-14) when the caller passes one, and the
    process date otherwise."""
    stmt = select(PlannedWorkout).where(PlannedWorkout.plan_id == plan_id)
    if upcoming_only:
        stmt = stmt.where(
            PlannedWorkout.date >= (today or dt.date.today()).isoformat(),
            PlannedWorkout.status == "planned",
        )
    return (await session.execute(stmt.order_by(PlannedWorkout.date))).scalars().all()


async def get_workout_for_activity(
    session: AsyncSession, user_id: int, activity_id: int
) -> Optional[PlannedWorkout]:
    """The PlannedWorkout (if any) matched to this activity by ``matching.match_activities``
    (``completed_activity_id``). Scoped to the user so cross-user ids can't leak."""
    return (
        await session.execute(
            select(PlannedWorkout).where(
                PlannedWorkout.user_id == user_id,
                PlannedWorkout.completed_activity_id == activity_id,
            )
        )
    ).scalar_one_or_none()


# ---------- MANUAL WORKOUT STATUS (ST-21) ----------

# Which plan-workout types a manual link may match against (v1 — run/cycling only, no
# strength manual match; a strength session has no distance to reconcile anyway).
_MANUAL_MATCH_TYPES = {"easy", "long", "tempo", "intervals", "race", "cycling"}


async def get_workout(session: AsyncSession, user_id: int, workout_id: int):
    """One PlannedWorkout by id, scoped to the user (None if missing / not theirs)."""
    return (
        await session.execute(
            select(PlannedWorkout).where(
                PlannedWorkout.id == workout_id, PlannedWorkout.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def link_candidates(
    session: AsyncSession, user_id: int, workout: PlannedWorkout
) -> List[ActivityRecord]:
    """Own, visible activities of a compatible sport within ±1 day of ``workout``'s date —
    the pick list for a manual "🔗 привʼязати" (ST-21). Running plan types offer running
    activities, cycling offers cycling; already-hidden activities are excluded. Newest first.
    Empty for a strength/rest/cross session (no manual match in v1)."""
    wtype = (workout.type or "").lower()
    if wtype not in _MANUAL_MATCH_TYPES or not workout.date:
        return []
    if wtype == "cycling":
        from app.multisport import BIKE_NEEDLES
        substrs = BIKE_NEEDLES
    else:
        substrs = ("run",)
    w_date = dt.date.fromisoformat(workout.date)
    lo = (w_date - dt.timedelta(days=1)).isoformat()
    hi = (w_date + dt.timedelta(days=1)).isoformat()
    from sqlalchemy import or_
    rows = (
        await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.user_id == user_id,
                ActivityRecord.is_hidden.is_(False),
                ActivityRecord.date.is_not(None),
                ActivityRecord.date >= lo,
                ActivityRecord.date <= hi,
                or_(*(ActivityRecord.type.contains(s) for s in substrs)),
            ).order_by(ActivityRecord.date.desc(), ActivityRecord.id.desc())
        )
    ).scalars().all()
    return list(rows)


async def set_workout_status(
    session: AsyncSession, user_id: int, workout_id: int, action: str,
    *, activity_id: Optional[int] = None,
):
    """Manually override a past session's plan/actual state (ST-21). ``action`` is one of:

    * ``done``   — mark completed by hand (treadmill / a tracker that never synced): status
      ``done``, tag ``match_info.manual``; an existing activity link is kept.
    * ``skipped``— mark not done: status ``skipped``, un-link any matched activity (freeing it
      for another session), tag manual.
    * ``unlink`` — drop a wrong match: clear ``completed_activity_id``/``match_info`` and send
      the session back to ``missed``/``planned`` by date, so the auto-matcher may try again.
    * ``link``   — attach a specific own activity (``activity_id``, must be a compatible-sport
      row within ±1 day): status ``done``, tag manual with the activity's date/distance.

    A ``manual`` tag makes the auto-matcher leave the row alone on subsequent runs (see
    ``matching``). Returns the workout, or None if it isn't this user's (or the link target is
    invalid). Does not commit."""
    w = await get_workout(session, user_id, workout_id)
    if w is None:
        return None
    today_s = dt.date.today().isoformat()
    if action == "unlink":
        w.completed_activity_id = None
        w.match_info = None
        w.status = WorkoutStatus.MISSED if (w.date or "") < today_s else WorkoutStatus.PLANNED
        return w
    if action == "skipped":
        w.completed_activity_id = None
        w.match_info = {"manual": True}
        w.status = WorkoutStatus.SKIPPED
        return w
    if action == "done":
        info = dict(w.match_info or {})
        info["manual"] = True
        w.match_info = info
        w.status = WorkoutStatus.DONE
        return w
    if action == "link":
        if activity_id is None:
            return None
        candidates = await link_candidates(session, user_id, w)
        act = next((a for a in candidates if a.id == activity_id), None)
        if act is None:
            return None
        w.completed_activity_id = act.id
        w.match_info = {
            "manual": True,
            "activity_date": act.date,
            "actual_dist_km": act.dist_km,
        }
        w.status = WorkoutStatus.DONE
        return w
    return None


async def upcoming_plan_workouts(
    session: AsyncSession, user_id: int, days: int = 2,
    today: Optional[dt.date] = None,
) -> List[PlannedWorkout]:
    """Today's and the next ``days-1`` days' planned workouts from the active plan.
    Returns [] when there is no active plan or nothing in the window.

    ``today`` lets a caller pass the user's OWN date (their timezone, ST-14) instead of
    the process one — otherwise a user a few hours ahead gets a window shifted by a day.
    """
    plan = await get_active_plan(session, user_id)
    if plan is None:
        return []
    today = today or dt.date.today()
    window_end = (today + dt.timedelta(days=days - 1)).isoformat()
    return (
        await session.execute(
            select(PlannedWorkout).where(
                PlannedWorkout.plan_id == plan.id,
                PlannedWorkout.date >= today.isoformat(),
                PlannedWorkout.date <= window_end,
                PlannedWorkout.status == "planned",
            ).order_by(PlannedWorkout.date)
        )
    ).scalars().all()


async def recent_plan_workouts(
    session: AsyncSession, user_id: int, days: int = 7,
    today: Optional[dt.date] = None,
) -> List[PlannedWorkout]:
    """The active plan's sessions over the last ``days`` days, INCLUDING today, any status
    (the mirror of :func:`upcoming_plan_workouts`, which looks forward at planned ones only).

    NF-18 reads this to count a streak of consecutive ``missed`` sessions, so it must see
    ``done``/``partial``/``skipped`` rows too — those are what break a streak. Returns []
    when there is no active plan. ``today`` takes the user's OWN date (ST-14).
    """
    plan = await get_active_plan(session, user_id)
    if plan is None:
        return []
    today = today or dt.date.today()
    start = (today - dt.timedelta(days=days)).isoformat()
    return (
        await session.execute(
            select(PlannedWorkout).where(
                PlannedWorkout.plan_id == plan.id,
                PlannedWorkout.date >= start,
                PlannedWorkout.date <= today.isoformat(),
            ).order_by(PlannedWorkout.date)
        )
    ).scalars().all()


async def load_forecast(
    session: AsyncSession, user_id: int, *, today: Optional[dt.date] = None,
) -> Optional[dict]:
    """NF-20: this ISO week's forecast load + forward-looking ACWR for the active plan —
    the fetch+shape wiring around the pure ``app.loadforecast`` math, shared by ``/plan``,
    the dashboard, and the adaptation context so all three read the identical number.
    ``None`` when there's no active plan."""
    from app import loadforecast
    from app.core.config import settings

    plan = await get_active_plan(session, user_id)
    if plan is None:
        return None
    today = today or dt.date.today()
    end = loadforecast.week_end(today).isoformat()
    today_s = today.isoformat()
    workouts = await list_workouts(session, plan.id)
    remaining = [
        {"type": w.type, "dist_km": w.dist_km, "steps": w.steps, "date": w.date}
        for w in workouts
        if w.status == "planned" and today_s <= w.date <= end
    ]

    from app.garmin.repository.core import count_daily_metrics, typical_run_pace
    from app.garmin.repository.stats import weekly_activity_load

    history_days = await count_daily_metrics(session, user_id)
    weekly = await weekly_activity_load(
        session, user_id, weeks=loadforecast.MIN_CHRONIC_WEEKS + 2
    )
    by_week = {w["week"]: w["load"] for w in weekly}
    this_week = today.strftime("%G-W%V")
    done_load = by_week.get(this_week, 0.0)
    chronic = [
        by_week.get((today - dt.timedelta(weeks=n)).strftime("%G-W%V"), 0.0)
        for n in range(1, loadforecast.MIN_CHRONIC_WEEKS + 1)
    ]
    anchor_pace = await typical_run_pace(session, user_id)
    out = loadforecast.forecast_week(
        remaining_sessions=remaining, done_load=done_load,
        chronic_weekly_loads=chronic, history_days=history_days,
        anchor_pace=anchor_pace,
        warn_acwr=settings.FORECAST_ACWR_WARN, high_acwr=settings.FORECAST_ACWR_HIGH,
    )
    # UI-05 shows the week as a trace, not a single number, so it needs the same
    # per-session loads the total was summed from — scored by the module's own
    # ``session_load``, never re-derived here. Additive: existing callers ignore it.
    out["done_load"] = round(done_load, 1)
    out["sessions"] = [
        {"date": s["date"], "type": s["type"], "dist_km": s["dist_km"],
         "load": round(loadforecast.session_load(s, anchor_pace), 1)}
        for s in remaining
    ]
    return out


async def weekly_compliance(
    session: AsyncSession, plan_id: int, today: Optional[dt.date] = None,
) -> dict:
    """Per-week compliance summary for a plan, keyed by ISO week string ('YYYY-Www').

    Each entry: ``{total, done, remaining, pace_deltas: [float, ...], overreached}``.
    * ``total`` — run-type workouts (not rest/cross/strength) in that week.
    * ``done`` — workouts with status done or partial.
    * ``remaining`` — of those, the ones that have not come due yet: still ``planned``
      and dated today or later (the matcher only calls a session ``missed`` once its
      date has passed). Zero for every week that is over. It exists because
      ``done``/``total`` alone cannot tell "skipped three sessions" from "it is
      Wednesday": the ``/plan`` view wants the whole week's total, while anything
      reasoning about compliance must not read the back half of the current week as
      misses — see ``app.analysis.plans._recent_compliance``.
    * ``pace_deltas`` — list of (actual − plan) pace values in min/km for matched workouts
      where both sides are known (positive = slower, negative = faster).
    * ``overreached`` — count of *easy-intent* sessions (easy/recovery/base/long) done but
      whose post-run check-in RPE was hard (≥ ``subjective.HARD_RPE``): "did it, but it felt
      much harder than the session called for" (EP-12 phase 3 plan/fact status). Zero when
      there are no check-ins.
    """
    from app import subjective as subjective_mod

    workouts = (
        await session.execute(
            select(PlannedWorkout).where(PlannedWorkout.plan_id == plan_id)
        )
    ).scalars().all()

    # RPE per matched activity, for the overreached flag (one query for all done workouts).
    done_ids = [w.completed_activity_id for w in workouts if w.completed_activity_id]
    rpe_by_id: dict = {}
    if done_ids:
        arows = (
            await session.execute(
                select(ActivityRecord.id, ActivityRecord.subjective).where(
                    ActivityRecord.id.in_(done_ids)
                )
            )
        ).all()
        for aid, subj in arows:
            if isinstance(subj, dict) and isinstance(subj.get("rpe"), (int, float)):
                rpe_by_id[aid] = subj["rpe"]

    _SKIP = {"rest", "cross", "strength"}
    today_s = (today or dt.date.today()).isoformat()
    buckets: dict = {}
    for w in workouts:
        if (w.type or "").lower() in _SKIP:
            continue
        try:
            week = dt.date.fromisoformat(w.date).strftime("%G-W%V")
        except (ValueError, TypeError):
            continue
        b = buckets.setdefault(
            week, {"total": 0, "done": 0, "remaining": 0,
                   "pace_deltas": [], "overreached": 0})
        b["total"] += 1
        if w.status == WorkoutStatus.PLANNED and w.date >= today_s:
            b["remaining"] += 1
        if w.status in ("done", "partial"):
            b["done"] += 1
            if isinstance(w.match_info, dict):
                ap = w.match_info.get("actual_pace_minkm")
                pp = w.match_info.get("plan_pace_minkm")
                if ap is not None and pp is not None:
                    b["pace_deltas"].append(round(ap - pp, 2))
            rpe = rpe_by_id.get(w.completed_activity_id)
            if (rpe is not None and rpe >= subjective_mod.HARD_RPE
                    and (w.type or "").lower() in subjective_mod.EASY_TYPES):
                b["overreached"] += 1
    return buckets


STEP_MATCH_DAYS = 30   # how far back the adaptation context looks for step-level results


async def recent_step_match(
    session: AsyncSession, plan_id: int, days: int = STEP_MATCH_DAYS
) -> List[dict]:
    """This plan's recent completed sessions' step-level plan-vs-actual results (NF-14) —
    ``[{date, steps_hit, steps_total}]``, oldest first. Only workouts matched to an
    activity that actually has a ``step_match`` (i.e. pushed with structure and scored)
    contribute a row; everything else is silently skipped."""
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    workouts = (
        await session.execute(
            select(PlannedWorkout).where(
                PlannedWorkout.plan_id == plan_id,
                PlannedWorkout.date >= cutoff,
                PlannedWorkout.completed_activity_id.is_not(None),
            ).order_by(PlannedWorkout.date)
        )
    ).scalars().all()
    ids = [w.completed_activity_id for w in workouts]
    if not ids:
        return []
    rows = (
        await session.execute(
            select(ActivityRecord.id, ActivityRecord.date, ActivityRecord.step_match).where(
                ActivityRecord.id.in_(ids), ActivityRecord.step_match.is_not(None)
            )
        )
    ).all()
    by_id = {aid: (date, sm) for aid, date, sm in rows if isinstance(sm, dict)}
    out = []
    for w in workouts:
        hit = by_id.get(w.completed_activity_id)
        if not hit:
            continue
        date, sm = hit
        out.append({"date": date, "steps_hit": sm.get("steps_hit"),
                    "steps_total": sm.get("steps_total")})
    return out


async def list_pushed_workouts(session: AsyncSession, user_id: int) -> List[PlannedWorkout]:
    """This user's workouts already pushed to Garmin (``garmin_workout_id`` set), across
    all plans — for the sync cleanup pass. (A BigInteger column → real SQL NULL, so
    ``is_not(None)`` works here, unlike the JSON ``series`` gotcha.)"""
    return (
        await session.execute(
            select(PlannedWorkout).where(
                PlannedWorkout.user_id == user_id,
                PlannedWorkout.garmin_workout_id.is_not(None),
            )
        )
    ).scalars().all()


async def create_plan(
    session: AsyncSession,
    user_id: int,
    *,
    goal: str,
    goal_label: Optional[str],
    target_date: Optional[str],
    start_date: Optional[str],
    days_per_week: Optional[int],
    intensity: Optional[str],
    intake: Optional[dict],
    summary: Optional[str],
    workouts: list,
) -> TrainingPlan:
    """Create a new active plan (archiving any prior active one) and its workouts.
    ``workouts`` is a list of ``PlanWorkout`` (or anything with the same attrs)."""
    prior = (
        await session.execute(
            select(TrainingPlan).where(
                TrainingPlan.user_id == user_id,
                TrainingPlan.status.in_(CURRENT_PLAN_STATUSES),
            )
        )
    ).scalars().all()
    for p in prior:
        p.status = "archived"

    plan = TrainingPlan(
        user_id=user_id, goal=goal, goal_label=goal_label, target_date=target_date,
        start_date=start_date, days_per_week=days_per_week, intensity=intensity,
        intake=intake, summary=summary, status="active",
    )
    session.add(plan)
    await session.flush()  # assign plan.id
    for w in workouts:
        dist_km, steps = _consistent(
            w.dist_km, _dump_steps(getattr(w, "steps", None)),
            steps_given=True, where=f"create {w.date}",
        )
        session.add(PlannedWorkout(
            plan_id=plan.id, user_id=user_id, date=w.date, week=w.week,
            type=w.type, dist_km=dist_km, description=w.description,
            steps=steps, status="planned",
        ))
    await _relabel_long_runs(session, plan.id, [w.date for w in workouts])
    await session.commit()
    await prune_redundant_rest(session, plan.id)
    return plan


async def archive_plan(session: AsyncSession, plan: TrainingPlan) -> None:
    """Archive a plan. NF-23: this also cancels an unsent post-race debrief by pre-setting its
    send guard — a plan the runner archived (they moved on, or the race didn't happen) must
    not produce a race debrief days later, and a guard that's already "1" is the cheapest way
    to say that without a second state key."""
    from app import race
    from app.garmin.repository.state import set_state

    plan.status = "archived"
    if plan.user_id is not None:
        await set_state(session, plan.user_id,
                        race.stage_guard_key(plan.id, "debrief"), "1")
    await session.commit()


async def last_workout_date(session: AsyncSession, plan_id: int) -> Optional[str]:
    """The latest workout date (ISO string) in a plan, or None if it has no workouts.
    Used by the open-ended auto-extend job to know how far the plan currently reaches."""
    return (
        await session.execute(
            select(func.max(PlannedWorkout.date)).where(
                PlannedWorkout.plan_id == plan_id
            )
        )
    ).scalar_one_or_none()


async def append_workouts(
    session: AsyncSession, plan: TrainingPlan, workouts: list, *, week_offset: int = 0
) -> int:
    """Append more run workouts to an EXISTING plan (open-ended extension) — unlike
    ``create_plan`` this neither archives the plan nor touches prior rows. ``week_offset``
    is added to each workout's ``week`` so the new block continues the plan's numbering.
    Returns the number of rows added."""
    added = 0
    for w in workouts:
        base_week = getattr(w, "week", None) or 1
        dist_km, steps = _consistent(
            w.dist_km, _dump_steps(getattr(w, "steps", None)),
            steps_given=True, where=f"extend {w.date}",
        )
        session.add(PlannedWorkout(
            plan_id=plan.id, user_id=plan.user_id, date=w.date,
            week=base_week + week_offset,
            type=w.type, dist_km=dist_km, description=w.description,
            steps=steps, status="planned",
        ))
        added += 1
    await _relabel_long_runs(session, plan.id, [w.date for w in workouts])
    await session.commit()
    await prune_redundant_rest(session, plan.id)
    return added


_WEEKDAY = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


async def add_strength_workouts(session: AsyncSession, plan: TrainingPlan,
                                assignments: dict, snapshots: Optional[dict] = None,
                                custom: Optional[dict] = None, *,
                                start: Optional[str] = None, end: Optional[str] = None,
                                week_offset: int = 0) -> int:
    """Add strength sessions on fixed weekdays across the plan's date range. ``assignments``
    maps a weekday slug (mon..sun) → {"id", "name"} of the saved Garmin workout to place on
    that weekday **every week** (a fixed pairing, not a rotation). Each carries a
    ``garmin_template_id`` (cloned on push). ``snapshots`` (optional, keyed by workout id)
    caches each template's contents ({name?, exercises}) onto the row's ``strength_snapshot``
    so ``/plan`` renders the exercise accordion from the DB. ``custom`` maps a weekday slug →
    EITHER an already-sanitised ``strength_plan`` dict, placed on that weekday **every
    week** (the pre-EP-03 shape — a confirmed setup-form preview, or a reused extension
    session), OR (EP-03) a **list** of sanitised dicts, one per week of THIS call's window
    (0-based: the list's Nth entry lands on the Nth weekly occurrence, clamped to the last
    entry if the window runs longer than the list) — a week-by-week progression. A weekday
    in both ``assignments`` and ``custom`` prefers the saved workout. ``start``/``end``
    (ISO) override the plan's date range — the open-ended extension passes the new block's
    window so strength lands only on the freshly-added weeks; ``week_offset`` continues the
    plan's week numbering (independent of the progression index, which always counts from
    THIS call's own start). Returns the count."""
    snapshots = snapshots or {}
    by_wd = {}
    for slug, t in (assignments or {}).items():
        wd = _WEEKDAY.get(slug)
        if wd is not None and t and t.get("id"):
            by_wd[wd] = t
    custom_by_wd = {}
    for slug, sp in (custom or {}).items():
        wd = _WEEKDAY.get(slug)
        if wd is not None and sp:
            custom_by_wd[wd] = sp
    if not by_wd and not custom_by_wd:
        return 0
    # ``start``/``end`` override the plan's own range — used by the open-ended extension to
    # lay strength only across the freshly-added block. Default to the plan's date range.
    try:
        start_d = dt.date.fromisoformat(start or plan.start_date)
    except (ValueError, TypeError):
        return 0
    try:
        end_d = dt.date.fromisoformat(end or plan.target_date)
    except (ValueError, TypeError):
        end_d = start_d + dt.timedelta(weeks=12)
    if end_d < start_d:
        end_d = start_d + dt.timedelta(weeks=12)
    added = 0
    d = start_d
    while d <= end_d:
        wd = d.weekday()
        week_idx = (d - start_d).days // 7   # 0-based, THIS call's window only
        week = week_idx + 1 + week_offset
        t = by_wd.get(wd)
        cp = custom_by_wd.get(wd)
        if isinstance(cp, list):
            # EP-03 progression: pick this occurrence's week, clamped to the last entry.
            cp = cp[min(week_idx, len(cp) - 1)] if cp else None
        if t is not None:
            session.add(PlannedWorkout(
                plan_id=plan.id, user_id=plan.user_id, date=d.isoformat(),
                week=week, type="strength",
                description=t.get("name") or "Силова",
                garmin_template_id=t.get("id"),
                strength_snapshot=snapshots.get(t.get("id")), status="planned"))
            added += 1
        elif cp is not None:
            session.add(PlannedWorkout(
                plan_id=plan.id, user_id=plan.user_id, date=d.isoformat(),
                week=week, type="strength",
                description=cp.get("name") or "Силова",
                strength_plan=cp, status="planned"))
            added += 1
        d += dt.timedelta(days=1)
    await session.commit()
    # Strength lands on fixed weekdays regardless of what the generator wrote there, so
    # this is where a "no running today" note most often collides with a real session.
    await prune_redundant_rest(session, plan.id)
    return added


# A day with no session already MEANS rest — the plan page renders nothing for it. So a
# ``rest`` row is only ever a note, and it becomes actively wrong the moment something real
# lands on the same date: the generator writes "силовий/відпочинок, бігу немає" for a Monday
# it knows is a strength day, then ``add_strength_workouts`` puts the strength session right
# under it, and /plan shows "Відпочинок" and "🏋️ Силова" stacked on one date. The model can
# be told not to do it (SYSTEM_PLAN says so) but it is prose, not a guarantee — this is the
# guarantee. A lone rest row survives: there it is the only thing carrying the reason.
async def prune_redundant_rest(session: AsyncSession, plan_id: int) -> int:
    """Delete a plan's ``rest`` rows that share a date with a real session. Only untouched
    rows go (status ``planned``, nothing matched to them) — a rest day the athlete or an
    adaptation already acted on stays as history. Idempotent; returns the count removed."""
    rows = (
        await session.execute(
            select(PlannedWorkout).where(PlannedWorkout.plan_id == plan_id)
        )
    ).scalars().all()
    by_date: dict = {}
    for w in rows:
        by_date.setdefault(w.date, []).append(w)
    removed = 0
    for same_day in by_date.values():
        if len(same_day) < 2:
            continue
        if not any((w.type or "").lower() != "rest" for w in same_day):
            continue   # nothing real that day — the rest note is all there is
        for w in same_day:
            if (w.type or "").lower() != "rest":
                continue
            if w.status != "planned" or w.completed_activity_id is not None:
                continue
            await session.delete(w)
            removed += 1
    if removed:
        await session.commit()
        logger.info("PLAN pruned %d redundant rest row(s) plan=%s", removed, plan_id)
    return removed


async def workout_on_date(session: AsyncSession, plan_id: int, date: str):
    """The plan's session on a given date, or None. Public: also used to describe a
    proposed op's before-state (bot/jobs.py) without duplicating the query."""
    return (
        await session.execute(
            select(PlannedWorkout)
            .where(PlannedWorkout.plan_id == plan_id, PlannedWorkout.date == date)
            .order_by(PlannedWorkout.id)
            .limit(1)
        )
    ).scalar_one_or_none()


def _sanitize_strength(sp) -> Optional[dict]:
    """Validate a ``StrengthSession``(-like) into the stored ``strength_plan`` dict: keep
    only exercises whose ``category`` is a real Garmin code (so a hallucinated code never
    reaches the watch), drop empty blocks. Returns None if nothing valid remains."""
    if sp is None:
        return None
    data = sp.model_dump() if hasattr(sp, "model_dump") else dict(sp)
    blocks_out = []
    for b in data.get("blocks") or []:
        exs = []
        for e in b.get("exercises") or []:
            cat = (e.get("category") or "").upper()
            if not exercises.valid_category(cat):
                continue
            ex = exercises.check_exercise(cat, e.get("exercise"))
            exs.append({"category": cat, "exercise": ex,
                        "reps": e.get("reps"), "weight_kg": e.get("weight_kg")})
        if exs:
            blocks_out.append({"reps": int(b.get("reps") or 1),
                               "rest_s": b.get("rest_s"), "exercises": exs})
    if not blocks_out:
        return None
    return {"name": data.get("name"), "warmup_s": data.get("warmup_s"),
            "blocks": blocks_out}


async def apply_plan_ops(
    session: AsyncSession, plan: TrainingPlan, ops: list
) -> List[PlannedWorkout]:
    """Apply edit operations (``PlanOp``-like objects) to a plan's workouts. Returns the
    **touched** workouts (so the caller can re-sync just those to Garmin). ``move``/
    ``modify``/``skip`` target the workout on ``op.date``."""
    affected: List[PlannedWorkout] = []
    for op in ops:
        if op.action == "add":
            dist_km, steps = _consistent(
                op.dist_km, _dump_steps(getattr(op, "steps", None)),
                steps_given=True, where=f"add {op.date}",
            )
            w = PlannedWorkout(
                plan_id=plan.id, user_id=plan.user_id, date=op.date, week=op.week,
                type=op.type or "easy", dist_km=dist_km,
                description=op.description or "",
                steps=steps,
                garmin_template_id=getattr(op, "garmin_template_id", None),
                strength_plan=_sanitize_strength(getattr(op, "strength", None)),
                status="planned",
            )
            session.add(w)
            affected.append(w)
            continue
        w = await workout_on_date(session, plan.id, op.date)
        if w is None:
            continue
        if op.action == "skip":
            w.status = "skipped"
            affected.append(w)
        elif op.action == "move" and op.to_date:
            w.date = op.to_date
            affected.append(w)
        elif op.action == "modify":
            if op.type is not None:
                w.type = op.type
            if op.description is not None:
                w.description = op.description
            # Distance and steps are ONE decision, not two independent columns: a modify that
            # only eases the distance must re-cut the stale steps (they are what reaches the
            # watch), and one that sends new steps redefines the distance.
            new_steps = getattr(op, "steps", None)
            if new_steps is not None or op.dist_km is not None:
                steps_given = new_steps is not None
                steps = _dump_steps(new_steps) if steps_given else w.steps
                dist_km = op.dist_km if op.dist_km is not None else w.dist_km
                w.dist_km, w.steps = _consistent(
                    dist_km, steps, steps_given=steps_given,
                    where=f"modify {op.date}",
                )
            if getattr(op, "garmin_template_id", None) is not None:
                w.garmin_template_id = op.garmin_template_id
            if getattr(op, "strength", None) is not None:
                sp = _sanitize_strength(op.strength)
                if sp:
                    w.strength_plan = sp
            affected.append(w)
        elif op.action == "swap_exercise":
            frm = (getattr(op, "from_category", None) or "").upper()
            to = (getattr(op, "to_category", None) or "").upper()
            # reject an unmapped/invalid target so a hallucinated code never reaches Garmin
            if not frm or not exercises.valid_category(to):
                continue
            # validate the exercise name against the *target* category (it belongs to `to`)
            edit = {
                "from": frm, "to": to,
                "exercise": exercises.check_exercise(to, getattr(op, "exercise", None)),
                "reps": getattr(op, "reps", None),
            }
            w.exercise_edits = list(w.exercise_edits or []) + [edit]
            affected.append(w)
    # An op writes one session; whether it is the week's long run is a fact about the week.
    touched = [d for op in ops
               for d in (op.date, getattr(op, "to_date", None)) if d]
    for w in await _relabel_long_runs(session, plan.id, touched):
        if w not in affected:
            affected.append(w)
    await session.commit()
    return affected
