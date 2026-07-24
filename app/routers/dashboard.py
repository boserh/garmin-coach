"""EP-04: the mobile-first web dashboard — a single logged-in-user overview page
(readiness today, 30-day recovery trends, next 7 days of the active plan, last 5
activities, this month's AI cost) instead of paging through the raw /me tables.

Pure DB reads only — no Garmin/Claude call on this path, so it renders fast and free.
Reuses the same building blocks as /me and /plan (the hero ring, trend charts, plan
row markup, activity cards) rather than growing a parallel chart/markup stack.
"""
import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.charts import trend_series as _trend_series
from app.core.auth import current_user
from app.db.models import User
from app.dependencies import get_session
from app.garmin import repository, service
from app.routers.me import _act_meta, _latest_ring, _pace_str
from app.routers.plan import _dm, _dow

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["dow"] = _dow
templates.env.filters["dm"] = _dm

router = APIRouter(tags=["dashboard"])

TREND_DAYS = 30
PLAN_WINDOW_DAYS = 7
ACTIVITIES_N = 5

# label/colour/format for each 30-day trend sparkline, sourced from repository.read_history
_TREND_DEFS = [
    ("HRV", "#7aa2f7", "int", "hrv_avg"),
    ("Пульс спокою", "#f7768e", "int", "resting_hr"),
    ("Сон, год", "#9ece6a", "f1", "sleep_h"),
    ("Стрес", "#e0af68", "int", "stress_avg"),
]


def _trend_charts(trend: list) -> tuple:
    """30-day HRV/RHR/sleep/stress sparklines with hover — same shape as the /me
    daily view's charts, just a different metric set (adds RHR + stress)."""
    dates = [r["date"] for r in trend]
    charts = [
        {"label": lbl, "color": c, "fmt": fmt, "s": s}
        for lbl, c, fmt, key in _TREND_DEFS
        if (s := _trend_series([r.get(key) for r in trend], dates))
    ]
    return charts, (dates[0] if dates else ""), (dates[-1] if dates else "")


def _activity_cards(rows: list) -> list:
    out = []
    for a in rows:
        emoji, color = _act_meta(a["type"])
        out.append({
            "id": a["id"], "date": a["date"], "type": a["type"], "emoji": emoji, "color": color,
            "dist_km": a["dist_km"], "dur_min": a["dur_min"], "avg_hr": a["avg_hr"],
            "pace": _pace_str(a["dist_km"], a["dur_min"]),
        })
    return out


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    trend = await repository.read_history(session, user.id, days=TREND_DAYS)
    today = await _latest_ring(session, user.id)
    charts, first_x, last_x = _trend_charts(trend)

    plan = await repository.get_active_plan(session, user.id)
    upcoming = []
    if plan is not None:
        window_end = (dt.date.today() + dt.timedelta(days=PLAN_WINDOW_DAYS)).isoformat()
        upcoming = [
            w for w in await repository.list_workouts(session, plan.id, upcoming_only=True)
            if w.date <= window_end
        ]

    activities = _activity_cards(await repository.list_activities(session, user.id, n=ACTIVITIES_N))
    month_cost = await repository.month_cost(session, user.id)

    # OPS-05: a banner when the Garmin API threw failures in the last 24h (degradation vs a
    # watch that just hasn't synced). Expected garth 403 gaps are excluded from the count.
    garmin_errors = service.summarize_garmin_errors(
        await repository.get_state(session, user.id, service.GARMIN_ERRORS_KEY)
    )

    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "user": user, "today": today,
            "charts": charts, "first_x": first_x, "last_x": last_x,
            "has_history": bool(trend),
            "plan": plan, "upcoming": upcoming,
            "activities": activities,
            "month_cost": month_cost,
            "today_iso": dt.date.today().isoformat(),
            "garmin_errors": garmin_errors,
        },
    )
