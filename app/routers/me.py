"""Per-user data view — a logged-in user browses their own metrics, activities and
reports (scoped to their user_id). Mirrors the admin /ui browser but never spans
other users, and excludes the users / bot_state tables."""
import datetime as dt
import math
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.db.models import ActivityRecord, DailyMetric, ReportLog, User
from app.dependencies import get_session
from app.garmin import repository
from app.routers.admin import INDEX_COLS, _run_charts

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _hm(hours):
    """Decimal hours → 'Xг Yхв' (8.6 → '8 год 36 хв'); empty for None."""
    if hours is None:
        return ""
    total = round(hours * 60)
    h, m = divmod(total, 60)
    if h and m:
        return f"{h} год {m} хв"
    return f"{h} год" if h else f"{m} хв"


templates.env.filters["hm"] = _hm

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
    "gravel_cycling": ("🚵", "#e0af68"), "gravel_ride": ("🚵", "#e0af68"),
    "strength_training": ("🏋️", "#e0af68"), "cardio": ("❤️", "#f7768e"),
    "yoga": ("🧘", "#bb9af7"), "swimming": ("🏊", "#7dcfff"),
    "lap_swimming": ("🏊", "#7dcfff"), "kitesurfing": ("🪁", "#7dcfff"),
    "tennis": ("🎾", "#c3e88d"),
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
        strain_ring = {"color": "#3aa0ff", **_ring_geom(r.load / 2, 24)} if r.load else None
        cards.append({
            "id": r.id, "emoji": emoji, "color": color,
            "label": (r.type or "—").replace("_", " ").capitalize(),
            "date": _nice_date(r.date),
            "dist_km": r.dist_km, "dur_min": r.dur_min,
            "avg_hr": r.avg_hr, "max_hr": r.max_hr, "load": r.load,
            "pace": _pace_str(r.dist_km, r.dur_min) if runwalk else None,
            "spark": _spark(r.series) if runwalk else None,
            "strain_ring": strain_ring,
            "has_analysis": bool(r.analysis),
        })
    return cards


# ---- daily recovery metrics ----
_SVG_W, _SVG_H, _SVG_PAD = 720, 120, 22
_RING_R = 76
_RING_CIRC = round(2 * math.pi * _RING_R, 1)


def _recovery_band(v):
    """Whoop-style recovery zone for a 0–100 score → (colour, label)."""
    if v is None:
        return "#6b7490", "—"
    if v >= 67:
        return "#16e08a", "Відновлено"
    if v >= 34:
        return "#ffd23f", "Помірно"
    return "#ff5470", "Втома"


def _ring_geom(value, r):
    """SVG ring dash/circumference for a 0–100 ``value`` at radius ``r``."""
    circ = round(2 * math.pi * r, 1)
    return {"circ": circ, "dash": round(circ * min(max(value, 0), 100) / 100, 1), "r": r}


def _recovery_ring(day):
    """Hero ring model from the latest day: readiness if present, else sleep score."""
    val = day["readiness"] if day.get("readiness") is not None else day.get("sleep_score")
    if val is None:
        return None
    color, label = _recovery_band(val)
    return {
        "value": int(val), "color": color, "label": label,
        "metric": "готовність" if day.get("readiness") is not None else "сон, бал",
        "circ": _RING_CIRC, "dash": round(_RING_CIRC * min(val, 100) / 100, 1),
        "r": _RING_R, "date": day["date"],
        "sleep_hm": _hm(day.get("sleep_h")), "hrv_avg": day.get("hrv_avg"), "rhr": day.get("rhr"),
    }


def _trend_series(values, dates):
    """Trend sparkline for daily_metrics with per-point data (``pts``: x-fraction + value
    + date label) so the chart can show a value on hover. None if < 2 points."""
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
    pts = [{"x": round(i / (n - 1), 4), "v": v, "lbl": dates[i] if i < len(dates) else ""}
           for i, v in pairs]
    return {"points": points, "dots": dots, "pts": pts, "ymin": ymin, "ymax": ymax,
            "last": ys[-1], "W": _SVG_W, "H": _SVG_H}


async def _daily_trends(session, user_id, days: int = 60):
    """HRV / sleep-hours / sleep-score trend charts (hover-enabled) for the daily view."""
    trend = await repository.read_history(session, user_id, days=days)
    dates = [r["date"] for r in trend]
    defs = [
        ("HRV avg", "#7aa2f7", "int", [r["hrv_avg"] for r in trend]),
        ("Сон, год", "#9ece6a", "f1", [r["sleep_h"] for r in trend]),
        ("Сон, бал", "#e0af68", "int", [r["sleep_score"] for r in trend]),
    ]
    charts = [{"label": lbl, "color": c, "fmt": fmt, "s": s}
              for lbl, c, fmt, vals in defs if (s := _trend_series(vals, dates))]
    return charts, (dates[0] if dates else ""), (dates[-1] if dates else "")


def _hrv_color(status):
    s = (status or "").upper()
    if s == "BALANCED":
        return "#9ece6a"
    if s in ("UNBALANCED", "LOW", "POOR"):
        return "#f7768e"
    return "#e0af68"


async def _daily_cards(session, user_id, limit, offset):
    rows = (await session.execute(
        select(DailyMetric).where(DailyMetric.user_id == user_id)
        .order_by(DailyMetric.date.desc()).limit(limit).offset(offset)
    )).scalars().all()
    out = []
    for r in rows:
        ex = r.extra or {}
        score = ex.get("readiness_score")
        if score is None:
            score = r.sleep_score
        ring = None
        if score is not None:
            color, _ = _recovery_band(score)
            ring = {"value": int(score), "color": color, **_ring_geom(score, 26)}
        out.append({
            "id": r.id, "date": _nice_date(r.date),
            "sleep_score": r.sleep_score, "sleep_h": r.sleep_h,
            "hrv_avg": r.hrv_avg, "hrv_status": r.hrv_status, "hrv_color": _hrv_color(r.hrv_status),
            "stress_avg": r.stress_avg,
            "bb_charged": r.bb_charged, "bb_drained": r.bb_drained,
            "rhr": ex.get("resting_hr"), "readiness": ex.get("readiness_score"),
            "ring": ring,
        })
    return out


async def _latest_ring(session, user_id):
    """The recovery ring for the most recent day (for the /me overview hero)."""
    rows = await _daily_cards(session, user_id, 1, 0)
    return _recovery_ring(rows[0]) if rows else None


# ---- report history ----
_KIND_META = {
    "report": ("Звіт", "#7aa2f7"), "morning": ("Ранок", "#e0af68"),
    "deep": ("Глибокий", "#bb9af7"), "ask": ("Питання", "#7dcfff"),
    "activity": ("Активність", "#9ece6a"), "plan": ("План", "#bb9af7"),
    "plan_edit": ("Правка", "#73daca"),
}


def _kind_meta(k):
    return _KIND_META.get(k, (k or "—", "#909aa8"))


async def _report_cards(session, user_id, limit, offset):
    rows = (await session.execute(
        select(ReportLog).where(ReportLog.user_id == user_id)
        .order_by(ReportLog.created_at.desc()).limit(limit).offset(offset)
    )).scalars().all()
    out = []
    for r in rows:
        label, color = _kind_meta(r.kind)
        out.append({
            "id": r.id, "label": label, "color": color, "ok": r.ok, "cached": r.cached,
            "when": r.created_at.strftime("%d.%m %H:%M") if r.created_at else "",
            "cost": r.cost_usd, "in_tok": r.input_tokens, "out_tok": r.output_tokens,
            "preview": ((r.report_text or r.question or r.error or "").strip()[:140]),
        })
    return out


@router.get("/me", response_class=HTMLResponse)
async def me_index(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    counts = {name: await _count(session, model, user.id) for name, model in TABLES.items()}
    hero = await _latest_ring(session, user.id)
    return templates.TemplateResponse(
        request, "index.html",
        {"counts": counts, "user": user, "hero": hero,
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

    # Dedicated card views for the user-facing tables.
    if table == "activities":
        cards = await _activity_cards(session, user.id, limit, offset)
        total = await _count(session, model, user.id)
        return templates.TemplateResponse(
            request, "activities.html",
            {"acts": cards, "user": user, "tables": list(TABLES), "base": "/me",
             "token": "", "limit": limit, "offset": offset, "total": total},
        )
    if table == "daily_metrics":
        days = await _daily_cards(session, user.id, limit, offset)
        charts, first_date, last_date = await _daily_trends(session, user.id)
        total = await _count(session, model, user.id)
        hero = _recovery_ring(days[0]) if offset == 0 and days else None
        return templates.TemplateResponse(
            request, "daily.html",
            {"days": days, "charts": charts, "first_date": first_date, "last_date": last_date,
             "hero": hero, "user": user, "tables": list(TABLES), "base": "/me", "token": "",
             "limit": limit, "offset": offset, "total": total},
        )
    if table == "report_logs":
        reports = await _report_cards(session, user.id, limit, offset)
        total = await _count(session, model, user.id)
        return templates.TemplateResponse(
            request, "reports.html",
            {"reports": reports, "user": user, "tables": list(TABLES), "base": "/me",
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

    # (activities / daily_metrics / report_logs have dedicated views above; this generic
    # table path remains as a safe fallback for any future table.)
    return templates.TemplateResponse(
        request, "table.html",
        {
            "table": table, "cols": cols, "rows": rows, "user": user,
            "limit": limit, "offset": offset, "total": total,
            "tables": list(TABLES), "base": "/me", "token": "",
            "charts": None, "first_date": None, "last_date": None,
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

    # Activities get a dedicated hero + stats + charts view.
    if table == "activities":
        emoji, color = _act_meta(obj.type)
        runwalk = (obj.type or "").lower() in _RUNWALK
        a = {
            "id": obj.id, "emoji": emoji, "color": color,
            "label": (obj.type or "—").replace("_", " ").capitalize(),
            "date": _nice_date(obj.date),
            "dist_km": obj.dist_km, "dur_min": obj.dur_min,
            "avg_hr": obj.avg_hr, "max_hr": obj.max_hr, "load": obj.load,
            "pace": _pace_str(obj.dist_km, obj.dur_min) if runwalk else None,
            "exercises": obj.exercises,
        }
        strain = None
        if obj.load:
            strain = {"value": int(obj.load), "color": "#3aa0ff", "label": "Навантаження",
                      **_ring_geom(obj.load / 2, 76)}   # load ~0..200 → 0..100%
        charts, first_x, last_x = _run_charts(obj.series or [])
        return templates.TemplateResponse(
            request, "activity.html",
            {"a": a, "strain": strain, "charts": charts, "first_x": first_x, "last_x": last_x,
             "analysis": obj.analysis, "user": user, "base": "/me", "token": ""},
        )

    if table == "report_logs":
        label, color = _kind_meta(obj.kind)
        return templates.TemplateResponse(
            request, "report.html",
            {"r": obj, "label": label, "color": color,
             "when": obj.created_at.strftime("%d.%m.%Y %H:%M") if obj.created_at else "",
             "user": user, "base": "/me", "token": ""},
        )

    if table == "daily_metrics":
        ex = obj.extra or {}
        return templates.TemplateResponse(
            request, "day.html",
            {"m": obj, "date": _nice_date(obj.date), "hrv_color": _hrv_color(obj.hrv_status),
             "extra": ex, "user": user, "base": "/me", "token": ""},
        )

    fields = [(c.name, getattr(obj, c.name))
              for c in model.__table__.columns if c.name not in ("series", "analysis")]
    charts, first_x, last_x = _run_charts(getattr(obj, "series", None) or [])
    return templates.TemplateResponse(
        request, "detail.html",
        {"table": table, "fields": fields, "user": user, "base": "/me", "token": "",
         "charts": charts, "first_x": first_x, "last_x": last_x,
         "analysis": getattr(obj, "analysis", None)},
    )
