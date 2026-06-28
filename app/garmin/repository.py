"""Persistence: map the typed payload to ORM rows and read history back.

Keeps SQLAlchemy models and Pydantic schemas separate — the mapping between them
lives here. Upserts are idempotent on the natural key (``date`` / ``activity_id``)
and portable across SQLite and Postgres (select-then-update, no dialect-specific
ON CONFLICT).
"""
import datetime as dt
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ActivityRecord,
    BotState,
    DailyMetric,
    PlannedWorkout,
    ReportLog,
    TrainingPlan,
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
            .where(ActivityRecord.user_id == user_id)
            .order_by(ActivityRecord.date.desc(), ActivityRecord.id.desc())
            .limit(n)
        )
    ).scalars().all()
    return [
        {"id": a.id, "date": a.date, "type": a.type, "dist_km": a.dist_km,
         "dur_min": a.dur_min, "avg_hr": a.avg_hr}
        for a in rows
    ]


async def get_activity(session: AsyncSession, user_id: int, row_id: int):
    """One activity by its DB id, scoped to the user (None if missing / not theirs)."""
    return (
        await session.execute(
            select(ActivityRecord).where(
                ActivityRecord.id == row_id, ActivityRecord.user_id == user_id
            )
        )
    ).scalar_one_or_none()


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
        }
        for m in rows
    ]


# ---------- WRITE ----------

async def upsert_daily(session: AsyncSession, user_id: int, s: DailySummary) -> None:
    existing = (
        await session.execute(
            select(DailyMetric).where(
                DailyMetric.user_id == user_id, DailyMetric.date == s.date
            )
        )
    ).scalar_one_or_none()
    fields = {k: getattr(s, k) for k in _DAILY_FIELDS}
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
    else:
        session.add(DailyMetric(user_id=user_id, date=s.date, **fields))


async def upsert_activity(
    session: AsyncSession, user_id: int, activity_id: Optional[int], row: dict
) -> None:
    if not activity_id:
        return
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
    else:
        session.add(ActivityRecord(user_id=user_id, activity_id=int(activity_id), **fields))


async def persist_payload(
    session: AsyncSession, user_id: int, payload: Payload, act_pairs
) -> None:
    """Upsert everything the payload carries for this user (does not commit)."""
    for d in payload.daily:
        if d.has_data:
            await upsert_daily(session, user_id, d)
    for activity_id, row in act_pairs:
        await upsert_activity(session, user_id, activity_id, row)


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
) -> None:
    session.add(ReportLog(
        user_id=user_id, kind=kind, model=model, input_tokens=input_tokens,
        output_tokens=output_tokens, cost_usd=cost_usd, ok=ok, cached=cached,
        error=error, question=question, report_text=report_text,
    ))
    await session.commit()


# ---------- BOT STATE ----------

async def get_state(session: AsyncSession, user_id: int, key: str) -> Optional[str]:
    m = await session.get(BotState, (user_id, key))
    return m.value if m else None


async def set_state(session: AsyncSession, user_id: int, key: str, value: str) -> None:
    m = await session.get(BotState, (user_id, key))
    if m:
        m.value = value
    else:
        session.add(BotState(user_id=user_id, key=key, value=value))
    await session.commit()


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
            type=w.type, dist_km=w.dist_km, description=w.description, status="planned",
        ))
    await session.commit()
    return plan


async def archive_plan(session: AsyncSession, plan: TrainingPlan) -> None:
    plan.status = "archived"
    await session.commit()


async def _workout_on(session: AsyncSession, plan_id: int, date: str):
    return (
        await session.execute(
            select(PlannedWorkout)
            .where(PlannedWorkout.plan_id == plan_id, PlannedWorkout.date == date)
            .order_by(PlannedWorkout.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def apply_plan_ops(session: AsyncSession, plan: TrainingPlan, ops: list) -> int:
    """Apply edit operations (``PlanOp``-like objects) to a plan's workouts. Returns the
    number applied. ``move``/``modify``/``skip`` target the workout on ``op.date``."""
    applied = 0
    for op in ops:
        if op.action == "add":
            session.add(PlannedWorkout(
                plan_id=plan.id, user_id=plan.user_id, date=op.date, week=op.week,
                type=op.type or "easy", dist_km=op.dist_km,
                description=op.description or "", status="planned",
            ))
            applied += 1
            continue
        w = await _workout_on(session, plan.id, op.date)
        if w is None:
            continue
        if op.action == "skip":
            w.status = "skipped"
            applied += 1
        elif op.action == "move" and op.to_date:
            w.date = op.to_date
            applied += 1
        elif op.action == "modify":
            if op.type is not None:
                w.type = op.type
            if op.dist_km is not None:
                w.dist_km = op.dist_km
            if op.description is not None:
                w.description = op.description
            applied += 1
    await session.commit()
    return applied
