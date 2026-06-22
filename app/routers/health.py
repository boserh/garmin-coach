"""Liveness (public) and per-user status (login required)."""
import logging

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.config import settings
from app.db.models import DailyMetric, ReportLog, User
from app.dependencies import get_session
from app.garmin import service
from app.garmin.repository import get_state
from app.garmin.runtime import user_runtime

logger = logging.getLogger("web")

router = APIRouter(tags=["health"])

MORNING_STATE_KEY = "morning_sent_date"


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/status")
async def status(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The logged-in user's status: Garmin auth, their DB stats, last morning
    report, total cost."""
    garmin = "ok"
    try:
        async with user_runtime(session, user):
            await run_in_threadpool(service.login)
    except Exception as e:  # noqa: BLE001 — surface any login failure as status
        garmin = f"error: {type(e).__name__}: {e}"

    history_days = (await session.execute(
        select(func.count(DailyMetric.id)).where(DailyMetric.user_id == user.id)
    )).scalar_one()
    last_metric = (await session.execute(
        select(func.max(DailyMetric.date)).where(DailyMetric.user_id == user.id)
    )).scalar_one()
    reports_total = (await session.execute(
        select(func.count(ReportLog.id)).where(ReportLog.user_id == user.id)
    )).scalar_one()
    cost_total = (await session.execute(
        select(func.coalesce(func.sum(ReportLog.cost_usd), 0.0))
        .where(ReportLog.user_id == user.id)
    )).scalar_one()
    last_morning = await get_state(session, user.id, MORNING_STATE_KEY)

    return {
        "status": "ok",
        "provider": settings.GARMIN_PROVIDER,
        "garmin_login": garmin,
        "database": "ok",
        "history_days": history_days,
        "last_metric_date": last_metric,
        "last_morning_report": last_morning,
        "reports_total": reports_total,
        "cost_usd_total": round(cost_total, 4),
    }
