"""Liveness and status endpoints (unauthenticated — safe for uptime monitors)."""
import logging

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import DailyMetric, ReportLog
from app.dependencies import get_session
from app.garmin import service
from app.garmin.repository import get_state

logger = logging.getLogger("web")

router = APIRouter(tags=["health"])

MORNING_STATE_KEY = "morning_sent_date"


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/status")
async def status(session: AsyncSession = Depends(get_session)) -> dict:
    """System status: Garmin auth, DB stats, last morning report, total cost.
    Reports metadata only — no actual health metrics — so it needs no auth."""
    garmin = "ok"
    try:
        await run_in_threadpool(service.login)
    except Exception as e:  # noqa: BLE001 — surface any login failure as status
        garmin = f"error: {type(e).__name__}: {e}"

    history_days = (await session.execute(select(func.count(DailyMetric.id)))).scalar_one()
    last_metric = (await session.execute(select(func.max(DailyMetric.date)))).scalar_one()
    reports_total = (await session.execute(select(func.count(ReportLog.id)))).scalar_one()
    cost_total = (
        await session.execute(select(func.coalesce(func.sum(ReportLog.cost_usd), 0.0)))
    ).scalar_one()
    last_morning = await get_state(session, MORNING_STATE_KEY)

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
