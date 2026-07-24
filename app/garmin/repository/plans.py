"""TrainingPlan + PlannedWorkout reads/writes: active plan, workouts, compliance,
step-match, plan creation/archival/extension, strength days and plan-op apply. Split
out of the flat ``repository.py`` (B1)."""
import datetime as dt
from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ActivityRecord,
    PlannedWorkout,
    TrainingPlan,
    WorkoutStatus,
)
from app.garmin import exercises
from app.garmin.repository.core import _dump_steps

# ---------- TRAINING PLAN ----------

async def get_active_plan(session: AsyncSession, user_id: int):
    """This user's current active TrainingPlan, or None."""
    return (
        await session.execute(
            select(TrainingPlan)
            .where(TrainingPlan.user_id == user_id, TrainingPlan.status == "active")
            .order_by(TrainingPlan.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


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
    session: AsyncSession, plan_id: int, *, upcoming_only: bool = False
) -> List[PlannedWorkout]:
    """Workouts of a plan, oldest first. ``upcoming_only`` keeps today+ planned ones."""
    stmt = select(PlannedWorkout).where(PlannedWorkout.plan_id == plan_id)
    if upcoming_only:
        stmt = stmt.where(
            PlannedWorkout.date >= dt.date.today().isoformat(),
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
    session: AsyncSession, user_id: int, days: int = 2
) -> List[PlannedWorkout]:
    """Today's and the next ``days-1`` days' planned workouts from the active plan.
    Returns [] when there is no active plan or nothing in the window."""
    plan = await get_active_plan(session, user_id)
    if plan is None:
        return []
    today = dt.date.today()
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


async def weekly_compliance(
    session: AsyncSession, plan_id: int
) -> dict:
    """Per-week compliance summary for a plan, keyed by ISO week string ('YYYY-Www').

    Each entry: ``{total, done, pace_deltas: [float, ...], overreached}``.
    * ``total`` — run-type workouts (not rest/cross/strength) in that week.
    * ``done`` — workouts with status done or partial.
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
    buckets: dict = {}
    for w in workouts:
        if (w.type or "").lower() in _SKIP:
            continue
        try:
            week = dt.date.fromisoformat(w.date).strftime("%G-W%V")
        except (ValueError, TypeError):
            continue
        b = buckets.setdefault(
            week, {"total": 0, "done": 0, "pace_deltas": [], "overreached": 0})
        b["total"] += 1
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
                TrainingPlan.user_id == user_id, TrainingPlan.status == "active"
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
        session.add(PlannedWorkout(
            plan_id=plan.id, user_id=user_id, date=w.date, week=w.week,
            type=w.type, dist_km=w.dist_km, description=w.description,
            steps=_dump_steps(getattr(w, "steps", None)), status="planned",
        ))
    await session.commit()
    return plan


async def archive_plan(session: AsyncSession, plan: TrainingPlan) -> None:
    plan.status = "archived"
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
        session.add(PlannedWorkout(
            plan_id=plan.id, user_id=plan.user_id, date=w.date,
            week=base_week + week_offset,
            type=w.type, dist_km=w.dist_km, description=w.description,
            steps=_dump_steps(getattr(w, "steps", None)), status="planned",
        ))
        added += 1
    await session.commit()
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
    return added


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
            w = PlannedWorkout(
                plan_id=plan.id, user_id=plan.user_id, date=op.date, week=op.week,
                type=op.type or "easy", dist_km=op.dist_km,
                description=op.description or "",
                steps=_dump_steps(getattr(op, "steps", None)),
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
            if op.dist_km is not None:
                w.dist_km = op.dist_km
            if op.description is not None:
                w.description = op.description
            if getattr(op, "steps", None) is not None:
                w.steps = _dump_steps(op.steps)
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
    await session.commit()
    return affected
