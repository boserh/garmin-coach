"""Plan/actual matching: link completed activities to planned workout sessions.

Runs after every Garmin sync (``bot.jobs._tick_for_user``) and is idempotent —
it only touches workouts whose status is still ``planned``. Manual ``skipped``
statuses are never overwritten.

Matching rules — runs and cycling (EP-10 phase 3)
--------------------------------------------------
* Run-type workouts (easy / long / tempo / intervals / race) are matched against
  running activities; ``cycling`` workouts against cycling activities (same engine,
  ``_match_distance_based``, parametrized by plan-type set + activity substrings — a
  cycling match just skips the pace fields, since a cycling ``PlanStep`` never carries
  ``pace_min_km``). Rest and cross sessions are ignored.
* Candidate activities: ``ActivityRecord`` rows of the matching sport within ±1 day of
  the planned date that are not yet linked to another session (``completed_activity_id``
  already set on a different workout). Cycling activity types are identified via
  ``multisport.BIKE_NEEDLES`` (not a bare "run"-style substring — Garmin's cycling
  types vary too much: "road_biking", "virtual_ride", …).
* Scoring: exact-date match is preferred; ties broken by closest distance.
* Outcome:
  - |Δdist| ≤ DIST_PARTIAL_THRESH of the planned km → ``done``
  - |Δdist| > DIST_PARTIAL_THRESH → ``partial`` (completed but off-plan)
  - No match and date < today → ``missed`` (only today's workout may still sync)

Matching rules — strength
-------------------------
* Strength workouts are matched against ``strength_training`` activities within
  ±1 day (closest date wins). There's no distance to compare, so a match is simply
  ``done``; no match with a past date → ``missed``. This is what makes completed
  strength sessions show a ✅/❌ on ``/plan`` instead of sitting forever ``planned``.
"""
import datetime as dt
import logging
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import gap
from app.db.models import ActivityRecord, PlannedWorkout, WorkoutStatus
from app.garmin import repository
from app.multisport import BIKE_NEEDLES

logger = logging.getLogger("matching")

# |actual − planned| / planned > this threshold → ``partial`` instead of ``done``.
DIST_PARTIAL_THRESH = 0.25

# Workout types we try to match against running activities.
_PLAN_RUN_TYPES = {"easy", "long", "tempo", "intervals", "race"}

# Workout types matched against strength activities (presence-only, no distance).
_PLAN_STRENGTH_TYPES = {"strength"}

# EP-10 phase 3: workout types matched against cycling activities, same dist-delta scoring
# as running.
_PLAN_CYCLING_TYPES = {"cycling"}

# Activity type substrings that identify a running / strength / cycling activity. Cycling
# reuses NF-05's ``multisport.BIKE_NEEDLES`` — a single source for "is this a bike
# activity" rather than a second hand-rolled keyword list (running's "run" substring
# alone would miss "road_biking"/"virtual_ride"/etc, which don't contain "run").
_ACT_RUN_SUBSTR = ("run",)
_ACT_STRENGTH_SUBSTR = ("strength",)
_ACT_CYCLING_SUBSTR = BIKE_NEEDLES


def _is_manual(w: PlannedWorkout) -> bool:
    """ST-21: a workout whose status was set by hand carries ``match_info.manual`` — the
    auto-matcher must never overwrite it (a user's correction outranks the best-effort
    matcher, symmetric to the existing "already matched / skipped" skip). A ``manual`` row is
    typically already out of ``planned`` status anyway, but this guards the edge where a
    manual state left it planned."""
    return isinstance(w.match_info, dict) and bool(w.match_info.get("manual"))


def session_target_pace(steps) -> Optional[float]:
    """The midpoint target pace (min/km) of the WHOLE session, or None when no single
    pace target covers it.

    Only then is "whole-run average pace vs plan" a comparison at all. An interval day
    ("розминка 1.5 км + 5×2 хв на 5:55–6:15 + заминка 1.5 км") has a target for its work
    steps only; this used to return that target, the matcher paired it with the average
    over the warmup, jogs and cooldown too (6:52 vs 6:05), and the analyst dutifully
    reported a comparison it then had to explain was meaningless. Structured sessions are
    judged per step by ``step_match`` (NF-14) instead."""
    from app.stepmatch import flatten_steps

    flat = flatten_steps(steps if isinstance(steps, list) else None)
    if not flat:
        return None
    ranges = set()
    for s in flat:
        p = s.get("pace_min_km")
        if s.get("kind") not in ("run", "tempo") or not (
                isinstance(p, (list, tuple)) and len(p) == 2):
            return None           # a warmup/recovery/untargeted step: the target is partial
        try:
            ranges.add((float(p[0]), float(p[1])))
        except (TypeError, ValueError):
            return None
    if len(ranges) != 1:
        return None               # progression / mixed targets: no one number to compare
    fast, slow = ranges.pop()
    return (fast + slow) / 2


def _extract_target_pace(w: PlannedWorkout) -> Optional[float]:
    return session_target_pace(w.steps)


async def _get_unlinked_activities(
    session: AsyncSession, user_id: int, date_from: str, date_to: str,
    type_substrs: tuple,
) -> list:
    """Activities matching ANY of ``type_substrs`` for this user in [date_from, date_to]
    that haven't been linked to a planned workout yet."""
    rows = (
        await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.user_id == user_id,
                or_(*(ActivityRecord.type.contains(s) for s in type_substrs)),
                ActivityRecord.date.is_not(None),
                ActivityRecord.date >= date_from,
                ActivityRecord.date <= date_to,
                ActivityRecord.is_hidden.is_(False),   # ST-17: never match a hidden activity
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
    """Match completed activities (runs + strength) to planned workouts for this user.

    Idempotent: only workouts with ``status == 'planned'`` are touched; already-matched
    or manually skipped workouts are left unchanged.

    Returns ``{"done": n, "partial": n, "missed": n}`` for logging.
    """
    plan = await repository.get_active_plan(session, user_id)
    if plan is None:
        return {"done": 0, "partial": 0, "missed": 0}

    today_s = dt.date.today().isoformat()

    done, partial, missed = await _match_distance_based(
        session, plan, user_id, today_s,
        plan_types=_PLAN_RUN_TYPES, act_substrs=_ACT_RUN_SUBSTR, with_pace=True,
    )
    c_done, c_partial, c_missed = await _match_distance_based(
        session, plan, user_id, today_s,
        plan_types=_PLAN_CYCLING_TYPES, act_substrs=_ACT_CYCLING_SUBSTR, with_pace=False,
    )
    s_done, s_missed = await _match_strength(session, plan, user_id, today_s)

    await session.commit()
    return {
        "done": done + c_done + s_done,
        "partial": partial + c_partial,
        "missed": missed + c_missed + s_missed,
    }


def distance_verdict(plan_dist: float, actual_dist: float) -> str:
    """``done`` or ``partial`` for a matched session, by how far the distance landed from
    the plan (> :data:`DIST_PARTIAL_THRESH` → partial). No planned distance → done."""
    if plan_dist and plan_dist > 0:
        if abs((actual_dist or 0.0) - plan_dist) / plan_dist > DIST_PARTIAL_THRESH:
            return WorkoutStatus.PARTIAL
    return WorkoutStatus.DONE


def rescore_distance(w, dist_km: float) -> Optional[tuple]:
    """What an already-matched session's verdict becomes once its planned ``dist_km`` is
    corrected to ``dist_km`` (``app.cli fix-plan-steps``) — matching only ever looks at
    ``planned`` rows, so a verdict scored against a wrong headline would otherwise stand
    for good. Returns ``(status, match_info)`` to store, or None when nothing changes, the
    row isn't auto-matched, or it was corrected by hand (ST-21). Pure: mutates nothing."""
    info = w.match_info if isinstance(w.match_info, dict) else None
    if (info is None or _is_manual(w)
            or w.status not in (WorkoutStatus.DONE, WorkoutStatus.PARTIAL)):
        return None
    actual = info.get("actual_dist_km")
    if not isinstance(actual, (int, float)):
        return None
    delta = round(actual - (dist_km or 0.0), 2)
    status = distance_verdict(dist_km or 0.0, actual)
    if delta == info.get("dist_delta_km") and status == w.status:
        return None
    return status, {**info, "dist_delta_km": delta}


async def _match_distance_based(
    session: AsyncSession, plan, user_id: int, today_s: str, *,
    plan_types: set, act_substrs: tuple, with_pace: bool,
) -> tuple[int, int, int]:
    """Shared engine (CODE-06-style) for run and cycling matching — both score a
    candidate activity within ±1 day by distance delta; only running's ``match_info``
    carries a pace comparison (``with_pace``), since a cycling ``PlanStep`` never sets
    ``pace_min_km`` — ``_extract_target_pace`` would just return ``None`` for it anyway,
    but skipping the pace keys entirely keeps a cycling match's context honest.
    Returns (done, partial, missed). Mutates workouts in place; the caller commits."""
    # Only workouts of this plan-type set that are still in planned state and have
    # passed (≤ today).
    workouts = [
        w for w in await repository.list_workouts(session, plan.id)
        if w.status == WorkoutStatus.PLANNED
        and not _is_manual(w)                     # ST-21: honour a manual correction
        and (w.type or "").lower() in plan_types
        and w.date <= today_s
        and w.date >= (plan.start_date or "")
    ]
    if not workouts:
        return 0, 0, 0

    min_date = min(w.date for w in workouts)
    max_date = max(w.date for w in workouts)
    range_from = (dt.date.fromisoformat(min_date) - dt.timedelta(days=1)).isoformat()
    range_to = (dt.date.fromisoformat(max_date) + dt.timedelta(days=1)).isoformat()

    activities = await _get_unlinked_activities(
        session, user_id, range_from, range_to, act_substrs)

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

        match_info = {
            "dist_delta_km": round(actual_dist - plan_dist, 2),
            "actual_dist_km": best.dist_km,
            "activity_date": best.date,
        }
        if with_pace:
            # Actual pace in min/km (None when duration or distance is missing).
            actual_pace: Optional[float] = None
            if best.dur_min and best.dist_km and best.dist_km > 0:
                actual_pace = best.dur_min / best.dist_km
            # EP-15: on a hilly route (elevation gain > gap.HILLY_GAIN_PER_KM), "on pace"
            # should read by grade-adjusted effort, not the raw split — a route that isn't
            # significantly hilly, or has no elevation data at all (old runs), falls
            # straight through to the raw pace unchanged.
            if actual_pace is not None and best.series:
                actual_pace = gap.effective_pace_min_km(best.series, actual_pace)
            match_info["actual_pace_minkm"] = (
                round(actual_pace, 2) if actual_pace is not None else None)
            match_info["plan_pace_minkm"] = _extract_target_pace(w)

        w.completed_activity_id = best.id
        w.match_info = match_info

        w.status = distance_verdict(plan_dist, actual_dist)
        if w.status == WorkoutStatus.PARTIAL:
            partial += 1
        else:
            done += 1

        logger.info(
            f"MATCH {w.status} plan_id={plan.id} workout={w.id} date={w.date} "
            f"Δdist={w.match_info['dist_delta_km']:+.2f}km activity={best.id}"
        )

    return done, partial, missed


async def _match_strength(
    session: AsyncSession, plan, user_id: int, today_s: str
) -> tuple[int, int]:
    """Match strength workouts against ``strength_training`` activities (presence-only,
    closest date wins). Returns (done, missed). Mutates workouts in place; the caller
    commits."""
    workouts = [
        w for w in await repository.list_workouts(session, plan.id)
        if w.status == WorkoutStatus.PLANNED
        and not _is_manual(w)                     # ST-21: honour a manual correction
        and (w.type or "").lower() in _PLAN_STRENGTH_TYPES
        and w.date <= today_s
        and w.date >= (plan.start_date or "")
    ]
    if not workouts:
        return 0, 0

    min_date = min(w.date for w in workouts)
    max_date = max(w.date for w in workouts)
    range_from = (dt.date.fromisoformat(min_date) - dt.timedelta(days=1)).isoformat()
    range_to = (dt.date.fromisoformat(max_date) + dt.timedelta(days=1)).isoformat()

    activities = await _get_unlinked_activities(
        session, user_id, range_from, range_to, _ACT_STRENGTH_SUBSTR)

    used_ids: set = set()
    done = missed = 0

    for w in sorted(workouts, key=lambda x: x.date):
        w_date = dt.date.fromisoformat(w.date)
        candidates = [
            a for a in activities
            if a.id not in used_ids
            and a.date is not None
            and abs((dt.date.fromisoformat(a.date) - w_date).days) <= 1
        ]

        if not candidates:
            # Activity hasn't synced yet; only today's session may still sync.
            if w.date < today_s:
                w.status = WorkoutStatus.MISSED
                missed += 1
                logger.debug(
                    f"MATCH missed(strength) plan_id={plan.id} workout={w.id} date={w.date}")
            continue

        best = min(
            candidates, key=lambda a: abs((dt.date.fromisoformat(a.date) - w_date).days))
        used_ids.add(best.id)

        w.completed_activity_id = best.id
        w.match_info = {"activity_date": best.date, "actual_dist_km": None}
        w.status = WorkoutStatus.DONE
        done += 1

        logger.info(
            f"MATCH done(strength) plan_id={plan.id} workout={w.id} date={w.date} "
            f"activity={best.id}"
        )

    return done, missed
