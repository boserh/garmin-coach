"""Daily/activity/records reads + all writes + report-log/cost queries.

A domain module of the split-up ``repository`` package (CODE-AUDIT-2026-07 B1); the
package ``__init__`` re-exports every public name so ``from app.garmin import repository``
and ``repository.X`` keep working unchanged. ``stats`` imports ``query_daily`` /
``ASK_DAILY_FIELDS`` from here."""
import datetime as dt
from typing import Dict, List, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.db.models import (
    ActivityRecord,
    DailyMetric,
    PersonalRecord,
    PlannedWorkout,
    ReportLog,
    TrainingPlan,
    WorkoutStatus,
)
from app.garmin.schemas import DailySummary, Payload

_DAILY_FIELDS = (
    "sleep_score", "sleep_h", "deep_h", "rem_h", "light_h", "awake_h",
    "hrv_avg", "hrv_status", "stress_avg", "stress_max", "bb_charged", "bb_drained",
    "extra",
)


def _has_data(m: DailyMetric) -> bool:
    return any(getattr(m, k) is not None for k in ("sleep_score", "hrv_avg", "stress_avg"))


def _to_summary(m: DailyMetric) -> DailySummary:
    data = {k: getattr(m, k) for k in _DAILY_FIELDS}
    data["date"] = m.date
    data["has_data"] = _has_data(m)
    return DailySummary(**data)


# ---------- READ ----------

async def read_daily_metrics(
    session: AsyncSession, user_id: int, dates: List[str]
) -> Dict[str, DailySummary]:
    """Past-day metrics already stored for this user, keyed by ISO date (the
    day-level cache)."""
    if not dates:
        return {}
    rows = (
        await session.execute(
            select(DailyMetric).where(
                DailyMetric.user_id == user_id, DailyMetric.date.in_(dates)
            )
        )
    ).scalars().all()
    return {m.date: _to_summary(m) for m in rows}


async def list_activities(session: AsyncSession, user_id: int, n: int = 5) -> List[dict]:
    """This user's most recent activities (newest first) as compact dicts for the
    bot's ``/activities`` list — keyed by the short DB ``id`` the user references."""
    rows = (
        await session.execute(
            select(ActivityRecord)
            .where(ActivityRecord.user_id == user_id,
                   ActivityRecord.is_hidden.is_(False))   # ST-17
            .order_by(ActivityRecord.date.desc(), ActivityRecord.id.desc())
            .limit(n)
        )
    ).scalars().all()
    return [
        {"id": a.id, "date": a.date, "type": a.type, "dist_km": a.dist_km,
         "dur_min": a.dur_min, "avg_hr": a.avg_hr}
        for a in rows
    ]


ASK_MAX_ROWS = 200  # EP-09: hard cap on rows a single /ask tool call may return


async def query_activities(
    session: AsyncSession, user_id: int, *,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    type: Optional[str] = None, min_dist_km: Optional[float] = None,
    limit: int = ASK_MAX_ROWS,
) -> List[dict]:
    """EP-09 ``/ask`` tool: this user's activities in a date range (both ends inclusive,
    ISO dates), optionally filtered by activity type (substring match, e.g. "running") or a
    minimum distance. Read-only, user-scoped, newest-first, capped at ``limit`` (never above
    ``ASK_MAX_ROWS``) so a tool result stays small enough for the model to read. Compact rows
    only — no series; ``get_activity_detail`` is the drill-down for one activity."""
    stmt = select(ActivityRecord).where(
        ActivityRecord.user_id == user_id,
        ActivityRecord.is_hidden.is_(False),   # ST-17: /ask never sees a hidden activity
    )
    if date_from:
        stmt = stmt.where(ActivityRecord.date >= date_from)
    if date_to:
        stmt = stmt.where(ActivityRecord.date <= date_to)
    if type:
        stmt = stmt.where(ActivityRecord.type.ilike(f"%{type}%"))
    if min_dist_km is not None:
        stmt = stmt.where(ActivityRecord.dist_km >= min_dist_km)
    stmt = stmt.order_by(ActivityRecord.date.desc(), ActivityRecord.id.desc()) \
                .limit(max(1, min(limit, ASK_MAX_ROWS)))
    rows = (await session.execute(stmt)).scalars().all()
    out = []
    for a in rows:
        pace = round(a.dur_min / a.dist_km, 2) if (a.dur_min and a.dist_km) else None
        out.append({
            "id": a.id, "date": a.date, "type": a.type, "dist_km": a.dist_km,
            "dur_min": a.dur_min, "avg_hr": a.avg_hr, "max_hr": a.max_hr,
            "avg_pace_minkm": pace,
        })
    return out


# EP-09 /ask whitelist for query_daily/aggregate_weekly — base DailyMetric columns plus a
# subset of `extra` (everything a tool call is allowed to read; keeps a hallucinated field
# name a harmless miss instead of an arbitrary-column fishing expedition).
ASK_DAILY_BASE_FIELDS = {
    "sleep_score", "sleep_h", "deep_h", "rem_h", "light_h", "awake_h",
    "hrv_avg", "hrv_status", "stress_avg", "stress_max", "bb_charged", "bb_drained",
}
ASK_DAILY_EXTRA_FIELDS = {
    "resting_hr", "readiness_score", "readiness_level", "acwr_pct", "acute_load",
    "recovery_time_h", "vo2max", "fitness_age", "endurance_score", "endurance_class",
    "race_5k_s", "race_10k_s", "race_half_s", "race_marathon_s",
    "spo2_avg", "respiration_avg",
}
ASK_DAILY_FIELDS = ASK_DAILY_BASE_FIELDS | ASK_DAILY_EXTRA_FIELDS


async def query_daily(
    session: AsyncSession, user_id: int, *,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    fields: Optional[List[str]] = None, limit: int = ASK_MAX_ROWS,
) -> List[dict]:
    """EP-09 ``/ask`` tool: daily recovery/sleep metrics in a date range (oldest first, so a
    trend reads left-to-right), restricted to ``ASK_DAILY_FIELDS`` — an unknown/misspelled
    field name is silently dropped rather than erroring the tool call. ``fields=None`` returns
    all whitelisted fields. Capped at ``limit`` rows (never above ``ASK_MAX_ROWS``); a day with
    no stored data yet is simply absent, not a null-filled row."""
    stmt = select(DailyMetric).where(DailyMetric.user_id == user_id)
    if date_from:
        stmt = stmt.where(DailyMetric.date >= date_from)
    if date_to:
        stmt = stmt.where(DailyMetric.date <= date_to)
    stmt = stmt.order_by(DailyMetric.date.desc()).limit(max(1, min(limit, ASK_MAX_ROWS)))
    rows = (await session.execute(stmt)).scalars().all()
    want = [f for f in (fields or sorted(ASK_DAILY_FIELDS)) if f in ASK_DAILY_FIELDS]
    if not want:
        want = sorted(ASK_DAILY_FIELDS)
    out = []
    for m in rows:
        ex = m.extra or {}
        row = {"date": m.date}
        for f in want:
            v = getattr(m, f) if f in ASK_DAILY_BASE_FIELDS else ex.get(f)
            if v is not None:
                row[f] = v
        out.append(row)
    out.reverse()  # oldest → newest
    return out


async def latest_daily_date(session: AsyncSession, user_id: int) -> Optional[str]:
    """Most recent date with a stored ``daily_metrics`` row for this user — a pure-DB,
    no-Garmin proxy for "how fresh is the data" (EP-09 ``/ask`` dedup-cache key: a coarse
    daily-slice signal, since the answer's actual DB reads only happen mid-loop)."""
    return (
        await session.execute(
            select(func.max(DailyMetric.date)).where(DailyMetric.user_id == user_id)
        )
    ).scalar_one_or_none()


async def query_training_plan(
    session: AsyncSession, user_id: int, *,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    limit: int = ASK_MAX_ROWS,
) -> dict:
    """EP-09 ``/ask`` tool: this user's active training plan (goal/target/summary) plus its
    dated sessions in a date range (both ends inclusive; omit either for an open range,
    oldest first). Without this, a question about "the program" itself (upcoming sessions,
    goal, target date) had no real data to answer from — only whatever a daily report
    happened to mention about today/tomorrow. Read-only, user-scoped, capped at ``limit``
    sessions (never above ``ASK_MAX_ROWS``). Returns ``{"plan": None}`` if there's no active
    plan (an archived one isn't visible here — that's a deliberate v1 scope, not a bug)."""
    plan = (
        await session.execute(
            select(TrainingPlan).where(
                TrainingPlan.user_id == user_id, TrainingPlan.status == "active"
            ).order_by(TrainingPlan.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if plan is None:
        return {"plan": None}
    stmt = select(PlannedWorkout).where(PlannedWorkout.plan_id == plan.id)
    if date_from:
        stmt = stmt.where(PlannedWorkout.date >= date_from)
    if date_to:
        stmt = stmt.where(PlannedWorkout.date <= date_to)
    stmt = stmt.order_by(PlannedWorkout.date).limit(max(1, min(limit, ASK_MAX_ROWS)))
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "plan": {
            "goal": plan.goal, "goal_label": plan.goal_label,
            "target_date": plan.target_date, "start_date": plan.start_date,
            "days_per_week": plan.days_per_week, "intensity": plan.intensity,
            "summary": plan.summary,
        },
        "sessions": [
            {"date": w.date, "week": w.week, "type": w.type, "dist_km": w.dist_km,
             "description": w.description, "status": w.status}
            for w in rows
        ],
    }


async def get_activity(session: AsyncSession, user_id: int, row_id: int):
    """One activity by its DB id, scoped to the user (None if missing / not theirs)."""
    return (
        await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.id == row_id, ActivityRecord.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def get_last_activity(session: AsyncSession, user_id: int):
    """This user's most recent activity (newest first), or None. Used by /checkin to
    target the run the runner most likely wants to rate."""
    return (
        await session.execute(
            select(ActivityRecord)
            .where(ActivityRecord.user_id == user_id,
                   ActivityRecord.is_hidden.is_(False))   # ST-17
            .order_by(ActivityRecord.date.desc(), ActivityRecord.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def set_subjective(
    session: AsyncSession, user_id: int, row_id: int,
    *, rpe: Optional[int] = None, pain: Optional[bool] = None,
    note: Optional[str] = None,
):
    """Merge a post-run check-in (EP-12) into ``ActivityRecord.subjective``, scoped to the
    user. Only the passed fields are written, so a later tap (RPE, then a pain note) adds
    to the same record; a repeated field overwrites. Returns the row, or None if not found
    / not theirs. Does not commit."""
    act = await get_activity(session, user_id, row_id)
    if act is None:
        return None
    data = dict(act.subjective or {})
    if rpe is not None:
        data["rpe"] = rpe
    if pain is not None:
        data["pain"] = pain
        if not pain:  # "все ок" clears any earlier niggle note
            data.pop("note", None)
    if note is not None:
        data["pain"] = True
        data["note"] = note
    act.subjective = data
    # JSON column reassignment needs an explicit flag for SQLAlchemy to persist it.
    flag_modified(act, "subjective")
    return act


async def set_activity_hidden(
    session: AsyncSession, user_id: int, row_id: int, hidden: bool
):
    """ST-17: hide (or un-hide) one activity, scoped to the user. Returns the row, or None if
    it isn't this user's. Does not commit.

    Hiding also cleans up the poisoned downstream state the activity had already leaked into:
    * any ``PersonalRecord`` rows tied to THIS activity (``activity_id``) are deleted — a
      false PB from a broken-GPS track shouldn't survive the hide (other categories, e.g. a
      week/VO2max record not tied to an activity, are re-seeded by ``backfill-records``);
    * any ``PlannedWorkout`` it was matched to is un-matched (``completed_activity_id`` and
      ``match_info`` cleared, status back to ``missed``/``planned`` by date) so a session no
      longer reads as done off a bogus activity, and the activity is freed for another match.
    Un-hiding just flips the flag back (records/matches are not restored — the automatic
    detectors re-derive them on the next tick)."""
    act = await get_activity(session, user_id, row_id)
    if act is None:
        return None
    act.is_hidden = hidden
    if hidden:
        await session.execute(
            delete(PersonalRecord).where(
                PersonalRecord.user_id == user_id,
                PersonalRecord.activity_id == row_id,
            )
        )
        matched = (
            await session.execute(
                select(PlannedWorkout).where(
                    PlannedWorkout.user_id == user_id,
                    PlannedWorkout.completed_activity_id == row_id,
                )
            )
        ).scalars().all()
        today_s = dt.date.today().isoformat()
        for w in matched:
            w.completed_activity_id = None
            w.match_info = None
            w.status = (WorkoutStatus.MISSED if (w.date or "") < today_s
                        else WorkoutStatus.PLANNED)
    return act


async def current_records(session: AsyncSession, user_id: int) -> List[PersonalRecord]:
    """The user's current best per category — the latest ``PersonalRecord`` row of each kind.
    For the ``/records`` command. Empty list when the user has no records yet."""
    rows = (
        await session.execute(
            select(PersonalRecord)
            .where(PersonalRecord.user_id == user_id)
            .order_by(PersonalRecord.id)
        )
    ).scalars().all()
    latest: Dict[str, PersonalRecord] = {}
    for r in rows:
        latest[r.kind] = r   # ordered by id asc → last (newest) wins
    return list(latest.values())


async def recent_records(
    session: AsyncSession, user_id: int, days: int = 7
) -> List[PersonalRecord]:
    """Records achieved in the last ``days`` days (newest first) — fed into the morning
    report / weekly digest so a fresh PR gets a mention (EP-14)."""
    cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    return list(
        (
            await session.execute(
                select(PersonalRecord)
                .where(
                    PersonalRecord.user_id == user_id,
                    PersonalRecord.date >= cutoff,
                )
                .order_by(PersonalRecord.date.desc(), PersonalRecord.id.desc())
            )
        ).scalars().all()
    )


FITNESS_TREND_KEYS = ("race_5k_s", "race_10k_s", "race_half_s", "race_marathon_s", "vo2max")


async def read_fitness_history(
    session: AsyncSession, user_id: int, days: int = 120
) -> List[dict]:
    """Daily fitness-trend readings (race-time predictions + VO2max) over the last
    ``days`` days, oldest first — one row per day that carries at least one of
    ``FITNESS_TREND_KEYS``. Unlike :func:`get_recent_extra` (which coalesces to a single
    latest snapshot), this keeps the raw per-day series so NF-10's goal projection can
    fit a trend across weeks."""
    cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    rows = (
        await session.execute(
            select(DailyMetric.date, DailyMetric.extra).where(
                DailyMetric.user_id == user_id,
                DailyMetric.extra.is_not(None),
                DailyMetric.date >= cutoff,
            ).order_by(DailyMetric.date)
        )
    ).all()
    out = []
    for date, ex in rows:
        if not isinstance(ex, dict):
            continue
        row = {k: ex[k] for k in FITNESS_TREND_KEYS if ex.get(k) is not None}
        if row:
            row["date"] = date
            out.append(row)
    return out


async def get_recent_extra(session: AsyncSession, user_id: int, days: int = 21) -> dict:
    """Merge the last ``days`` days of ``extra`` into one dict — the most recent non-null
    value wins per key. Garmin metrics refresh at different cadences (race predictions &
    VO2max ~weekly, Training Readiness daily, HRV/RHR baselines slowly), so any single
    day rarely carries them all; coalescing gives plan generation the freshest value of
    each (race predictions, VO2max, endurance, ACWR/load, baselines, …)."""
    cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    rows = (
        await session.execute(
            select(DailyMetric.extra)
            .where(
                DailyMetric.user_id == user_id,
                DailyMetric.extra.is_not(None),
                DailyMetric.date >= cutoff,
            )
            .order_by(DailyMetric.date.desc())  # newest first → first non-null wins
        )
    ).scalars().all()
    merged: dict = {}
    for ex in rows:
        if not isinstance(ex, dict):
            continue
        for k, v in ex.items():
            if v is not None and k not in merged:
                merged[k] = v
    return merged


async def read_history(session: AsyncSession, user_id: int, days: int = 30) -> List[dict]:
    """Recovery trends over the last ``days`` days for this user, oldest first."""
    cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    rows = (
        await session.execute(
            select(DailyMetric)
            .where(DailyMetric.user_id == user_id, DailyMetric.date >= cutoff)
            .order_by(DailyMetric.date)
        )
    ).scalars().all()
    return [
        {
            "date": m.date,
            "sleep_score": m.sleep_score,
            "sleep_h": m.sleep_h,
            "hrv_avg": m.hrv_avg,
            "hrv_status": m.hrv_status,
            "stress_avg": m.stress_avg,
            "stress_max": m.stress_max,
            "bb_charged": m.bb_charged,
            "bb_drained": m.bb_drained,
            # resting HR drift is a key fatigue marker; it lives in extra, not a column
            "resting_hr": (m.extra or {}).get("resting_hr"),
            # NF-16: the evening sleep-nudge wants Garmin's own sleep_need_h (extra) vs
            # actual sleep_h — cheaper to carry the whole dict than add a second column.
            "extra": m.extra,
        }
        for m in rows
    ]


async def typical_run_pace(
    session: AsyncSession, user_id: int, days: int = 42
) -> Optional[float]:
    """Median run pace (min/km) over the last ``days`` days for this user, or None when
    there aren't enough runs. The grounded anchor for time estimates on plan steps that
    only carry an HR zone (see routers.plan._est_minutes). Same sanity band as records.py."""
    cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    rows = (
        await session.execute(
            select(ActivityRecord.dist_km, ActivityRecord.dur_min).where(
                ActivityRecord.user_id == user_id,
                ActivityRecord.type.like("%run%"),
                ActivityRecord.date.is_not(None),
                ActivityRecord.date >= cutoff,
                ActivityRecord.is_hidden.is_(False),   # ST-17
            )
        )
    ).all()
    paces = sorted(
        d / km for km, d in rows
        if km and d and km > 0 and 2.5 <= d / km <= 12.0
    )
    if not paces:
        return None
    n = len(paces)
    return paces[n // 2] if n % 2 else (paces[n // 2 - 1] + paces[n // 2]) / 2


async def count_daily_metrics(session: AsyncSession, user_id: int) -> int:
    """Total number of stored daily rows for this user — the calibration gate for the
    injury radar (NF-04): no warnings until there's enough history to trust the signals."""
    from sqlalchemy import func

    return int(
        (await session.execute(
            select(func.count()).select_from(DailyMetric).where(
                DailyMetric.user_id == user_id
            )
        )).scalar_one()
    )


async def read_load_history(
    session: AsyncSession, user_id: int, days: int = 14
) -> List[dict]:
    """The injury radar's daily inputs (NF-04) over the last ``days`` days, oldest first:
    ``{date, hrv_avg, resting_hr, acwr_pct, hrv_baseline_low}``. ``hrv_avg`` is a column; the
    rest live in ``extra``."""
    cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    rows = (
        await session.execute(
            select(DailyMetric)
            .where(DailyMetric.user_id == user_id, DailyMetric.date >= cutoff)
            .order_by(DailyMetric.date)
        )
    ).scalars().all()
    out = []
    for m in rows:
        ex = m.extra or {}
        out.append({
            "date": m.date,
            "hrv_avg": m.hrv_avg,
            "resting_hr": ex.get("resting_hr"),
            "acwr_pct": ex.get("acwr_pct"),
            "hrv_baseline_low": ex.get("hrv_baseline_low"),
        })
    return out


async def recent_subjective_runs(
    session: AsyncSession, user_id: int, days: int = 14
) -> List[dict]:
    """Runs with a post-run check-in (EP-12) in the last ``days`` days, oldest first, for the
    injury radar (NF-04): ``{date, pace, rpe, pain, note}`` — ``pace`` = min/km (or None). Only
    rows carrying ``subjective`` are returned (silence isn't a signal)."""
    cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    rows = (
        await session.execute(
            select(ActivityRecord)
            .where(
                ActivityRecord.user_id == user_id,
                ActivityRecord.type.like("%run%"),
                ActivityRecord.date.is_not(None),
                ActivityRecord.date >= cutoff,
                ActivityRecord.subjective.is_not(None),
                ActivityRecord.is_hidden.is_(False),   # ST-17
            )
            .order_by(ActivityRecord.date)
        )
    ).scalars().all()
    out = []
    for a in rows:
        subj = a.subjective or {}
        pace = (a.dur_min / a.dist_km) if (a.dur_min and a.dist_km) else None
        out.append({
            "date": a.date,
            "pace": pace,
            "rpe": subj.get("rpe"),
            "pain": subj.get("pain"),
            "note": subj.get("note"),
        })
    return out


# ---------- WRITE ----------

def _dump_steps(steps) -> Optional[list]:
    """Serialize a workout's structured steps (PlanStep models or plain dicts) to a
    JSON-storable list, dropping null fields. None/empty → None (stored as JSON null)."""
    if not steps:
        return None
    out = [s.model_dump(exclude_none=True) if hasattr(s, "model_dump") else s
           for s in steps]
    return out or None


async def upsert_daily(
    session: AsyncSession, user_id: int, s: DailySummary, *, merge: bool = False
) -> None:
    """Upsert one day's metrics. The default (``merge=False``) fully overwrites an existing
    row — the volatile "today"/normal path. With ``merge=True`` (ST-18's incomplete-day
    refetch) it is **null-safe fill-only**: a null in the fresh fetch never overwrites a
    stored value, and ``extra`` is merged key-by-key (a stored non-null key wins). So a day
    saved with sleep-but-no-HRV gets HRV filled on the next tick without clobbering what was
    already good."""
    existing = (
        await session.execute(
            select(DailyMetric).where(
                DailyMetric.user_id == user_id, DailyMetric.date == s.date
            )
        )
    ).scalar_one_or_none()
    fields = {k: getattr(s, k) for k in _DAILY_FIELDS}
    if existing:
        if merge:
            for k, v in fields.items():
                if k == "extra":
                    cur = dict(existing.extra or {})
                    for ek, ev in (v or {}).items():
                        if ev is not None and cur.get(ek) is None:
                            cur[ek] = ev
                    if cur != (existing.extra or {}):
                        existing.extra = cur
                        flag_modified(existing, "extra")
                elif v is not None and getattr(existing, k) is None:
                    setattr(existing, k, v)
        else:
            for k, v in fields.items():
                setattr(existing, k, v)
    else:
        session.add(DailyMetric(user_id=user_id, date=s.date, **fields))


async def upsert_activity(
    session: AsyncSession, user_id: int, activity_id: Optional[int], row: dict
) -> Optional[ActivityRecord]:
    """Upsert one activity. Returns the ORM row if it was newly inserted (so the
    caller can detect "just synced" activities), or None for an update to an
    existing row / a missing id."""
    if not activity_id:
        return None
    existing = (
        await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.user_id == user_id,
                ActivityRecord.activity_id == int(activity_id),
            )
        )
    ).scalar_one_or_none()
    fields = {
        "date": row.get("date") or None,
        "type": row.get("type"),
        "dur_min": row.get("dur_min"),
        "dist_km": row.get("dist_km"),
        "avg_hr": row.get("avg_hr"),
        "max_hr": row.get("max_hr"),
        "load": row.get("load"),
        "exercises": row.get("exercises"),
        "series": row.get("series"),
    }
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
        return None
    rec = ActivityRecord(user_id=user_id, activity_id=int(activity_id), **fields)
    session.add(rec)
    return rec


async def persist_payload(
    session: AsyncSession, user_id: int, payload: Payload, act_pairs,
    merge_dates: Optional[set] = None,
) -> List[ActivityRecord]:
    """Upsert everything the payload carries for this user (does not commit).
    Returns newly inserted activity rows (never updates) — the caller uses this to
    trigger auto-analysis of freshly synced activities. ``merge_dates`` (ST-18) is the set
    of ISO dates whose upsert should be null-safe fill-only (a re-fetched incomplete past
    day) instead of a full overwrite — see :func:`upsert_daily`."""
    merge_dates = merge_dates or set()
    for d in payload.daily:
        if d.has_data:
            await upsert_daily(session, user_id, d, merge=d.date in merge_dates)
    new_activities: List[ActivityRecord] = []
    for activity_id, row in act_pairs:
        rec = await upsert_activity(session, user_id, activity_id, row)
        if rec is not None:
            new_activities.append(rec)
    if new_activities:
        await session.flush()  # assign ids before the caller reads them
    return new_activities


async def get_last_report(session: AsyncSession, user_id: int):
    """This user's most recent *daily* report from a prior day, as (text, date_iso),
    for day-over-day continuity context.

    Excludes today's reports (so repeated same-day /report presses keep a stable
    dedup-cache key instead of each picking up the previous press as "previous"),
    and excludes /deep and /ask — only the daily-status thread (report/morning)."""
    today_start = dt.datetime.now(dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    row = (
        await session.execute(
            select(ReportLog.report_text, ReportLog.created_at)
            .where(
                ReportLog.user_id == user_id,
                ReportLog.report_text.is_not(None),
                ReportLog.ok.is_(True),
                ReportLog.kind.in_(("report", "morning")),
                ReportLog.created_at < today_start,
            )
            .order_by(ReportLog.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    text, created = row
    return text, (created.date().isoformat() if created else None)


async def get_last_report_of_kind(session: AsyncSession, user_id: int, kind: str):
    """This user's most recent successful report of a given ``kind``, as
    (text, date_iso), or None. A generalisation of :func:`get_last_report` (pinned to
    report/morning) — EP-05 uses this to show the last generated race pack on ``/plan``
    without regenerating it."""
    row = (
        await session.execute(
            select(ReportLog.report_text, ReportLog.created_at)
            .where(
                ReportLog.user_id == user_id,
                ReportLog.report_text.is_not(None),
                ReportLog.ok.is_(True),
                ReportLog.kind == kind,
            )
            .order_by(ReportLog.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    text, created = row
    return text, (created.date().isoformat() if created else None)


async def get_recent_reports(
    session: AsyncSession, user_id: int, n: int = 3
) -> List[dict]:
    """This user's last ``n`` delivered daily reports (newest first) as
    [{date, text}, ...], for the /ask follow-up context. Only kind="report" — /deep
    and /ask answers are excluded so the context stays the daily-report thread."""
    rows = (
        await session.execute(
            select(ReportLog.report_text, ReportLog.created_at)
            .where(
                ReportLog.user_id == user_id,
                ReportLog.report_text.is_not(None),
                ReportLog.ok.is_(True),
                ReportLog.kind == "report",
            )
            .order_by(ReportLog.created_at.desc())
            .limit(n)
        )
    ).all()
    return [
        {"date": created.date().isoformat() if created else None, "text": text}
        for text, created in rows
    ]


async def get_recent_asks(
    session: AsyncSession, user_id: int, minutes: int = 5
) -> List[dict]:
    """This user's successful /ask exchanges from the last ``minutes`` minutes, oldest
    first, as [{question, answer}, ...] — the short conversation thread so a follow-up
    /ask can build on what was just asked."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)
    rows = (
        await session.execute(
            select(ReportLog.question, ReportLog.report_text)
            .where(
                ReportLog.user_id == user_id,
                ReportLog.kind == "ask",
                ReportLog.ok.is_(True),
                ReportLog.report_text.is_not(None),
                ReportLog.created_at >= cutoff,
            )
            .order_by(ReportLog.created_at.asc())
        )
    ).all()
    return [{"question": q, "answer": a} for q, a in rows]


# EP-11: which ReportLog kinds render as a turn in the web chat transcript — the same
# free-text engines the bot's /ask and /plan <text>/sick already log to. Deliberately not
# "report"/"deep"/etc: those are their own dedicated surfaces, not chat turns.
CHAT_KINDS = ("ask", "plan_edit", "sick")


async def get_chat_history(
    session: AsyncSession, user_id: int, n: int = 30, offset: int = 0
) -> List[dict]:
    """Chat-shaped exchanges **newest first**, as [{kind, question, answer, ok,
    created_at}, ...] for the web chat page — a window of ``n`` starting ``offset`` rows
    back from the newest (for "load more" pagination). ``ReportLog`` is user-scoped, not
    chat-scoped, so this is the exact same thread the bot's /ask and /plan already write
    to — a question asked in Telegram shows up here too. A failed call (``ok=False``)
    still renders as a turn, with ``answer`` from ``error`` instead of ``report_text``,
    so a chat reload never silently drops a turn the user saw fail live."""
    rows = (
        await session.execute(
            select(ReportLog)
            .where(ReportLog.user_id == user_id, ReportLog.kind.in_(CHAT_KINDS))
            .order_by(ReportLog.created_at.desc())
            .offset(max(0, offset))
            .limit(n)
        )
    ).scalars().all()
    return [
        {
            "kind": r.kind, "question": r.question,
            "answer": r.report_text if r.ok else (r.error or "Помилка."),
            "ok": r.ok,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


async def month_cost(session: AsyncSession, user_id: int) -> float:
    """Sum of ``cost_usd`` for this user's Claude calls in the current calendar month
    (UTC) — the dashboard's "AI cost this month" tile (EP-04); reused later by EP-06's
    quotas. Includes failed/errored calls (their cost is usually 0 anyway, but a
    partial call that still burned tokens should count)."""
    month_start = dt.datetime.now(dt.timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    total = (
        await session.execute(
            select(func.sum(ReportLog.cost_usd)).where(
                ReportLog.user_id == user_id, ReportLog.created_at >= month_start,
            )
        )
    ).scalar_one()
    return round(total or 0.0, 4)


async def costs_for_month(
    session: AsyncSession, user_id: int, start: dt.datetime, end: dt.datetime
) -> dict:
    """ST-12: cost aggregation over ``[start, end)`` — total $, a per-``kind`` breakdown
    ({cost, calls}), total/cache-hit call counts, and the 3 priciest individual calls.
    Bounds are caller-supplied UTC datetimes (bot/handlers computes them in the user's own
    timezone via ST-14's ``user_tz`` — a calendar month means THEIR month, not UTC's) so
    this stays a dumb range query. A ``cached=True`` row carries ~$0 cost — counted towards
    ``calls`` (visible cache effectiveness) but excluded from ``top3`` (nothing to show)."""
    rows = (
        await session.execute(
            select(ReportLog).where(
                ReportLog.user_id == user_id,
                ReportLog.created_at >= start, ReportLog.created_at < end,
            )
        )
    ).scalars().all()
    by_kind: Dict[str, dict] = {}
    for r in rows:
        b = by_kind.setdefault(r.kind, {"cost": 0.0, "calls": 0})
        b["cost"] += r.cost_usd or 0.0
        b["calls"] += 1
    for b in by_kind.values():
        b["cost"] = round(b["cost"], 4)
    top = sorted(
        (r for r in rows if (r.cost_usd or 0.0) > 0), key=lambda r: r.cost_usd, reverse=True
    )[:3]
    return {
        "total_usd": round(sum(r.cost_usd or 0.0 for r in rows), 4),
        "calls": len(rows),
        "cached": sum(1 for r in rows if r.cached),
        "by_kind": by_kind,
        "top3": [{"kind": r.kind, "date": r.created_at.date().isoformat(),
                  "cost": round(r.cost_usd, 4)} for r in top],
    }


async def log_report(
    session: AsyncSession,
    *,
    user_id: Optional[int] = None,
    kind: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    ok: bool = True,
    cached: bool = False,
    error: Optional[str] = None,
    question: Optional[str] = None,
    report_text: Optional[str] = None,
    tool_rounds: Optional[int] = None,
) -> None:
    session.add(ReportLog(
        user_id=user_id, kind=kind, model=model, input_tokens=input_tokens,
        output_tokens=output_tokens, cost_usd=cost_usd, ok=ok, cached=cached,
        error=error, question=question, report_text=report_text,
        tool_rounds=tool_rounds,
    ))
    await session.commit()
