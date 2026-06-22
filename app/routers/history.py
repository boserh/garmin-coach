"""Trend history read from the DB (HRV / sleep / stress / body battery)."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.db.models import User
from app.dependencies import get_session
from app.garmin import repository

router = APIRouter(tags=["history"])


@router.get("/history")
async def history(
    days: int = Query(default=30, ge=1, le=365),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    trend = await repository.read_history(session, user.id, days=days)
    return {"days": days, "count": len(trend), "trend": trend}
