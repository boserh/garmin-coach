"""Per-user data view — a logged-in user browses their own metrics, activities and
reports (scoped to their user_id). Mirrors the admin /ui browser but never spans
other users, and excludes the users / bot_state tables."""
import datetime as dt
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


# ---- activities: a nice card view (type icon, key stats, run sparkline) ----
# activity type → (emoji, accent colour). Matched exactly, else by first word.
_ACT_META = {
    "running": ("🏃", "#7aa2f7"), "treadmill_running": ("🏃", "#7aa2f7"),
    "trail_running": ("⛰️", "#9ece6a"), "track_running": ("🏃", "#7aa2f7"),
    "walking": ("🚶", "#73daca"), "hiking": ("🥾", "#9ece6a"),
    "cycling": ("🚴", "#7dcfff"), "road_biking": ("🚴", "#7dcfff"),
    "mountain_biking": ("🚵", "#9ece6a"), "indoor_cycling": ("🚴", "#7dcfff"),
    "strength_training": ("🏋️", "#e0af68"), "cardio": ("❤️", "#f7768e"),
    "yoga": ("🧘", "#bb9af7"), "swimming": ("🏊", "#7dcfff"),
    "lap_swimming": ("🏊", "#7dcfff"), "kitesurfing": ("🪁", "#7dcfff"),
}
_RUNWALK = {"running", "treadmill_running", "trail_running", "track_running",
            "walking", "hiking"}
_MONTHS = ["січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер", "жов", "лис", "гру"]
_DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def _act_meta(t: str):
    t = (t or "").lower()
    if t in _ACT_META:
        return _ACT_META[t]
    head = t.split("_")[0]
    for k, v in _ACT_META.items():
        if k.startswith(head):
            return v
    return ("🏅", "#909aa8")


def _nice_date(iso: str) -> str:
    try:
        d = dt.date.fromisoformat((iso or "")[:10])
        return f"{_DOW[d.weekday()]}, {d.day} {_MONTHS[d.month - 1]} {d.year}"
    except (ValueError, TypeError):
        return iso or ""


def _pace_str(dist_km, dur_min):
    if not dist_km or not dur_min:
        return None
    total = round(dur_min / dist_km * 60)   # seconds per km
    return f"{total // 60}:{total % 60:02d}"


def _spark(series, n: int = 48):
    """A pace sparkline (SVG points) from a run's series; faster = higher. None if too short."""
    vals = [p.get("p") for p in (series or []) if p.get("p")]
    if len(vals) < 3:
        return None
    if len(vals) > n:
        step = len(vals) / n
        vals = [vals[int(i * step)] for i in range(n)]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    W, H, pad = 160, 36, 3
    m = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = pad + (W - 2 * pad) * i / (m - 1)
        y = pad + (H - 2 * pad) * (v - lo) / rng   # higher pace (slower) sits lower
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


async def _activity_cards(session, user_id, limit, offset):
    rows = (await session.execute(
        select(ActivityRecord)
        .where(ActivityRecord.user_id == user_id)
        .order_by(ActivityRecord.date.desc(), ActivityRecord.id.desc())
        .limit(limit).offset(offset)
    )).scalars().all()
    cards = []
    for r in rows:
        emoji, color = _act_meta(r.type)
        runwalk = (r.type or "").lower() in _RUNWALK
        cards.append({
            "id": r.id, "emoji": emoji, "color": color,
            "label": (r.type or "—").replace("_", " ").capitalize(),
            "date": _nice_date(r.date),
            "dist_km": r.dist_km, "dur_min": r.dur_min,
            "avg_hr": r.avg_hr, "max_hr": r.max_hr, "load": r.load,
            "pace": _pace_str(r.dist_km, r.dur_min) if runwalk else None,
            "spark": _spark(r.series) if runwalk else None,
            "has_analysis": bool(r.analysis),
        })
    return cards


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

    # Activities get a dedicated card view (type icon, stats, run sparkline).
    if table == "activities":
        cards = await _activity_cards(session, user.id, limit, offset)
        total = await _count(session, model, user.id)
        return templates.TemplateResponse(
            request, "activities.html",
            {"acts": cards, "user": user, "tables": list(TABLES), "base": "/me",
             "token": "", "limit": limit, "offset": offset, "total": total},
        )

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
