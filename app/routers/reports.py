"""On-demand report endpoints. Thin: build payload → analyze → shape response.

Heavy work (Garmin fetch, Claude call) happens inside the services, which offload
blocking calls to a threadpool. Each request runs in the logged-in user's runtime
context (their Garmin provider + Claude key)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis import delivery
from app.analysis.service import AnalystError, run_analysis
from app.core.auth import current_user
from app.db.models import User
from app.dependencies import get_session
from app.garmin import service
from app.garmin.runtime import user_runtime

router = APIRouter(tags=["reports"])

_REPORT_Q = "Оціни відновлення і дай пораду до наступної запланованої пробіжки."
_DEEP_Q = "Глибокий розбір сну, HRV і навантаження за два тижні."


@router.get("/report.json")
async def report_json(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    async with user_runtime(session, user) as creds:
        payload, _ = await service.build_payload_cached(
            session, user.id, days=7, activity_limit=20
        )
        try:
            result = await delivery.build_report(
                session, user, payload, question=_REPORT_Q,
                kind="report", api_key=creds.anthropic_key,
            )
        except AnalystError as e:
            raise HTTPException(status_code=502, detail=str(e))
    return {
        "synced_today": result.synced_today,
        "last_data_date": result.last_data_date,
        "note": None if result.synced_today else delivery.STALE_NOTE,
        "report": result.text,
    }


@router.get("/deep")
async def deep(
    q: str = Query(default="", description="Optional analysis question"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    async with user_runtime(session, user) as creds:
        payload, _ = await service.build_payload_cached(
            session, user.id, days=14, activity_limit=30
        )
        try:
            text = await run_analysis(
                session, payload, user_id=user.id, question=q or _DEEP_Q,
                deep=True, kind="deep", api_key=creds.anthropic_key,
            )
        except AnalystError as e:
            raise HTTPException(status_code=502, detail=str(e))
    return {
        "synced_today": payload.synced_today,
        "last_data_date": payload.last_data_date,
        "question": q or _DEEP_Q,
        "report": text,
    }
