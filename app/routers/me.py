"""Per-user data view — a logged-in user browses their own metrics, activities and
reports (scoped to their user_id). Mirrors the admin /ui browser but never spans
other users, and excludes the users / bot_state tables."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.db.models import ActivityRecord, DailyMetric, ReportLog, User
from app.dependencies import get_session
from app.routers.admin import INDEX_COLS, _daily_charts, _run_charts

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Only the user's own data tables (all carry user_id).
TABLES = {
    "daily_metrics": DailyMetric,
    "activities": ActivityRecord,
    "report_logs": ReportLog,
}

router = APIRouter(tags=["me"])


async def _count(session: AsyncSession, model, user_id: int) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(model).where(model.user_id == user_id)
        )
    ).scalar_one()


@router.get("/me", response_class=HTMLResponse)
async def me_index(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    counts = {name: await _count(session, model, user.id) for name, model in TABLES.items()}
    return templates.TemplateResponse(
        request, "index.html",
        {"counts": counts, "user": user,
         "base": "/me", "title": "Мої дані", "token": ""},
    )


@router.get("/me/{table}", response_class=HTMLResponse)
async def me_table(
    table: str,
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    model = TABLES.get(table)
    if model is None:
        raise HTTPException(status_code=404, detail="Unknown table")

    cols = INDEX_COLS.get(table) or [c.name for c in model.__table__.columns]
    table_cols = model.__table__.columns
    pk = list(model.__table__.primary_key.columns)[0]
    order_col = next(
        (table_cols[c] for c in ("date", "created_at") if c in table_cols), pk
    )
    result = await session.execute(
        select(model)
        .where(model.user_id == user.id)
        .order_by(order_col.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = [[getattr(r, c) for c in cols] for r in result.scalars().all()]
    total = await _count(session, model, user.id)

    charts = first_date = last_date = None
    if table == "daily_metrics":
        charts, first_date, last_date = await _daily_charts(session, user.id)

    return templates.TemplateResponse(
        request, "table.html",
        {
            "table": table, "cols": cols, "rows": rows, "user": user,
            "limit": limit, "offset": offset, "total": total,
            "tables": list(TABLES), "base": "/me", "token": "",
            "charts": charts, "first_date": first_date, "last_date": last_date,
        },
    )


@router.get("/me/{table}/{row_id}", response_class=HTMLResponse)
async def me_row(
    table: str,
    row_id: int,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    model = TABLES.get(table)
    if model is None:
        raise HTTPException(status_code=404, detail="Unknown table")

    pk = list(model.__table__.primary_key.columns)[0]
    obj = (
        await session.execute(
            select(model).where(pk == row_id, model.user_id == user.id)
        )
    ).scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=404, detail="Row not found")  # not yours / missing

    fields = [(c.name, getattr(obj, c.name))
              for c in model.__table__.columns if c.name not in ("series", "analysis")]
    charts, first_x, last_x = _run_charts(getattr(obj, "series", None) or [])
    return templates.TemplateResponse(
        request, "detail.html",
        {"table": table, "fields": fields, "user": user, "base": "/me", "token": "",
         "charts": charts, "first_x": first_x, "last_x": last_x,
         "analysis": getattr(obj, "analysis", None)},
    )
