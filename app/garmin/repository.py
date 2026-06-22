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

from app.db.models import ActivityRecord, BotState, DailyMetric, ReportLog
from app.garmin.schemas import DailySummary, Payload

_DAILY_FIELDS = (
    "sleep_score", "sleep_h", "deep_h", "rem_h", "light_h", "awake_h",
    "hrv_avg", "hrv_status", "stress_avg", "stress_max", "bb_charged", "bb_drained",
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
    session: AsyncSession, dates: List[str]
) -> Dict[str, DailySummary]:
    """Past-day metrics already stored, keyed by ISO date (the day-level cache)."""
    if not dates:
        return {}
    rows = (
        await session.execute(
            select(DailyMetric).where(DailyMetric.date.in_(dates))
        )
    ).scalars().all()
    return {m.date: _to_summary(m) for m in rows}


async def read_history(session: AsyncSession, days: int = 30) -> List[dict]:
    """Recovery trends over the last ``days`` days, oldest first."""
    cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    rows = (
        await session.execute(
            select(DailyMetric)
            .where(DailyMetric.date >= cutoff)
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

async def upsert_daily(session: AsyncSession, s: DailySummary) -> None:
    existing = (
        await session.execute(select(DailyMetric).where(DailyMetric.date == s.date))
    ).scalar_one_or_none()
    fields = {k: getattr(s, k) for k in _DAILY_FIELDS}
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
    else:
        session.add(DailyMetric(date=s.date, **fields))


async def upsert_activity(
    session: AsyncSession, activity_id: Optional[int], row: dict
) -> None:
    if not activity_id:
        return
    existing = (
        await session.execute(
            select(ActivityRecord).where(ActivityRecord.activity_id == int(activity_id))
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
    }
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
    else:
        session.add(ActivityRecord(activity_id=int(activity_id), **fields))


async def persist_payload(session: AsyncSession, payload: Payload, act_pairs) -> None:
    """Upsert everything the payload carries (does not commit)."""
    for d in payload.daily:
        if d.has_data:
            await upsert_daily(session, d)
    for activity_id, row in act_pairs:
        await upsert_activity(session, activity_id, row)


async def log_report(
    session: AsyncSession,
    *,
    kind: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    ok: bool = True,
    cached: bool = False,
    error: Optional[str] = None,
    report_text: Optional[str] = None,
) -> None:
    session.add(ReportLog(
        kind=kind, model=model, input_tokens=input_tokens,
        output_tokens=output_tokens, cost_usd=cost_usd, ok=ok, cached=cached,
        error=error, report_text=report_text,
    ))
    await session.commit()


# ---------- BOT STATE ----------

async def get_state(session: AsyncSession, key: str) -> Optional[str]:
    m = await session.get(BotState, key)
    return m.value if m else None


async def set_state(session: AsyncSession, key: str, value: str) -> None:
    m = await session.get(BotState, key)
    if m:
        m.value = value
    else:
        session.add(BotState(key=key, value=value))
    await session.commit()
