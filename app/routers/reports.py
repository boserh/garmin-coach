"""On-demand report endpoints. Thin: build payload → analyze → shape response.

Heavy work (Garmin fetch, Claude call) happens inside the services, which offload
blocking calls to a threadpool. Protected by the shared-secret token dependency.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.service import AnalystError, run_analysis
from app.dependencies import get_session, verify_token
from app.garmin import service

router = APIRouter(tags=["reports"], dependencies=[Depends(verify_token)])

_REPORT_Q = "Оціни відновлення і дай пораду до наступної запланованої пробіжки."
_DEEP_Q = "Глибокий розбір сну, HRV і навантаження за два тижні."
_STALE_NOTE = "⚠️ Дані за сьогодні ще не синканулись, аналіз за останній доступний день."


@router.get("/report.json")
async def report_json(session: AsyncSession = Depends(get_session)) -> dict:
    payload = await service.build_payload_cached(session, days=7, activity_limit=20)
    try:
        text = await run_analysis(session, payload, question=_REPORT_Q, kind="report")
    except AnalystError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {
        "synced_today": payload.synced_today,
        "last_data_date": payload.last_data_date,
        "note": None if payload.synced_today else _STALE_NOTE,
        "report": text,
    }


@router.get("/deep")
async def deep(
    q: str = Query(default="", description="Optional analysis question"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    payload = await service.build_payload_cached(session, days=14, activity_limit=30)
    try:
        text = await run_analysis(
            session, payload, question=q or _DEEP_Q, deep=True, kind="deep"
        )
    except AnalystError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {
        "synced_today": payload.synced_today,
        "last_data_date": payload.last_data_date,
        "question": q or _DEEP_Q,
        "report": text,
    }
