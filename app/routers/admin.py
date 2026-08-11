"""Minimal server-rendered UI to browse the database tables.

Whitelisted models only (no arbitrary SQL). Token-gated like the other data
endpoints; the token can be passed as ``?token=`` so plain browser links work.
"""
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import (
    JSON,
    Boolean,
    Integer,
    LargeBinary,
    String,
    Text,
    and_,
    cast,
    func,
    or_,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.charts import run_charts as _run_charts
from app.charts import series as _series
from app.core.auth import require_admin
from app.db import llm_cache
from app.db.models import (
    ActivityRecord,
    BotState,
    CheckupAttachment,
    DailyMetric,
    HealthCheckup,
    PersonalRecord,
    PlannedWorkout,
    ReportLog,
    Supplement,
    TrainingPlan,
    User,
)
from app.dependencies import get_session
from app.garmin import client as garmin_client
from app.garmin import repository
from app.templating import create_templates

templates = create_templates()

# name → ORM model (whitelist; the path param is matched against these keys only).
# llm_cache and job_runs are deliberately excluded — they already have dedicated
# views (/admin/cache, /admin/jobs) rather than a raw-table one.
TABLES = {
    "users": User,
    "daily_metrics": DailyMetric,
    "activities": ActivityRecord,
    "report_logs": ReportLog,
    "bot_state": BotState,
    "personal_records": PersonalRecord,
    "training_plans": TrainingPlan,
    "planned_workouts": PlannedWorkout,
    "supplements": Supplement,
    "health_checkups": HealthCheckup,
    "checkup_attachments": CheckupAttachment,
}

# Columns shown on a table's list view (the detail page always shows every column).
# Tables not listed here show all columns. Keeps the activities list scannable; the
# heavy fields (load/exercises/series) live on the per-row detail page.
INDEX_COLS = {
    "activities": ["id", "date", "type", "dur_min", "dist_km", "avg_hr", "max_hr"],
    # "data" is a raw blob (the uploaded photo/PDF) — unusable in a text table cell.
    "checkup_attachments": ["id", "checkup_id", "filename", "media_type", "created_at"],
}

# The raw DB browser spans all users' rows → admin only.
router = APIRouter(tags=["ui"], dependencies=[Depends(require_admin)])


async def _count(session: AsyncSession, model, where=None) -> int:
    stmt = select(func.count()).select_from(model)
    if where is not None:
        stmt = stmt.where(where)
    return (await session.execute(stmt)).scalar_one()


def _search_filter(model, search: str):
    """Substring match (case-insensitive) across every column, cast to text.

    Secondary catch-all (e.g. a report's question text) on top of the real
    per-column filters below — lets one search box cover every table without
    per-table filter config.
    """
    if not search:
        return None
    like = f"%{search}%"
    cols = [c for c in model.__table__.columns if c.name != "data"]  # skip blob columns
    return or_(*(cast(c, String).ilike(like) for c in cols))


# Max distinct values for a column to be offered as a dropdown filter — beyond
# this it's not a meaningful "category" (e.g. hrv_avg has ~60 distinct ints).
ENUM_FILTER_MAX_DISTINCT = 25

# Stands in for SQL NULL as a dropdown option's URL value — real values collide
# with this only in theory (no column here holds the literal string "__none__").
NULL_FILTER_VALUE = "__none__"


def _filter_candidate_columns(model):
    """Short scalar columns worth checking for dropdown-filter eligibility —
    excludes PKs, FKs, the ``date`` column (has its own range filter), and any
    blob/JSON/long-text field."""
    cols = []
    for c in model.__table__.columns:
        if c.primary_key or c.name.endswith("_id") or c.name == "date":
            continue
        if isinstance(c.type, (JSON, Text, LargeBinary)):
            continue
        if isinstance(c.type, String) and (c.type.length is None or c.type.length > 64):
            continue
        cols.append(c)
    return cols


async def _enum_filter_options(session: AsyncSession, model) -> dict:
    """{column_name: [(url_value, label), ...]} for every candidate column whose
    distinct values (over the whole table, ignoring any currently-active filter —
    so the dropdown never disappears once picked) fit within ENUM_FILTER_MAX_DISTINCT."""
    options = {}
    for c in _filter_candidate_columns(model):
        rows = (
            await session.execute(
                select(c).distinct().limit(ENUM_FILTER_MAX_DISTINCT + 1)
            )
        ).scalars().all()
        if 0 < len(rows) <= ENUM_FILTER_MAX_DISTINCT:
            rows.sort(key=lambda v: (v is None, str(v)))
            options[c.name] = [
                (NULL_FILTER_VALUE, "— порожньо —") if v is None else (str(v), str(v))
                for v in rows
            ]
    return options


def _coerce_filter_value(col, value: str):
    """A dropdown option's value round-trips through the URL as a string —
    put it back into the column's Python type so ``col == value`` compares
    correctly (SQLite in particular won't match `1` against `"True"`)."""
    if isinstance(col.type, Boolean):
        return value in ("True", "true", "1")
    if isinstance(col.type, Integer):
        try:
            return int(value)
        except ValueError:
            return value
    return value


def _column_filters(model, active: dict):
    """Exact-match conditions for the ``f_<col>=<value>`` query params picked
    from the dropdowns built by ``_enum_filter_options``."""
    table_cols = model.__table__.columns
    conds = []
    for name, value in active.items():
        if name not in table_cols or value == "":
            continue
        col = table_cols[name]
        if value == NULL_FILTER_VALUE:
            conds.append(col.is_(None))
        else:
            conds.append(col == _coerce_filter_value(col, value))
    return conds


async def _user_filter_options(session: AsyncSession) -> list:
    """[(user_id_str, email), ...] for the "Користувач" dropdown — a raw user_id
    is meaningless to an admin, so this is offered separately from the generic
    enum-column scan (which skips every ``*_id`` column on purpose)."""
    rows = (await session.execute(select(User.id, User.email).order_by(User.email))).all()
    return [(str(uid), email) for uid, email in rows]


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
         "base": "/ui", "title": "Bihun DB",
         "token": request.query_params.get("token", "")},
    )


@router.get("/admin/jobs", response_class=HTMLResponse)
async def admin_jobs(
    request: Request,
    job: str = Query(""),
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """OPS-04: all users' background-job runs (admin), optionally filtered by job label."""
    from app.db import job_runs as _job_runs
    runs = await _job_runs.recent_job_runs(session, job=job or None, limit=100)
    return templates.TemplateResponse(
        request, "jobs.html",
        {"runs": runs, "user": user, "base": "/ui", "job_filter": job,
         "is_admin_view": True, "title": "Фонові задачі (всі)",
         "token": request.query_params.get("token", "")},
    )


@router.get("/ui/{table}", response_class=HTMLResponse)
async def ui_table(
    table: str,
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    search: str = Query(""),
    date_from: str = Query(""),
    date_to: str = Query(""),
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
    date_col = table_cols["date"] if "date" in table_cols else None

    active_filters = {
        k[2:]: v for k, v in request.query_params.items() if k.startswith("f_")
    }
    conds = _column_filters(model, active_filters)
    if date_col is not None:
        if date_from:
            conds.append(date_col >= date_from)
        if date_to:
            conds.append(date_col <= date_to)
    search_cond = _search_filter(model, search.strip())
    if search_cond is not None:
        conds.append(search_cond)
    where = and_(*conds) if conds else None

    stmt = select(model).order_by(order_col.desc()).limit(limit).offset(offset)
    if where is not None:
        stmt = stmt.where(where)
    result = await session.execute(stmt)
    rows = [[getattr(r, c) for c in cols] for r in result.scalars().all()]
    total = await _count(session, model, where)
    enum_options = await _enum_filter_options(session, model)
    user_options = (
        await _user_filter_options(session) if "user_id" in table_cols else []
    )

    charts = first_date = last_date = None
    if table == "daily_metrics":
        charts, first_date, last_date = await _daily_charts(session, user.id)

    token = request.query_params.get("token", "")
    # Every active filter, preserved across pagination links (offset/limit are
    # set explicitly by those links, so they're excluded here).
    preserved = {"token": token, "search": search, "date_from": date_from, "date_to": date_to,
                 **{f"f_{k}": v for k, v in active_filters.items()}}
    page_qs = urlencode({k: v for k, v in preserved.items() if v})

    return templates.TemplateResponse(
        request, "table.html",
        {
            "table": table, "cols": cols, "rows": rows, "user": user,
            "limit": limit, "offset": offset, "total": total, "search": search,
            "tables": list(TABLES), "token": token, "page_qs": page_qs,
            "charts": charts, "first_date": first_date, "last_date": last_date,
            "enum_options": enum_options, "active_filters": active_filters,
            "user_options": user_options, "has_date_col": date_col is not None,
            "date_from": date_from, "date_to": date_to,
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

    # ``series`` renders as charts; ``analysis`` as its own block; ``data`` is a raw
    # blob (checkup_attachments) — none render as plain fields.
    fields = [(c.name, getattr(obj, c.name))
              for c in model.__table__.columns
              if c.name not in ("series", "analysis", "data")]
    charts, first_x, last_x = _run_charts(getattr(obj, "series", None) or [])
    return templates.TemplateResponse(
        request, "detail.html",
        {
            "table": table, "fields": fields, "user": user,
            "charts": charts, "first_x": first_x, "last_x": last_x,
            "analysis": getattr(obj, "analysis", None),
            "token": request.query_params.get("token", ""),
        },
    )


@router.get("/admin/cache", response_class=HTMLResponse)
async def admin_cache(
    request: Request,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """ST-20: llm_cache + Garmin disk-cache summary, with purge actions below.
    OPS-06: + dedup cache-hit rate by kind (7/30 days)."""
    llm = await llm_cache.stats(session)
    garmin = garmin_client.cache_stats()
    hit_7 = await repository.cache_hit_stats(session, days=7)
    hit_30 = await repository.cache_hit_stats(session, days=30)
    return templates.TemplateResponse(
        request, "cache.html",
        {"user": user, "base": "/ui", "title": "Кеші",
         "token": request.query_params.get("token", ""),
         "llm": llm, "garmin": garmin, "hit_7": hit_7, "hit_30": hit_30,
         "msg": request.query_params.get("msg", "")},
    )


@router.post("/admin/cache/llm/purge_expired")
async def admin_cache_llm_purge_expired(session: AsyncSession = Depends(get_session)):
    n = await llm_cache.purge_expired(session)
    return RedirectResponse(
        f"/admin/cache?msg=Видалено+{n}+прострочених+llm_cache", status_code=303
    )


@router.post("/admin/cache/llm/purge_all")
async def admin_cache_llm_purge_all(session: AsyncSession = Depends(get_session)):
    n = await llm_cache.purge_all(session)
    return RedirectResponse(
        f"/admin/cache?msg=Видалено+{n}+рядків+llm_cache+повністю", status_code=303
    )


@router.post("/admin/cache/garmin/purge_expired")
async def admin_cache_garmin_purge_expired():
    n = garmin_client.cache_purge_expired()
    return RedirectResponse(
        f"/admin/cache?msg=Видалено+{n}+прострочених+файлів+garmin-кешу", status_code=303
    )


@router.post("/admin/cache/garmin/del_activity")
async def admin_cache_garmin_del_activity(activity_id: str = Form(...)):
    garmin_client.cache_del_activity(activity_id)
    return RedirectResponse(
        f"/admin/cache?msg=Видалено+garmin-кеш+активності+{activity_id}", status_code=303
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
