"""Minimal server-rendered UI to browse the database tables.

Whitelisted models only (no arbitrary SQL). Token-gated like the other data
endpoints; the token can be passed as ``?token=`` so plain browser links work.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_admin
from app.db.models import ActivityRecord, BotState, DailyMetric, ReportLog, User
from app.dependencies import get_session
from app.garmin import repository

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# name → ORM model (whitelist; the path param is matched against these keys only)
TABLES = {
    "users": User,
    "daily_metrics": DailyMetric,
    "activities": ActivityRecord,
    "report_logs": ReportLog,
    "bot_state": BotState,
}

# Columns shown on a table's list view (the detail page always shows every column).
# Tables not listed here show all columns. Keeps the activities list scannable; the
# heavy fields (load/exercises/series) live on the per-row detail page.
INDEX_COLS = {
    "activities": ["id", "date", "type", "dur_min", "dist_km", "avg_hr", "max_hr"],
}

# The raw DB browser spans all users' rows → admin only.
router = APIRouter(tags=["ui"], dependencies=[Depends(require_admin)])

# inline-SVG sparkline geometry (no JS / no CDN — renders server-side)
_SVG_W, _SVG_H, _SVG_PAD = 720, 120, 22


async def _count(session: AsyncSession, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


def _series(values):
    """Scale a chronological value list to SVG coords for a trend sparkline.
    Returns None when there are fewer than 2 data points to draw."""
    pairs = [(i, float(v)) for i, v in enumerate(values) if v is not None]
    if len(pairs) < 2:
        return None
    n = len(values)
    ys = [v for _, v in pairs]
    ymin, ymax = min(ys), max(ys)
    span = (ymax - ymin) or 1.0

    def px(i):
        return _SVG_PAD + (i / (n - 1)) * (_SVG_W - 2 * _SVG_PAD)

    def py(v):
        return _SVG_H - _SVG_PAD - ((v - ymin) / span) * (_SVG_H - 2 * _SVG_PAD)

    dots = [(round(px(i), 1), round(py(v), 1)) for i, v in pairs]
    points = " ".join(f"{x},{y}" for x, y in dots)
    return {"points": points, "dots": dots, "ymin": ymin, "ymax": ymax,
            "last": ys[-1], "W": _SVG_W, "H": _SVG_H}


async def _daily_charts(session: AsyncSession, user_id: int, days: int = 60):
    """Trend charts (HRV / sleep hours / sleep score) for the daily_metrics page
    (the viewing admin's own data)."""
    trend = await repository.read_history(session, user_id, days=days)
    dates = [r["date"] for r in trend]
    defs = [
        ("HRV avg", "#6cb6ff", [r["hrv_avg"] for r in trend]),
        ("Сон, год", "#7ee787", [r["sleep_h"] for r in trend]),
        ("Сон, бал", "#e3b341", [r["sleep_score"] for r in trend]),
    ]
    charts = [{"label": lbl, "color": c, "s": s}
              for lbl, c, vals in defs if (s := _series(vals))]
    return charts, (dates[0] if dates else ""), (dates[-1] if dates else "")


def _run_charts(series):
    """Pace + HR sparklines for a run's per-point series ([{d, p, hr}, ...]).
    Returns (charts, first_km, last_km) for the activity detail page."""
    if not series:
        return [], "", ""
    defs = [
        ("Темп, хв/км", "#6cb6ff", [p.get("p") for p in series]),
        ("Пульс", "#ff7b72", [p.get("hr") for p in series]),
    ]
    charts = [{"label": lbl, "color": c, "s": s}
              for lbl, c, vals in defs if (s := _series(vals))]
    dists = [p.get("d") for p in series if p.get("d") is not None]
    first = f"{dists[0]:.1f} км" if dists else ""
    last = f"{dists[-1]:.1f} км" if dists else ""
    return charts, first, last


@router.get("/ui", response_class=HTMLResponse)
async def ui_index(
    request: Request,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    counts = {name: await _count(session, model) for name, model in TABLES.items()}
    return templates.TemplateResponse(
        request, "index.html",
        {"counts": counts, "user": user,
         "base": "/ui", "title": "Garmin DB",
         "token": request.query_params.get("token", "")},
    )


@router.get("/ui/{table}", response_class=HTMLResponse)
async def ui_table(
    table: str,
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    model = TABLES.get(table)
    if model is None:
        raise HTTPException(status_code=404, detail="Unknown table")

    cols = INDEX_COLS.get(table) or [c.name for c in model.__table__.columns]
    pk = list(model.__table__.primary_key.columns)[0]
    # Order by the most meaningful recency column (newest first), not the PK,
    # so date-based tables read chronologically instead of by insert order.
    table_cols = model.__table__.columns
    order_col = next(
        (table_cols[c] for c in ("date", "created_at") if c in table_cols), pk
    )
    result = await session.execute(
        select(model).order_by(order_col.desc()).limit(limit).offset(offset)
    )
    rows = [[getattr(r, c) for c in cols] for r in result.scalars().all()]
    total = await _count(session, model)

    charts = first_date = last_date = None
    if table == "daily_metrics":
        charts, first_date, last_date = await _daily_charts(session, user.id)

    return templates.TemplateResponse(
        request, "table.html",
        {
            "table": table, "cols": cols, "rows": rows, "user": user,
            "limit": limit, "offset": offset, "total": total,
            "tables": list(TABLES), "token": request.query_params.get("token", ""),
            "charts": charts, "first_date": first_date, "last_date": last_date,
        },
    )


@router.get("/ui/{table}/{row_id}", response_class=HTMLResponse)
async def ui_row(
    table: str,
    row_id: str,
    request: Request,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    model = TABLES.get(table)
    if model is None:
        raise HTTPException(status_code=404, detail="Unknown table")

    pk = list(model.__table__.primary_key.columns)[0]
    try:
        key = int(row_id)  # integer PKs (most tables); bot_state uses a string key
    except ValueError:
        key = row_id
    obj = (await session.execute(select(model).where(pk == key))).scalar_one_or_none()
    if obj is None:
        raise HTTPException(status_code=404, detail="Row not found")

    # ``series`` is a long per-point array — render it as charts, not raw JSON.
    fields = [(c.name, getattr(obj, c.name))
              for c in model.__table__.columns if c.name != "series"]
    charts, first_x, last_x = _run_charts(getattr(obj, "series", None) or [])
    return templates.TemplateResponse(
        request, "detail.html",
        {
            "table": table, "fields": fields, "user": user,
            "charts": charts, "first_x": first_x, "last_x": last_x,
            "token": request.query_params.get("token", ""),
        },
    )


@router.post("/ui/bot_state/delete")
async def bot_state_delete(
    user_id: int = Form(...),
    key: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Clear one bot_state row (e.g. a user's morning-sent guard so the report can
    re-fire). Composite PK (user_id, key)."""
    obj = await session.get(BotState, (user_id, key))
    if obj is not None:
        await session.delete(obj)
        await session.commit()
    return RedirectResponse("/ui/bot_state", status_code=303)
