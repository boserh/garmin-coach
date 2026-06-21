"""Trend history read from the DB (HRV / sleep / stress / body battery)."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_session, verify_token
from app.garmin import repository

router = APIRouter(tags=["history"], dependencies=[Depends(verify_token)])


@router.get("/history")
async def history(
    days: int = Query(default=30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
) -> dict:
    trend = await repository.read_history(session, days=days)
    return {"days": days, "count": len(trend), "trend": trend}
