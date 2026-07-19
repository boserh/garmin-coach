"""Training-plan setup + view (web).

A logged-in user picks a goal and a few intake answers (``GET/POST /plan``); we ask
Claude to generate a dated program (``app.analysis.service.run_plan_generation``) and
store it. Day-to-day adjustments happen in the bot (free text). One active plan per user.
"""
import asyncio
import datetime as dt
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.service import (
    ADJUST_LEVELS,
    AnalystError,
    plan_adjust_level,
    resolve_plan_model,
    run_plan_generation,
    run_strength_preview,
)
from app.core.auth import current_user
from app.db.base import async_session_maker
from app.db.models import User
from app.dependencies import get_session
from app.garmin import exercises as _exercises
from app.garmin import plan_sync, repository
from app.garmin.credentials import load_credentials
from app.garmin.runtime import user_runtime


def _desc_hash(desc: str) -> str:
    """Stable short hash of a normalised strength description — ties a previewed session
    to the exact text it was generated from, so an edited description invalidates it (ST-05)."""
    return hashlib.sha256((desc or "").strip().lower().encode("utf-8")).hexdigest()[:16]


async def _confirmed_previews(request: Request, custom: dict) -> dict:
    """ST-05: pull the previewed strength sessions (hidden inputs) whose description hash
    still matches the submitted text — an edited description no longer matches, so its stale
    preview is dropped and the session regenerates. Every session is re-sanitised here: the
    JSON came back from the browser and is never trusted. Returns {weekday_slug: plan_dict}."""
    form = await request.form()
    out: dict = {}
    for slug, desc in custom.items():
        pv = form.get(f"strength_preview_{slug}")
        ph = form.get(f"strength_prehash_{slug}")
        if not (pv and ph) or ph != _desc_hash(desc):
            continue
        try:
            parsed = json.loads(pv)
        except (ValueError, TypeError):
            continue
        san = repository._sanitize_strength(parsed)
        if san:
            out[slug] = san
    return out

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
    zone = s.get("hr_zone")
    pace = s.get("pace_min_km")
    target_str = ""
    if isinstance(zone, int) and 1 <= zone <= 5:
        target_str = f" @ пульс зона {zone}"
    elif isinstance(pace, (list, tuple)) and len(pace) == 2 and all(
            isinstance(p, (int, float)) for p in pace):
        target_str = f" @ {_pace(pace[0])}–{_pace(pace[1])}/км"
    return " ".join(p for p in (label, amount) if p) + target_str


def _pace(dec: float) -> str:
    """Decimal min/km → m:ss (6.75 → 6:45)."""
    total = round(dec * 60)
    return f"{total // 60}:{total % 60:02d}"


# Fallback pace (min/km) for a distance step that carries no pace target of its own
# (e.g. an easy run prescribed by HR zone) — used only if the workout has no pace at all.
_DEFAULT_PACE_MIN_KM = 6.5


def _step_mid_pace(s: dict) -> Optional[float]:
    """Midpoint of a step's ``pace_min_km`` range, or None."""
    p = s.get("pace_min_km")
    if isinstance(p, (list, tuple)) and len(p) == 2 and all(
            isinstance(x, (int, float)) for x in p):
        return (float(p[0]) + float(p[1])) / 2
    return None


def _first_pace(steps) -> Optional[float]:
    """First pace midpoint found anywhere in the step tree (a per-workout fallback so a
    warmup/easy step with only an HR zone still gets estimated at the run's own pace)."""
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        m = _step_mid_pace(s)
        if m is not None:
            return m
        m = _first_pace(s.get("steps"))
        if m is not None:
            return m
    return None


def _steps_seconds(steps, fallback: float) -> float:
    """Sum the estimated time (seconds) of a step tree: ``dur_s`` verbatim, a distance
    step as dist × pace, and a repeat group as reps × its inner time."""
    total = 0.0
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        if s.get("kind") == "repeat":
            reps = s.get("reps")
            reps = reps if isinstance(reps, (int, float)) else 1
            total += reps * _steps_seconds(s.get("steps"), fallback)
            continue
        ds = s.get("dur_s")
        if isinstance(ds, (int, float)):
            total += float(ds)
            continue
        dm = s.get("dist_m")
        if isinstance(dm, (int, float)):
            pace = _step_mid_pace(s)
            total += (float(dm) / 1000.0) * (pace if pace is not None else fallback) * 60.0
    return total


def _est_minutes(steps) -> Optional[int]:
    """Approximate total duration (whole minutes) of a run's structured steps, or None
    when it can't be estimated. Powers the '~NN хв' hint next to the distance."""
    if not steps:
        return None
    fallback = _first_pace(steps) or _DEFAULT_PACE_MIN_KM
    secs = _steps_seconds(steps, fallback)
    if secs <= 0:
        return None
    return int(round(secs / 60.0))


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


_STEP_KIND = {"warmup": "Розминка", "run": "Біг", "recovery": "Відновлення",
              "cooldown": "Заминка", "rest": "Пауза"}


def _step_label(s: dict) -> str:
    """Role label for a structured step (repeat handled in the template)."""
    if not isinstance(s, dict):
        return ""
    return _STEP_KIND.get(s.get("kind"), (s.get("kind") or "").capitalize())


def _step_amount(s: dict) -> str:
    """Distance/time of a step, e.g. '2.7 км' or '20 с'."""
    dm, ds = s.get("dist_m"), s.get("dur_s")
    if isinstance(dm, (int, float)):
        return f"{dm / 1000:.1f} км"
    if isinstance(ds, (int, float)):
        return f"{int(ds // 60)} хв" if ds >= 60 else f"{int(ds)} с"
    return ""


def _step_pace(s: dict) -> str:
    """Target of a step — pace range ('6:45–7:15/км'), HR zone ('пульс зона 2') for
    easy/recovery effort steps, or empty when there's no target."""
    z = s.get("hr_zone")
    if isinstance(z, int) and 1 <= z <= 5:
        return f"пульс зона {z}"
    p = s.get("pace_min_km")
    if isinstance(p, (list, tuple)) and len(p) == 2 and all(
            isinstance(x, (int, float)) for x in p):
        return f"{_pace(p[0])}–{_pace(p[1])}/км"
    return ""


templates.env.filters["fmt_step"] = _fmt_step
templates.env.filters["dow"] = _dow
templates.env.filters["dm"] = _dm
templates.env.filters["step_label"] = _step_label
templates.env.filters["step_amount"] = _step_amount
templates.env.filters["step_pace"] = _step_pace
templates.env.filters["exlabel"] = lambda cat, ex="": _exercises.label(cat or "", ex or "")
templates.env.filters["pace_fmt"] = _pace  # decimal min/km → "m:ss"
templates.env.filters["est_min"] = _est_minutes  # steps → approx whole minutes

logger = logging.getLogger("plan")

router = APIRouter(tags=["plan"])

GOALS = {
    "first_5k": "Перші 5 км",
    "faster_5k": "Швидше 5 км",
    "first_10k": "Перші 10 км",
    "first_half": "Перший півмарафон",
    "general": "Персональний тренер (постійні тренування)",
}

# Goals with no target race: an open-ended plan whose weeks are auto-extended in blocks
# (see app.analysis.plans.run_plan_extension). Kept in sync with plans.OPEN_ENDED_GOAL.
OPEN_ENDED_GOALS = {"general"}

# weekday slug → Ukrainian label (used for the run-day picker)
WEEKDAYS = {
    "mon": "Пн", "tue": "Вт", "wed": "Ср", "thu": "Чт",
    "fri": "Пт", "sat": "Сб", "sun": "Нд",
}

# adjust level slug → Ukrainian label (setup form + plan page badge, ST-07)
ADJUST_LABELS = {
    "conservative": "обережна",
    "flexible": "гнучка",
    "off": "вимкнена",
}


_MONTHS_UK = ["січ", "лют", "бер", "кві", "тра", "чер",
              "лип", "сер", "вер", "жов", "лис", "гру"]


def _fmt_day(d: dt.date) -> str:
    return f"{d.day} {_MONTHS_UK[d.month - 1]}"


def _by_week(workouts):
    """Group workouts into **Monday–Sunday calendar weeks** (by date, not the plan's
    ``week`` field), ordered and numbered sequentially. Returns
    ``[(week_no, "29 чер – 5 лип", iso_week_key, [workouts...]), ...]``
    where ``iso_week_key`` is the '%G-W%V' string for the Monday of that week."""
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
        iso_key = monday.strftime("%G-W%V")
        out.append((i, f"{_fmt_day(monday)} – {_fmt_day(sunday)}", iso_key, weeks[monday]))
    if None in weeks:   # undated (shouldn't happen) — keep them visible at the end
        out.append((len(out) + 1, "", None, weeks[None]))
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


async def _strength_workouts(session, user):
    """The user's saved Garmin strength workouts (Day 1/Day 2) for the setup picker.
    Best-effort — no creds / a Garmin outage just yields [] (strength option hidden)."""
    from fastapi.concurrency import run_in_threadpool

    from app.garmin import client
    from app.garmin.providers import get_provider
    try:
        async with user_runtime(session, user) as creds:
            if not creds.has_garmin:
                return []
            await run_in_threadpool(get_provider().login)
            # Show names verbatim: the user has both a "Day 1" and a "Day 1 manual" —
            # stripping " manual" collapsed them into one label, hiding the distinction.
            return [w for w in await run_in_threadpool(client.fetch_workouts)
                    if (w.get("sport") or "") == "strength_training"]
    except Exception:
        logger.exception(f"strength workouts fetch failed user={user.id}")
        return []


async def _strength_details(session, user, workouts):
    """Exercise lists for the plan view's strength accordion, keyed by workout id.

    - From-scratch days (``strength_plan``) are used directly (blocks with reps/rest/weight).
    - Clone days (``garmin_template_id``) carry a ``strength_snapshot`` cached at build time
      ({name?, exercises}) — served straight from the DB, no Garmin call.
    - Only clone days **without** a snapshot (plans made before snapshots existed) fall back
      to a live template fetch. Returns ``({workout_id: blocks}, {workout_id: name})``.

    Best-effort: only binds Garmin when there are snapshot-less clone days; on any outage the
    maps stay empty and the page still renders (just without the exercise list)."""
    view: dict = {}
    names: dict = {}
    for w in workouts:
        if w.type != "strength":
            continue
        if w.strength_plan and w.strength_plan.get("blocks"):
            view[w.id] = w.strength_plan["blocks"]
        elif w.strength_snapshot and w.strength_snapshot.get("exercises"):
            snap = w.strength_snapshot
            view[w.id] = [{"reps": None, "rest_s": None, "exercises": snap["exercises"]}]
            if snap.get("name"):
                names[w.id] = snap["name"]
    clones = [w for w in workouts
              if w.type == "strength" and w.garmin_template_id
              and not w.strength_plan and not w.strength_snapshot]
    if not clones:
        return view, names

    from fastapi.concurrency import run_in_threadpool

    from app.garmin import client, workout_export
    from app.garmin.providers import get_provider
    cache: dict = {}   # template id → (exercises, name)
    try:
        async with user_runtime(session, user) as creds:
            if not creds.has_garmin:
                return view, names
            await run_in_threadpool(get_provider().login)
            for w in clones:
                tid = w.garmin_template_id
                if tid not in cache:
                    raw = await run_in_threadpool(client.fetch_workout_full, tid)
                    exs = workout_export.read_exercises(raw) if raw else []
                    cache[tid] = (exs, (raw.get("workoutName") or "").strip() if raw else "")
                exs, nm = cache[tid]
                if exs:   # one pseudo-block (no set/rest structure from a bare template read)
                    view[w.id] = [{"reps": None, "rest_s": None, "exercises": exs}]
                if nm:
                    names[w.id] = nm
    except Exception:
        logger.exception(f"strength details fetch failed user={user.id}")
    return view, names


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
             "strength_workouts": await _strength_workouts(session, user),
             "error": error},
        )
    workouts = await repository.list_workouts(session, plan.id)
    strength_view, strength_names = await _strength_details(session, user, workouts)
    compliance = await repository.weekly_compliance(session, plan.id)
    return templates.TemplateResponse(
        request, "plan.html",
        {"user": user, "plan": plan, "weeks": _by_week(workouts),
         "weekdays": WEEKDAYS, "today": dt.date.today().isoformat(),
         "strength_view": strength_view, "strength_names": strength_names,
         "compliance": compliance,
         "adjust_level": plan_adjust_level(plan), "adjust_labels": ADJUST_LABELS,
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
    strength_view, strength_names = await _strength_details(session, user, workouts)
    compliance = await repository.weekly_compliance(session, plan.id)
    return templates.TemplateResponse(
        request, "plan.html",
        {"user": user, "plan": plan, "weeks": _by_week(workouts),
         "weekdays": WEEKDAYS, "today": dt.date.today().isoformat(),
         "strength_view": strength_view, "strength_names": strength_names,
         "compliance": compliance,
         "adjust_level": plan_adjust_level(plan), "adjust_labels": ADJUST_LABELS,
         "count": len(workouts), "readonly": True},
    )


@router.post("/plan/strength/preview", response_class=HTMLResponse)
async def strength_preview(
    request: Request,
    description: str = Form(""),
    plan_model: str = Form("opus"),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """ST-05: generate ONE strength session from the free-text description and render it as
    an HTML fragment (same look as the /plan accordion) WITHOUT submitting the whole form.
    The fragment carries the sanitised session + its description hash so the form can hand a
    confirmed preview back to generation and skip a second (paid) Claude call."""
    desc = description.strip()
    if not desc:
        raise HTTPException(status_code=400, detail="empty description")
    creds = load_credentials(user)
    try:
        sp = await run_strength_preview(
            session, user_id=user.id, description=desc,
            api_key=creds.anthropic_key, model=resolve_plan_model(plan_model),
        )
    except AnalystError as e:
        return templates.TemplateResponse(request, "_strength_preview.html", {"error": str(e)})
    if not sp:
        return templates.TemplateResponse(
            request, "_strength_preview.html",
            {"error": "Не вдалось скласти силову з опису. Спробуй інакше."},
        )
    return templates.TemplateResponse(
        request, "_strength_preview.html",
        {"session": sp, "session_json": json.dumps(sp, ensure_ascii=False),
         "phash": _desc_hash(desc)},
    )


@router.post("/plan")
async def plan_create(
    request: Request,
    goal: str = Form(...),
    target_date: str = Form(""),
    run_days: list[str] = Form(default=[]),
    long_run_day: str = Form("sun"),
    intensity: str = Form("moderate"),
    adjust_level: str = Form(""),     # off | conservative | flexible ("" → goal default)
    plan_model: str = Form("opus"),   # generation engine toggle: opus | fable
    recent_5k: str = Form(""),
    longest_run_km: str = Form(""),
    notes: str = Form(""),
    sync_garmin: str = Form(""),   # checkbox: push this plan to the Garmin calendar
    strength_enabled: str = Form(""),      # checkbox: add strength sessions
    strength_mon: str = Form(""),          # per-weekday: workout id, "custom", or "" (none)
    strength_tue: str = Form(""),
    strength_wed: str = Form(""),
    strength_thu: str = Form(""),
    strength_fri: str = Form(""),
    strength_sat: str = Form(""),
    strength_sun: str = Form(""),
    strength_desc_mon: str = Form(""),     # free-text session for a weekday set to "custom"
    strength_desc_tue: str = Form(""),
    strength_desc_wed: str = Form(""),
    strength_desc_thu: str = Form(""),
    strength_desc_fri: str = Form(""),
    strength_desc_sat: str = Form(""),
    strength_desc_sun: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    if goal not in GOALS:
        return RedirectResponse("/plan?error=goal", status_code=303)
    if goal in OPEN_ENDED_GOALS:
        target_date = ""   # open-ended plan: never pinned to a race date
    run_days = [d for d in WEEKDAYS if d in run_days]  # normalise to Mon→Sun order
    if len(run_days) < 2:
        return RedirectResponse("/plan?error=days", status_code=303)
    if long_run_day not in run_days:
        long_run_day = run_days[-1]
    if adjust_level not in ADJUST_LEVELS:   # unset/garbage → default by goal (ST-07)
        adjust_level = "conservative" if target_date else "flexible"
    intake = {
        "recent_5k": recent_5k.strip() or None,
        "longest_run_km": longest_run_km.strip() or None,
        "notes": notes.strip() or None,
        "run_days": run_days, "long_run_day": long_run_day,
        "adjust_level": adjust_level,
    }
    if strength_enabled:
        picks = {"mon": strength_mon, "tue": strength_tue, "wed": strength_wed,
                 "thu": strength_thu, "fri": strength_fri, "sat": strength_sat,
                 "sun": strength_sun}
        descs = {"mon": strength_desc_mon, "tue": strength_desc_tue, "wed": strength_desc_wed,
                 "thu": strength_desc_thu, "fri": strength_desc_fri, "sat": strength_desc_sat,
                 "sun": strength_desc_sun}
        # weekday slug → chosen saved workout id (skip days left on "— нема —")
        assignments = {slug: int(v) for slug, v in picks.items() if v.isdigit()}
        # weekday slug → free-text description (days set to "інше…" with text) — generated
        # from scratch into a strength_plan during plan generation.
        custom = {slug: descs[slug].strip() for slug, v in picks.items()
                  if v == "custom" and descs[slug].strip()}
        if assignments or custom:
            intake["strength"] = {"enabled": True}
            if assignments:
                intake["strength"]["assignments"] = assignments
            if custom:
                intake["strength"]["custom"] = custom
                # ST-05: carry any confirmed previews so generation reuses them (skip regen).
                gen = await _confirmed_previews(request, custom)
                if gen:
                    intake["strength"]["custom_generated"] = gen
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
        "model": resolve_plan_model(plan_model),
    }
    # Persist the Garmin-sync preference from the form before generation runs (the
    # background task reads it via the DB); set_state's commit persists it too.
    user.garmin_sync_enabled = bool(sync_garmin)
    await repository.set_state(session, user.id, PLAN_GEN_KEY, f"pending:{int(time.time())}")
    logger.info(f"PLAN generate requested user={user.id} goal={goal} days={run_days} "
                f"sync={user.garmin_sync_enabled}")
    _spawn_plan_generation(user.id, params)
    return RedirectResponse("/plan", status_code=303)


@router.post("/plan/adjust-level")
async def plan_set_adjust_level(
    adjust_level: str = Form(...),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Change the active plan's adaptation level without regenerating it (ST-07).
    Takes effect on the next adaptation check — no Garmin/Claude call here."""
    plan = await repository.get_active_plan(session, user.id)
    if plan is not None and adjust_level in ADJUST_LEVELS:
        # Reassign (not mutate) the JSON column so SQLAlchemy sees the change.
        plan.intake = dict(plan.intake or {}, adjust_level=adjust_level)
        await session.commit()
        logger.info(f"PLAN adjust_level={adjust_level} user={user.id} plan={plan.id}")
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
