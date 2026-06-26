"""Training-plan setup + view (web).

A logged-in user picks a goal and a few intake answers (``GET/POST /plan``); we ask
Claude to generate a dated program (``app.analysis.service.run_plan_generation``) and
store it. Day-to-day adjustments happen in the bot (free text). One active plan per user.
"""
import datetime as dt
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.service import AnalystError, run_plan_generation
from app.core.auth import current_user
from app.db.models import User
from app.dependencies import get_session
from app.garmin import repository
from app.garmin.runtime import user_runtime

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

logger = logging.getLogger("plan")

router = APIRouter(tags=["plan"])

GOALS = {
    "first_5k": "Перші 5 км",
    "faster_5k": "Швидше 5 км",
    "first_10k": "Перші 10 км",
    "first_half": "Перший півмарафон",
}


def _by_week(workouts):
    """Group workouts into [(week_no, [workouts...]), ...] ordered by week."""
    weeks: dict = {}
    for w in workouts:
        weeks.setdefault(w.week or 0, []).append(w)
    return [(wk, weeks[wk]) for wk in sorted(weeks)]


@router.get("/plan", response_class=HTMLResponse)
async def plan_page(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    plan = await repository.get_active_plan(session, user.id)
    if plan is None:
        return templates.TemplateResponse(
            request, "plan_setup.html",
            {"user": user, "goals": GOALS, "today": dt.date.today().isoformat(),
             "error": request.query_params.get("error")},
        )
    workouts = await repository.list_workouts(session, plan.id)
    return templates.TemplateResponse(
        request, "plan.html",
        {"user": user, "plan": plan, "weeks": _by_week(workouts),
         "today": dt.date.today().isoformat(),
         "created": request.query_params.get("created") == "1",
         "count": len(workouts)},
    )


@router.post("/plan")
async def plan_create(
    goal: str = Form(...),
    target_date: str = Form(""),
    days_per_week: str = Form("3"),
    intensity: str = Form("moderate"),
    recent_5k: str = Form(""),
    longest_run_km: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    if goal not in GOALS:
        return RedirectResponse("/plan?error=goal", status_code=303)
    try:
        dpw = int(days_per_week)
    except ValueError:
        dpw = 3
    intake = {
        "recent_5k": recent_5k.strip() or None,
        "longest_run_km": longest_run_km.strip() or None,
        "notes": notes.strip() or None,
    }
    logger.info(f"PLAN generate requested user={user.id} goal={goal} dpw={dpw}")
    async with user_runtime(session, user) as creds:
        try:
            plan = await run_plan_generation(
                session, user_id=user.id, goal=goal, goal_label=GOALS[goal],
                target_date=target_date or None, start_date=dt.date.today().isoformat(),
                days_per_week=dpw, intensity=intensity, intake=intake,
                api_key=creds.anthropic_key,
            )
        except AnalystError as e:
            logger.warning(f"PLAN generate failed user={user.id}: {e}")
            return RedirectResponse("/plan?error=gen", status_code=303)
    logger.info(f"PLAN created id={plan.id} user={user.id}")
    return RedirectResponse("/plan?created=1", status_code=303)


@router.post("/plan/archive")
async def plan_archive(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    plan = await repository.get_active_plan(session, user.id)
    if plan:
        await repository.archive_plan(session, plan)
    return RedirectResponse("/plan", status_code=303)
