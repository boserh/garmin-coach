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

    # OPS-05: recent Garmin API failures (from the drained error buffer in bot_state) so a
    # user can tell "watch didn't sync" from "API is degrading" without grepping the Pi log.
    errors = service.summarize_garmin_errors(
        await get_state(session, user.id, service.GARMIN_ERRORS_KEY)
    )

    # ST-18: how many of the last 30 stored days are incomplete (missing a key recovery
    # field this user normally has) — the diagnostic for holes in baselines/trends.
    from app import completeness
    from app.garmin import repository
    history30 = await repository.read_history(session, user.id, days=30)
    expected = completeness.expected_fields(history30)
    incomplete_days_30d = sum(
        1 for r in history30 if completeness.daily_completeness(r, expected)
    )

    # OPS-04: the most recent morning-tick run's status/reason (from the job-run log).
    from app.db import job_runs
    last_morning_job = await job_runs.last_job_status(session, user.id, "MORNING")
    last_morning_status = None
    if last_morning_job is not None:
        last_morning_status = {
            "status": last_morning_job.status,
            "detail": last_morning_job.detail,
            "at": (last_morning_job.finished_at or last_morning_job.started_at).isoformat()
            if (last_morning_job.finished_at or last_morning_job.started_at) else None,
        }

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
        "garmin_errors_24h": errors["count_24h"],
        "garmin_errors_breakdown": errors["counts_24h"],
        "garmin_last_error": errors["last"],
        "incomplete_days_30d": incomplete_days_30d,
        "last_morning_job": last_morning_status,
    }
