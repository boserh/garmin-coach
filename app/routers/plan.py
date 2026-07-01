"""Training-plan setup + view (web).

A logged-in user picks a goal and a few intake answers (``GET/POST /plan``); we ask
Claude to generate a dated program (``app.analysis.service.run_plan_generation``) and
store it. Day-to-day adjustments happen in the bot (free text). One active plan per user.
"""
import asyncio
import datetime as dt
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.service import AnalystError, run_plan_generation
from app.core.auth import current_user
from app.db.base import async_session_maker
from app.db.models import User
from app.dependencies import get_session
from app.garmin import plan_sync, repository
from app.garmin.runtime import user_runtime

# Per-user BotState key tracking an in-flight (Opus, slow) plan generation: "pending"
# while running, "err:<msg>" on failure, cleared once the new plan is active. Generation
# runs in a background task so the request returns immediately (no gateway 504).
PLAN_GEN_KEY = "plan_gen"
# A "pending:<epoch>" older than this is treated as dead (e.g. the worker restarted
# mid-generation) so /plan falls back to the form instead of spinning forever.
PLAN_GEN_STALE_S = 600
_bg_tasks: set = set()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _fmt_step(s: dict) -> str:
    """Render one structured workout step as a compact human label, e.g.
    'розминка 1.5 км', 'біг 3 хв @ 5:15–5:24/км', '5× (…)'."""
    kinds = {"warmup": "розминка", "run": "біг", "recovery": "відновлення",
             "cooldown": "заминка", "repeat": "повтор"}
    if not isinstance(s, dict):
        return ""
    if s.get("kind") == "repeat":
        inner = " + ".join(_fmt_step(x) for x in (s.get("steps") or []))
        return f"{s.get('reps', '')}× ({inner})"
    label = kinds.get(s.get("kind"), s.get("kind") or "")
    dist_m, dur_s = s.get("dist_m"), s.get("dur_s")
    if isinstance(dist_m, (int, float)):
        amount = f"{dist_m / 1000:.1f} км".rstrip()
    elif isinstance(dur_s, (int, float)):
        amount = f"{int(dur_s // 60)} хв" if dur_s >= 60 else f"{int(dur_s)} с"
    else:
        amount = ""
    pace = s.get("pace_min_km")
    pace_str = ""
    if isinstance(pace, (list, tuple)) and len(pace) == 2 and all(
            isinstance(p, (int, float)) for p in pace):
        pace_str = f" @ {_pace(pace[0])}–{_pace(pace[1])}/км"
    return " ".join(p for p in (label, amount) if p) + pace_str


def _pace(dec: float) -> str:
    """Decimal min/km → m:ss (6.75 → 6:45)."""
    total = round(dec * 60)
    return f"{total // 60}:{total % 60:02d}"


_DOW_UK = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def _dow(iso: str) -> str:
    """ISO date → Ukrainian weekday abbreviation."""
    try:
        return _DOW_UK[dt.date.fromisoformat(iso).weekday()]
    except (ValueError, TypeError):
        return ""


def _dm(iso: str) -> str:
    """ISO date → 'day month' (7 лип)."""
    try:
        return _fmt_day(dt.date.fromisoformat(iso))
    except (ValueError, TypeError):
        return iso


templates.env.filters["fmt_step"] = _fmt_step
templates.env.filters["dow"] = _dow
templates.env.filters["dm"] = _dm

logger = logging.getLogger("plan")

router = APIRouter(tags=["plan"])

GOALS = {
    "first_5k": "Перші 5 км",
    "faster_5k": "Швидше 5 км",
    "first_10k": "Перші 10 км",
    "first_half": "Перший півмарафон",
}

# weekday slug → Ukrainian label (used for the run-day picker)
WEEKDAYS = {
    "mon": "Пн", "tue": "Вт", "wed": "Ср", "thu": "Чт",
    "fri": "Пт", "sat": "Сб", "sun": "Нд",
}


_MONTHS_UK = ["січ", "лют", "бер", "кві", "тра", "чер",
              "лип", "сер", "вер", "жов", "лис", "гру"]


def _fmt_day(d: dt.date) -> str:
    return f"{d.day} {_MONTHS_UK[d.month - 1]}"


def _by_week(workouts):
    """Group workouts into **Monday–Sunday calendar weeks** (by date, not the plan's
    ``week`` field), ordered and numbered sequentially. Returns
    ``[(week_no, "29 чер – 5 лип", [workouts...]), ...]``."""
    weeks: dict = {}
    for w in workouts:
        try:
            d = dt.date.fromisoformat(w.date)
            monday = d - dt.timedelta(days=d.weekday())   # ISO week start (Mon)
        except (ValueError, TypeError):
            monday = None
        weeks.setdefault(monday, []).append(w)
    out = []
    for i, monday in enumerate(sorted(k for k in weeks if k is not None), 1):
        sunday = monday + dt.timedelta(days=6)
        out.append((i, f"{_fmt_day(monday)} – {_fmt_day(sunday)}", weeks[monday]))
    if None in weeks:   # undated (shouldn't happen) — keep them visible at the end
        out.append((len(out) + 1, "", weeks[None]))
    return out


async def _generate_plan_bg(user_id: int, params: dict) -> None:
    """Run the (slow, Opus) plan generation off the request path, in its own DB session.
    Writes the result via ``run_plan_generation``; updates the per-user ``PLAN_GEN_KEY``
    state so ``GET /plan`` can show progress/result. Never raises — failures land in state."""
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user is None:
            return
        try:
            async with user_runtime(session, user) as creds:
                plan = await run_plan_generation(
                    session, user_id=user_id, api_key=creds.anthropic_key, **params)
                # A fresh plan archives the prior one — sync now to remove the old plan's
                # pushed workouts and push the new window (only if the user opted in).
                # Never fail generation over it.
                if user.garmin_sync_enabled:
                    try:
                        await plan_sync.sync_plan_to_garmin(session, user_id)
                    except Exception:
                        logger.exception(f"PLAN gen sync failed user={user_id}")
            await repository.set_state(session, user_id, PLAN_GEN_KEY, "")  # done
            logger.info(f"PLAN created id={plan.id} user={user_id} (background)")
        except AnalystError as e:
            logger.warning(f"PLAN generate failed user={user_id}: {e}")
            await repository.set_state(session, user_id, PLAN_GEN_KEY, f"err:{str(e)[:200]}")
        except Exception:
            logger.exception(f"PLAN background generation crashed user={user_id}")
            await repository.set_state(session, user_id, PLAN_GEN_KEY, "err:Внутрішня помилка.")


def _pending_stale(state: str) -> bool:
    """True if a ``pending:<epoch>`` marker is older than the staleness window (or has
    no parseable timestamp) — i.e. the background job likely died."""
    ts = state.split(":", 1)[1] if ":" in state else ""
    try:
        return time.time() - int(ts) > PLAN_GEN_STALE_S
    except ValueError:
        return True


def _spawn_plan_generation(user_id: int, params: dict) -> None:
    """Fire-and-forget the background generation, keeping a reference so it isn't GC'd."""
    task = asyncio.create_task(_generate_plan_bg(user_id, params))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


@router.get("/plan", response_class=HTMLResponse)
async def plan_page(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    # A background generation is in flight → show the waiting page (auto-refreshes),
    # unless it's gone stale (worker died) — then fall through as an error.
    gen = await repository.get_state(session, user.id, PLAN_GEN_KEY) or ""
    error = request.query_params.get("error")
    if gen.startswith("pending"):
        if not _pending_stale(gen):
            return templates.TemplateResponse(request, "plan_generating.html", {"user": user})
        logger.warning(f"PLAN generation went stale user={user.id} — falling back to form")
        await repository.set_state(session, user.id, PLAN_GEN_KEY, "")
        error = error or "gen"
    elif gen.startswith("err:"):
        await repository.set_state(session, user.id, PLAN_GEN_KEY, "")  # consume once
        error = error or "gen"

    plan = await repository.get_active_plan(session, user.id)
    if plan is None:
        return templates.TemplateResponse(
            request, "plan_setup.html",
            {"user": user, "goals": GOALS, "weekdays": WEEKDAYS,
             "default_days": ["tue", "thu", "sun"], "default_long": "sun",
             "today": dt.date.today().isoformat(),
             "garmin_sync_enabled": user.garmin_sync_enabled,
             "error": error},
        )
    workouts = await repository.list_workouts(session, plan.id)
    return templates.TemplateResponse(
        request, "plan.html",
        {"user": user, "plan": plan, "weeks": _by_week(workouts),
         "weekdays": WEEKDAYS, "today": dt.date.today().isoformat(),
         "created": request.query_params.get("created") == "1",
         "count": len(workouts), "readonly": False},
    )


@router.get("/plan/archive", response_class=HTMLResponse)
async def plan_archive_list(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    plans = await repository.list_plans(session, user.id, status="archived")
    return templates.TemplateResponse(
        request, "plan_archive.html", {"user": user, "plans": plans},
    )


@router.get("/plan/{plan_id}", response_class=HTMLResponse)
async def plan_view(
    plan_id: int,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    plan = await repository.get_plan(session, user.id, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    workouts = await repository.list_workouts(session, plan.id)
    return templates.TemplateResponse(
        request, "plan.html",
        {"user": user, "plan": plan, "weeks": _by_week(workouts),
         "weekdays": WEEKDAYS, "today": dt.date.today().isoformat(),
         "count": len(workouts), "readonly": True},
    )


@router.post("/plan")
async def plan_create(
    goal: str = Form(...),
    target_date: str = Form(""),
    run_days: list[str] = Form(default=[]),
    long_run_day: str = Form("sun"),
    intensity: str = Form("moderate"),
    recent_5k: str = Form(""),
    longest_run_km: str = Form(""),
    notes: str = Form(""),
    sync_garmin: str = Form(""),   # checkbox: push this plan to the Garmin calendar
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    if goal not in GOALS:
        return RedirectResponse("/plan?error=goal", status_code=303)
    run_days = [d for d in WEEKDAYS if d in run_days]  # normalise to Mon→Sun order
    if len(run_days) < 2:
        return RedirectResponse("/plan?error=days", status_code=303)
    if long_run_day not in run_days:
        long_run_day = run_days[-1]
    intake = {
        "recent_5k": recent_5k.strip() or None,
        "longest_run_km": longest_run_km.strip() or None,
        "notes": notes.strip() or None,
        "run_days": run_days, "long_run_day": long_run_day,
    }
    # Ignore a duplicate submit while one is already running (and not stale).
    cur = await repository.get_state(session, user.id, PLAN_GEN_KEY) or ""
    if cur.startswith("pending") and not _pending_stale(cur):
        return RedirectResponse("/plan", status_code=303)

    # Generation is a slow Opus call — run it in the background and return immediately,
    # otherwise the gateway times out (504). GET /plan polls the PLAN_GEN_KEY state.
    params = {
        "goal": goal, "goal_label": GOALS[goal],
        "target_date": target_date or None, "start_date": dt.date.today().isoformat(),
        "days_per_week": len(run_days), "intensity": intensity, "intake": intake,
        "run_days": run_days, "long_run_day": long_run_day,
    }
    # Persist the Garmin-sync preference from the form before generation runs (the
    # background task reads it via the DB); set_state's commit persists it too.
    user.garmin_sync_enabled = bool(sync_garmin)
    await repository.set_state(session, user.id, PLAN_GEN_KEY, f"pending:{int(time.time())}")
    logger.info(f"PLAN generate requested user={user.id} goal={goal} days={run_days} "
                f"sync={user.garmin_sync_enabled}")
    _spawn_plan_generation(user.id, params)
    return RedirectResponse("/plan", status_code=303)


@router.post("/plan/archive")
async def plan_archive(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    plan = await repository.get_active_plan(session, user.id)
    if plan:
        await repository.archive_plan(session, plan)
        # Remove the archived plan's pushed workouts from the Garmin calendar now (the
        # daily job would also catch it). Skip if sync is off; don't fail on a Garmin error.
        if user.garmin_sync_enabled:
            try:
                async with user_runtime(session, user):
                    await plan_sync.sync_plan_to_garmin(session, user.id)
            except Exception:
                logger.exception(f"PLAN archive sync failed user={user.id}")
    return RedirectResponse("/plan", status_code=303)
