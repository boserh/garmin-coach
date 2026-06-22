"""Minimal server-rendered UI to browse the database tables.

Whitelisted models only (no arbitrary SQL). Token-gated like the other data
endpoints; the token can be passed as ``?token=`` so plain browser links work.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActivityRecord, BotState, DailyMetric, ReportLog
from app.dependencies import get_session, verify_token

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# name → ORM model (whitelist; the path param is matched against these keys only)
TABLES = {
    "daily_metrics": DailyMetric,
    "activities": ActivityRecord,
    "report_logs": ReportLog,
    "bot_state": BotState,
}

router = APIRouter(tags=["ui"], dependencies=[Depends(verify_token)])


async def _count(session: AsyncSession, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


@router.get("/ui", response_class=HTMLResponse)
async def ui_index(request: Request, session: AsyncSession = Depends(get_session)):
    counts = {name: await _count(session, model) for name, model in TABLES.items()}
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "counts": counts, "token": request.query_params.get("token", "")},
    )


@router.get("/ui/{table}", response_class=HTMLResponse)
async def ui_table(
    table: str,
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    model = TABLES.get(table)
    if model is None:
        raise HTTPException(status_code=404, detail="Unknown table")

    cols = [c.name for c in model.__table__.columns]
    pk = list(model.__table__.primary_key.columns)[0]
    result = await session.execute(
        select(model).order_by(pk.desc()).limit(limit).offset(offset)
    )
    rows = [[getattr(r, c) for c in cols] for r in result.scalars().all()]
    total = await _count(session, model)

    return templates.TemplateResponse(
        "table.html",
        {
            "request": request, "table": table, "cols": cols, "rows": rows,
            "limit": limit, "offset": offset, "total": total,
            "tables": list(TABLES), "token": request.query_params.get("token", ""),
        },
    )
