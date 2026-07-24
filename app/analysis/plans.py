"""Training-plan LLM operations: generation, free-text edits, recovery/weather adaptation
and from-scratch strength sessions.

Everything that produces a structured plan artefact (``GeneratedPlan``/``PlanEdit``/
``StrengthSession``) via Claude, plus the deterministic guards that bound what the model
may do (window + adjust-level + weather-action filters). Split out of the old flat
``analysis.service`` (CODE-01). ``reports`` reuses two small helpers from here
(``_days_to_target``, ``_recent_compliance``) for the weekly digest.
"""
import datetime as dt
import json
import logging
from typing import List, Optional, Tuple

from app.analysis.cache import _build_fitness_snapshot, _build_multisport
from app.analysis.client import (
    MODEL_PLAN,
    MODEL_PLAN_GEN,
    AnalystError,
    CallStats,
    _complete,
    _run_claude,
)
from app.analysis.prompts import (
    SYSTEM_PLAN,
    SYSTEM_PLAN_ADAPT,
    SYSTEM_PLAN_EDIT,
    SYSTEM_SICK,
    SYSTEM_STRENGTH_GEN,
    SYSTEM_WEATHER_PLAN,
)
from app.core.config import settings
from app.garmin import exercises
from app.garmin.schemas import GeneratedPlan, PlanEdit, StrengthSession

# The `general` goal has no target race — an open-ended, continuously-extended plan.
OPEN_ENDED_GOAL = "general"

logger = logging.getLogger("claude")


# ---------- TRAINING PLAN GENERATION ----------

def _coerce_plan(text: str) -> GeneratedPlan:
    """Parse Claude's reply into a GeneratedPlan, tolerating ``` fences / surrounding
    prose by slicing to the outermost {...}."""
    s = text.strip()
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        s = s[i:j + 1]
    return GeneratedPlan(**json.loads(s))


def _block_end(start_date: Optional[str], weeks: int) -> Optional[str]:
    """ISO end date (inclusive last day) of an ``weeks``-long block from ``start_date``."""
    try:
        s = dt.date.fromisoformat(start_date or "")
    except (ValueError, TypeError):
        return None
    return (s + dt.timedelta(weeks=weeks) - dt.timedelta(days=1)).isoformat()


_WD_SLUGS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _weeks_span(start: Optional[str], end: Optional[str]) -> int:
    """Inclusive week count covered by ``[start, end]`` — mirrors the numbering
    ``repository.add_strength_workouts`` computes internally, so a generated progression's
    length lines up with how many weekly occurrences actually get placed."""
    try:
        s = dt.date.fromisoformat(start or "")
        e = dt.date.fromisoformat(end or "")
    except (ValueError, TypeError):
        return 1
    if e < s:
        return 1
    return (e - s).days // 7 + 1


def _fill_progression_gaps(sessions: list) -> list:
    """EP-03: forward/back-fill a week-by-week progression's holes (a week whose session
    didn't survive ``_sanitize_strength``) with the nearest valid neighbour, so one bad
    week never leaves a silent gap mid-block. All-``None`` stays all-``None``."""
    filled = list(sessions)
    last = None
    for i, s in enumerate(filled):
        if s is None:
            filled[i] = last
        else:
            last = s
    nxt = None
    for i in range(len(filled) - 1, -1, -1):
        if filled[i] is None:
            filled[i] = nxt
        else:
            nxt = filled[i]
    return filled


def _existing_custom_strength(workouts, intake) -> dict:
    """Weekday slug → an already-generated ``strength_plan`` from the plan's current custom
    strength rows, so an extension reuses them verbatim (no Claude call). Only weekdays the
    intake marks as free-text "custom" are returned."""
    wanted = set(((((intake or {}).get("strength")) or {}).get("custom") or {}).keys())
    if not wanted:
        return {}
    out: dict = {}
    for w in workouts:
        if (w.type or "") != "strength" or not getattr(w, "strength_plan", None):
            continue
        try:
            slug = _WD_SLUGS[dt.date.fromisoformat(w.date).weekday()]
        except (ValueError, TypeError):
            continue
        if slug in wanted and slug not in out:
            out[slug] = w.strength_plan
    return out


async def _add_plan_strength(
    session, plan, *, intake, fitness, api_key, model,
    start: Optional[str] = None, end: Optional[str] = None,
    week_offset: int = 0, reuse_only: bool = False,
) -> int:
    """Lay the plan's opt-in strength sessions on their weekdays (best-effort — never fails
    plan creation). Two sources: saved Garmin Day 1/Day 2 workouts (snapshotted here, cloned
    on push) and free-text "інше…" sessions built from scratch. ``start``/``end`` bound the
    window (an extension passes the new block); ``week_offset`` continues week numbering.
    Confirmed setup-form previews (``custom_generated``) are reused verbatim, skipping the
    Claude call — and, since a preview is inherently a single approved session, that
    weekday stays the pre-EP-03 "same session every week" shape rather than progressing
    (the user previewed exactly that session, nothing else). With ``reuse_only`` a
    free-text weekday with no reusable session is skipped rather than regenerated (used by
    extensions, which reuse the first block's sessions). Otherwise (fresh generation, no
    confirmed preview), EP-03: a free-text weekday's session PROGRESSES week to week —
    ``generate_strength_progression_with_stats`` returns one session per week (weight/reps
    growth + a deload every 4th week) when the window spans more than one week."""
    from app.garmin import repository

    strength = (intake or {}).get("strength") or {}
    assignments = strength.get("assignments") or {}
    custom = strength.get("custom") or {}
    custom_generated = strength.get("custom_generated") or {}
    if not (strength.get("enabled") and (assignments or custom)):
        return 0
    weeks_span = _weeks_span(start or plan.start_date, end or plan.target_date)
    try:
        from fastapi.concurrency import run_in_threadpool

        from app.garmin import client, workout_export
        amap: dict = {}
        snapshots: dict = {}
        if assignments:
            workouts = await run_in_threadpool(client.fetch_workouts)
            if not workouts:
                logger.warning(f"PLAN strength snapshot empty: fetch_workouts() plan={plan.id}")
            saved = {w["id"]: workout_export.clean_workout_name(w["name"]) for w in workouts}
            amap = {slug: {"id": wid, "name": saved.get(wid) or "Силова"}
                    for slug, wid in assignments.items()}
            # Snapshot each chosen template's exercises + name NOW (Garmin is bound here),
            # so /plan renders the accordion from the DB instead of re-fetching per load.
            for tid in set(assignments.values()):
                raw = await run_in_threadpool(client.fetch_workout_full, tid)
                if raw:
                    snapshots[tid] = {
                        "name": (raw.get("workoutName") or "").strip() or None,
                        "exercises": workout_export.read_exercises(raw),
                        "blocks": workout_export.read_blocks(raw),  # supersets/sets/rest
                    }
                else:
                    logger.warning(f"PLAN strength snapshot empty tid={tid} plan={plan.id}")
        # Generate each distinct free-text session once, sanitise categories, and lay it on
        # its weekday as a from-scratch strength_plan (built natively on push).
        custom_plans: dict = {}
        gen_cache: dict = {}
        for slug, desc in custom.items():
            key = (desc or "").strip().lower()
            if not key:
                continue
            # A confirmed preview / reused session for this weekday → skip the Claude call.
            pre = repository._sanitize_strength(custom_generated.get(slug)) \
                if custom_generated.get(slug) else None
            if pre:
                custom_plans[slug] = pre
                continue
            if reuse_only:
                continue   # extension with nothing to reuse — skip rather than pay
            if key not in gen_cache:
                try:
                    if weeks_span > 1:
                        sessions, _ = await _run_claude(
                            generate_strength_progression_with_stats,
                            {"description": desc, "fitness": fitness or None,
                             "exercise_categories": exercises.CATEGORIES,
                             "weeks": weeks_span},
                            api_key, model)
                        progression = _fill_progression_gaps(
                            [repository._sanitize_strength(s) for s in sessions])
                        gen_cache[key] = progression if any(progression) else None
                    else:
                        sess, _ = await _run_claude(
                            generate_strength_with_stats,
                            {"description": desc, "fitness": fitness or None,
                             "exercise_categories": exercises.CATEGORIES},
                            api_key, model)
                        gen_cache[key] = repository._sanitize_strength(sess)
                except Exception:
                    logger.exception(f"PLAN strength gen failed plan={plan.id}")
                    gen_cache[key] = None
            if gen_cache[key]:
                custom_plans[slug] = gen_cache[key]
        n = await repository.add_strength_workouts(
            session, plan, amap, snapshots, custom_plans,
            start=start, end=end, week_offset=week_offset)
        logger.info(f"PLAN strength plan={plan.id}: +{n} sessions "
                    f"({len(amap)} saved, {len(custom_plans)} custom)")
        return n
    except Exception:
        logger.exception(f"PLAN strength add failed plan={plan.id}")
        return 0


def generate_plan_with_stats(
    context: dict, api_key: Optional[str] = None, model: Optional[str] = None
) -> Tuple[GeneratedPlan, CallStats]:
    """Generate a structured training plan. Returns (GeneratedPlan, stats); one retry
    with a stricter JSON nudge before giving up. Raises AnalystError on API/parse failure.
    Not dedup-cached — dates are relative to today, so every generation is fresh.
    ``model`` picks the engine (Opus default, Fable via the form toggle)."""
    model = model or MODEL_PLAN_GEN
    text, stats = _complete(model, SYSTEM_PLAN, context, "plan", api_key, max_tokens=16000)
    try:
        return _coerce_plan(text), stats
    except Exception:
        retry = dict(context, _note="Поверни ЛИШЕ валідний JSON за схемою, без тексту навколо.")
        text, stats2 = _complete(model, SYSTEM_PLAN, retry, "plan", api_key, max_tokens=16000)
        stats.input_tokens += stats2.input_tokens
        stats.output_tokens += stats2.output_tokens
        stats.cost_usd += stats2.cost_usd
        try:
            return _coerce_plan(text), stats
        except Exception as e:
            logger.error(f"PLAN parse failed: {e}")
            raise AnalystError(
                "Не вдалось згенерувати план (некоректна відповідь). Спробуй ще раз."
            )


def generate_strength_with_stats(
    context: dict, api_key: Optional[str] = None, model: Optional[str] = None
) -> Tuple[StrengthSession, CallStats]:
    """Generate ONE from-scratch strength session from a free-text description (the setup
    form's "інше…" option). Returns (StrengthSession, stats); one retry on a parse miss.
    Raises AnalystError on failure. Categories are validated later by _sanitize_strength."""
    model = model or MODEL_PLAN_GEN

    def _parse(t: str) -> StrengthSession:
        s = t.strip()
        i, j = s.find("{"), s.rfind("}")
        if i != -1 and j != -1:
            s = s[i:j + 1]
        return StrengthSession(**json.loads(s))

    text, stats = _complete(model, SYSTEM_STRENGTH_GEN, context, "plan", api_key, max_tokens=1500)
    try:
        return _parse(text), stats
    except Exception:
        retry = dict(context, _note="Поверни ЛИШЕ валідний JSON сесії за схемою.")
        text, st2 = _complete(model, SYSTEM_STRENGTH_GEN, retry, "plan", api_key, max_tokens=1500)
        stats.input_tokens += st2.input_tokens
        stats.output_tokens += st2.output_tokens
        stats.cost_usd += st2.cost_usd
        try:
            return _parse(text), stats
        except Exception as e:
            logger.error(f"STRENGTH gen parse failed: {e}")
            raise AnalystError("Не вдалось згенерувати силову з опису. Спробуй інакше.")


def generate_strength_progression_with_stats(
    context: dict, api_key: Optional[str] = None, model: Optional[str] = None,
) -> Tuple[List[StrengthSession], CallStats]:
    """EP-03: generate a WEEK-BY-WEEK progression of strength sessions from a free-text
    description — ``context["weeks"]`` sets how many (weight/reps growth, deload every
    4th week; see SYSTEM_STRENGTH_GEN). Returns (one StrengthSession per week, stats).

    Degrades gracefully rather than erroring: a reply with fewer sessions than ``weeks``
    (or the old single-session shape, if the model ignores the array format) is padded by
    repeating the last/only session — the pre-EP-03 "same session every week" behaviour,
    never a failed generation. Raises AnalystError only when nothing parses at all."""
    model = model or MODEL_PLAN_GEN
    weeks = max(1, int(context.get("weeks") or 1))

    def _parse(t: str) -> List[StrengthSession]:
        s = t.strip()
        i, j = s.find("{"), s.rfind("}")
        if i != -1 and j != -1:
            s = s[i:j + 1]
        data = json.loads(s)
        if isinstance(data, dict) and isinstance(data.get("weeks"), list) and data["weeks"]:
            sessions = [StrengthSession(**w) for w in data["weeks"]]
        else:
            sessions = [StrengthSession(**data)]  # model returned one session — replicate it
        if len(sessions) < weeks:
            sessions = sessions + [sessions[-1]] * (weeks - len(sessions))
        return sessions[:weeks]

    # Progression responses are N sessions long — give it real room (same ceiling as the
    # main plan generation, an equally infrequent Opus-affordable call).
    text, stats = _complete(model, SYSTEM_STRENGTH_GEN, context, "plan", api_key,
                            max_tokens=16000)
    try:
        return _parse(text), stats
    except Exception:
        retry = dict(context, _note=(
            "Поверни ЛИШЕ валідний JSON за схемою — масив weeks довжиною "
            f"{weeks}, якщо weeks>1 у вхідних даних."))
        text, st2 = _complete(model, SYSTEM_STRENGTH_GEN, retry, "plan", api_key,
                              max_tokens=16000)
        stats.input_tokens += st2.input_tokens
        stats.output_tokens += st2.output_tokens
        stats.cost_usd += st2.cost_usd
        try:
            return _parse(text), stats
        except Exception as e:
            logger.error(f"STRENGTH progression gen parse failed: {e}")
            raise AnalystError("Не вдалось згенерувати прогресію силової з опису. Спробуй інакше.")


async def run_plan_generation(
    session, *, user_id: int, goal: str, goal_label: Optional[str],
    target_date: Optional[str], start_date: Optional[str], days_per_week: Optional[int],
    intensity: Optional[str], intake: Optional[dict], api_key: Optional[str] = None,
    run_days: Optional[list] = None, long_run_day: Optional[str] = None,
    model: Optional[str] = None,
):
    """Build context, generate the plan, persist it (archiving any active plan), log a
    ReportLog(kind="plan"), and return the new TrainingPlan. ``model`` selects the
    generation engine (Opus default; Fable via the setup-form toggle)."""
    gen_model = model or MODEL_PLAN_GEN
    from app.garmin import repository

    # The `general` goal is open-ended: never pin the plan to a race date; instead plan a
    # first block of PLAN_BLOCK_WEEKS weeks (the daily job auto-extends it later). The model
    # gets a concrete block-end as its range + an open_ended flag (no taper) — see SYSTEM_PLAN.
    open_ended = goal == OPEN_ENDED_GOAL
    ctx_target = target_date
    strength_end = target_date
    if open_ended:
        target_date = None
        ctx_target = _block_end(start_date, settings.PLAN_BLOCK_WEEKS)
        strength_end = ctx_target

    recent_runs = [a for a in await repository.list_activities(session, user_id, n=10)
                   if "run" in (a.get("type") or "")]
    recovery = await repository.read_history(session, user_id, days=30)
    weekly_volume = await repository.weekly_run_volume(session, user_id, weeks=8)
    ex = await repository.get_recent_extra(session, user_id, days=21)
    fitness = _build_fitness_snapshot(ex)
    multisport = await _build_multisport(session, user_id)
    context = {
        "today": dt.date.today().isoformat(),
        "goal": goal, "start_date": start_date, "target_date": ctx_target,
        "open_ended": open_ended,
        "days_per_week": days_per_week, "intensity": intensity,
        "run_days": run_days, "long_run_day": long_run_day, "intake": intake,
        "recent_runs": recent_runs, "recovery": recovery[-14:],
        "weekly_volume": weekly_volume or None,
        "fitness": fitness or None,
        "multisport": multisport,
        "season": (intake or {}).get("season") or None,
        "cycling": (intake or {}).get("cycling") or None,
    }
    logger.info(f"PLAN generating user={user_id} goal={goal} ({len(recent_runs)} recent runs)")
    try:
        plan_out, stats = await _run_claude(
            generate_plan_with_stats, context, api_key, gen_model)
    except AnalystError as e:
        await repository.log_report(
            session, user_id=user_id, kind="plan", model=gen_model, ok=False,
            question=goal, error=str(e)[:512],
        )
        raise
    logger.info(f"PLAN parsed user={user_id}: {len(plan_out.workouts)} workouts")
    plan = await repository.create_plan(
        session, user_id, goal=goal, goal_label=goal_label, target_date=target_date,
        start_date=start_date, days_per_week=days_per_week, intensity=intensity,
        intake=intake, summary=plan_out.summary, workouts=plan_out.workouts,
    )
    await _add_plan_strength(
        session, plan, intake=intake, fitness=fitness, api_key=api_key, model=gen_model,
        end=strength_end,
    )
    await repository.log_report(
        session, user_id=user_id, kind=stats.kind, model=stats.model,
        input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd, ok=True, cached=stats.cached,
        question=f"plan: {goal}", report_text=plan_out.summary,
    )
    return plan


async def run_plan_extension(
    session, *, user_id: int, api_key: Optional[str] = None,
    model: Optional[str] = None, weeks: Optional[int] = None,
):
    """Append the next block to an open-ended (``general``) plan, continuing progression
    from where it currently ends — the rolling top-up behind the daily auto-extend job.
    Returns the plan (now longer) or ``None`` when there's no active open-ended plan.
    Appends ``PlannedWorkout`` rows to the SAME plan (never archives) and logs a
    ReportLog(kind="plan"). Best-effort strength: reuses the first block's custom sessions
    and re-clones saved templates onto the new weeks (no extra Claude call)."""
    from app.garmin import repository

    gen_model = model or MODEL_PLAN_GEN
    weeks = weeks or settings.PLAN_BLOCK_WEEKS
    plan = await repository.get_active_plan(session, user_id)
    if plan is None or plan.target_date or plan.goal != OPEN_ENDED_GOAL:
        return None   # only open-ended plans extend

    last = await repository.last_workout_date(session, plan.id)
    anchor = dt.date.today()
    if last:
        try:
            anchor = max(anchor, dt.date.fromisoformat(last))
        except ValueError:
            pass
    new_start = (anchor + dt.timedelta(days=1)).isoformat()
    block_end = _block_end(new_start, weeks)

    intake = plan.intake or {}
    existing = await repository.list_workouts(session, plan.id)
    # The tail of the existing plan so the model continues progression, not restarts.
    tail = [{"date": w.date, "type": w.type, "dist_km": w.dist_km} for w in existing[-18:]]
    week_offset = max((w.week or 0) for w in existing) if existing else 0

    recent_runs = [a for a in await repository.list_activities(session, user_id, n=10)
                   if "run" in (a.get("type") or "")]
    recovery = await repository.read_history(session, user_id, days=30)
    weekly_volume = await repository.weekly_run_volume(session, user_id, weeks=8)
    ex = await repository.get_recent_extra(session, user_id, days=21)
    fitness = _build_fitness_snapshot(ex)
    multisport = await _build_multisport(session, user_id)
    context = {
        "today": dt.date.today().isoformat(),
        "goal": plan.goal, "start_date": new_start, "target_date": block_end,
        "open_ended": True, "extension": True, "previous_weeks": tail,
        "days_per_week": plan.days_per_week, "intensity": plan.intensity,
        "run_days": intake.get("run_days"), "long_run_day": intake.get("long_run_day"),
        "intake": intake,
        "recent_runs": recent_runs, "recovery": recovery[-14:],
        "weekly_volume": weekly_volume or None,
        "fitness": fitness or None, "multisport": multisport,
        "season": intake.get("season") or None,
        "cycling": intake.get("cycling") or None,
    }
    logger.info(f"PLAN extend user={user_id} plan={plan.id} {new_start}..{block_end}")
    try:
        plan_out, stats = await _run_claude(
            generate_plan_with_stats, context, api_key, gen_model)
    except AnalystError as e:
        await repository.log_report(
            session, user_id=user_id, kind="plan", model=gen_model, ok=False,
            question=f"extend: {plan.goal}", error=str(e)[:512],
        )
        raise
    added = await repository.append_workouts(
        session, plan, plan_out.workouts, week_offset=week_offset)
    logger.info(f"PLAN extend user={user_id} plan={plan.id}: +{added} workouts")

    # Extend opt-in strength onto the new block, reusing the first block's custom sessions
    # (no extra Claude call); saved-template picks are re-cloned cheaply.
    reuse = _existing_custom_strength(existing, intake)
    if reuse:
        strength = dict(intake.get("strength") or {}, custom_generated=reuse)
        intake = dict(intake, strength=strength)
    await _add_plan_strength(
        session, plan, intake=intake, fitness=fitness, api_key=api_key, model=gen_model,
        start=new_start, end=block_end, week_offset=week_offset, reuse_only=True,
    )
    await repository.log_report(
        session, user_id=user_id, kind=stats.kind, model=stats.model,
        input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd, ok=True, cached=stats.cached,
        question=f"extend: {plan.goal}", report_text=plan_out.summary,
    )
    return plan


async def run_strength_preview(
    session, *, user_id: int, description: str, api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[dict]:
    """Generate + sanitise ONE from-scratch strength session for the setup form's
    "Прев'ю" button (ST-05) — the same context/model as plan generation, so a confirmed
    preview matches what generation would have produced. Logs a ReportLog(kind="strength")
    so the (paid) call is visible in cost tracking. Returns the stored ``strength_plan``
    dict ({name, warmup_s, blocks}) or None if nothing valid remained after sanitising.
    Not dedup-cached (like the other plan-gen calls)."""
    gen_model = model or MODEL_PLAN_GEN
    from app.garmin import repository

    ex = await repository.get_recent_extra(session, user_id, days=21)
    fitness = _build_fitness_snapshot(ex)
    context = {"description": description, "fitness": fitness or None,
               "exercise_categories": exercises.CATEGORIES}
    try:
        sess, stats = await _run_claude(
            generate_strength_with_stats, context, api_key, gen_model)
    except AnalystError as e:
        await repository.log_report(
            session, user_id=user_id, kind="strength", model=gen_model, ok=False,
            question=f"strength: {description[:120]}", error=str(e)[:512],
        )
        raise
    await repository.log_report(
        session, user_id=user_id, kind="strength", model=stats.model,
        input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd, ok=True, cached=stats.cached,
        question=f"strength: {description[:120]}",
    )
    return repository._sanitize_strength(sess)


def _coerce_edit(text: str) -> PlanEdit:
    s = text.strip()
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        s = s[i:j + 1]
    return PlanEdit(**json.loads(s))


def _plan_ops_with_stats(
    context: dict, api_key: Optional[str], *,
    system: str, kind: str, log_label: str, error_msg: str,
) -> Tuple[PlanEdit, CallStats]:
    """Shared engine for the AST-identical plan_edit / plan_adapt calls (CODE-06):
    build the message, call Claude, parse into a ``PlanEdit`` with one retry, else
    ``AnalystError``. Callers differ only in system prompt, ReportLog ``kind``, the
    ``claude`` log label and the user-facing error. Deliberately un-cached (adaptation
    must not be dedup-cached — see CODE-06)."""
    model = MODEL_PLAN
    text, stats = _complete(model, system, context, kind, api_key, max_tokens=1500)
    try:
        return _coerce_edit(text), stats
    except Exception:
        retry = dict(context, _note="Поверни ЛИШЕ валідний JSON за схемою, без тексту навколо.")
        text, stats2 = _complete(model, system, retry, kind, api_key, max_tokens=1500)
        stats.input_tokens += stats2.input_tokens
        stats.output_tokens += stats2.output_tokens
        stats.cost_usd += stats2.cost_usd
        try:
            return _coerce_edit(text), stats
        except Exception as e:
            logger.error(f"{log_label} parse failed: {e}")
            raise AnalystError(error_msg)


def plan_edit_with_stats(
    context: dict, api_key: Optional[str] = None
) -> Tuple[PlanEdit, CallStats]:
    """Turn a free-text instruction + current workouts into a structured PlanEdit
    (proposed only — not applied). One retry on a parse miss, else AnalystError."""
    return _plan_ops_with_stats(
        context, api_key,
        system=SYSTEM_PLAN_EDIT, kind="plan_edit", log_label="PLAN_EDIT",
        error_msg="Не вдалось зрозуміти зміну. Спробуй переформулювати.",
    )


async def run_plan_edit(session, *, user_id: int, instruction: str, api_key: Optional[str] = None):
    """Propose changes to the active plan from a free-text instruction (does NOT apply —
    the caller confirms first). Returns (plan, PlanEdit). Logs ReportLog(kind="plan_edit")."""
    from fastapi.concurrency import run_in_threadpool

    from app.garmin import repository

    plan = await repository.get_active_plan(session, user_id)
    if plan is None:
        raise AnalystError("Немає активної програми. Створи її на сторінці /plan у вебі.")
    ws = await repository.list_workouts(session, plan.id, upcoming_only=True)
    # Distinct strength templates already in the plan (Day 1/Day 2) — so the model can add
    # a strength day referencing the right saved workout to clone.
    templates: dict = {}
    for w in await repository.list_workouts(session, plan.id):
        if w.type == "strength" and w.garmin_template_id:
            templates.setdefault(w.garmin_template_id, w.description or "Силова")
    # For each template, pull its exercise list (best-effort — a bound provider; a Garmin
    # outage just omits it) so the model can generate a session "similar to Day 1/2 for
    # <focus>" by adapting the real exercises via swap_exercise ops.
    strength_templates = []
    for tid, nm in templates.items():
        entry = {"id": tid, "name": nm}
        try:
            from app.garmin import client, workout_export
            raw = await run_in_threadpool(client.fetch_workout_full, tid)
            if raw:
                entry["exercises"] = workout_export.read_exercises(raw)
                # Full block structure (sets/rest/weight per superset) so "схоже на Day 1"
                # generation can mirror the template's real loading, not just its exercises.
                entry["blocks"] = workout_export.read_blocks(raw)
        except Exception:
            logger.debug(f"template {tid} exercises unavailable", exc_info=True)
        strength_templates.append(entry)
    # Valid exercise-name variants for the categories that appear in the plan's templates —
    # so a swap/generation picks a real Garmin name (not a hallucination that gets dropped
    # to a bare category on save). Bounded to the plan's categories, not the whole catalog.
    variant_cats = {(e.get("category") or "").upper()
                    for t in strength_templates for e in t.get("exercises", [])}
    exercise_variants = {c: v for c in sorted(variant_cats)
                         if c and (v := exercises.exercises_for(c))}
    context = {
        "today": dt.date.today().isoformat(),
        "instruction": instruction,
        "upcoming": [{"date": w.date, "type": w.type, "dist_km": w.dist_km,
                      "description": w.description,
                      "garmin_template_id": w.garmin_template_id} for w in ws],
        "strength_templates": strength_templates,
        # valid Garmin exercise category codes — the vocabulary for both swap_exercise and
        # from-scratch strength generation (always provided so "згенеруй силову" works even
        # when the plan has no strength day yet)
        "exercise_categories": exercises.CATEGORIES,
        # valid exercise-name variants per category in the plan's templates (may be empty
        # without the catalog); an invalid name is otherwise dropped to a bare category
        "exercise_variants": exercise_variants,
    }
    try:
        edit, stats = await _run_claude(plan_edit_with_stats, context, api_key)
    except AnalystError as e:
        await repository.log_report(
            session, user_id=user_id, kind="plan_edit", model=MODEL_PLAN, ok=False,
            question=instruction[:200], error=str(e)[:512],
        )
        raise
    await repository.log_report(
        session, user_id=user_id, kind=stats.kind, model=stats.model,
        input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd, ok=True, cached=stats.cached,
        question=instruction[:200], report_text=edit.summary,
    )
    return plan, edit


# ---------- ADAPTIVE PLAN (EP-02) ----------

ADAPT_WINDOW_DAYS_DEFAULT = 14
ADAPT_COMPLIANCE_WEEKS = 3

# ST-07 adjust level: per-plan bounds on how boldly adaptation may change workouts.
# Stored in TrainingPlan.intake["adjust_level"]; plans predating the field fall back
# to a goal-derived default (a race plan is conservative, a health plan flexible).
ADJUST_LEVELS = ("off", "conservative", "flexible")
ADAPT_TAPER_DAYS = 14              # ≤ this many days to target_date → taper rules
ADAPT_CONS_MOVE_MAX_DAYS = 2       # conservative: move at most this many days
ADAPT_CONS_DIST_MIN_FRAC = 0.7     # conservative: a modify may cut volume ≤30%
ADAPT_TAPER_DIST_MIN_FRAC = 0.85   # taper: only minimal easing (≤15%)


def plan_adjust_level(plan) -> str:
    """The plan's adaptation level, defaulting by goal when unset: a plan with a
    ``target_date`` (race prep) is *conservative*, an open-ended one *flexible*."""
    lvl = ((plan.intake or {}).get("adjust_level") or "").lower()
    if lvl in ADJUST_LEVELS:
        return lvl
    return "conservative" if plan.target_date else "flexible"


def _days_to_target(target_date, today: dt.date):
    try:
        return (dt.date.fromisoformat(target_date) - today).days
    except (TypeError, ValueError):
        return None


def _filter_ops_to_level(ops: list, level: str, dist_by_date: dict, days_to_target) -> list:
    """Drop operations that exceed the plan's adjust level — the guard behind the
    prompt (the model may overstep; ops outside the bounds must never reach the
    confirm buttons, same idea as ``_filter_ops_to_window``).

    conservative: only ``modify`` (volume cut ≤30% of the planned distance) and
    ``move`` by ≤2 days; within the taper (≤``ADAPT_TAPER_DAYS`` to target) moves are
    dropped too and a cut may be ≤15%. flexible: anything goes (window filter only).
    """
    if level == "flexible":
        return ops
    if level != "conservative":       # "off" never reaches the model; fail closed
        return []
    taper = days_to_target is not None and 0 <= days_to_target <= ADAPT_TAPER_DAYS
    min_frac = ADAPT_TAPER_DIST_MIN_FRAC if taper else ADAPT_CONS_DIST_MIN_FRAC
    kept = []
    for op in ops:
        if op.action == "move" and not taper:
            try:
                delta = abs((dt.date.fromisoformat(op.to_date)
                             - dt.date.fromisoformat(op.date)).days)
            except (TypeError, ValueError):
                continue
            if delta <= ADAPT_CONS_MOVE_MAX_DAYS:
                kept.append(op)
        elif op.action == "modify":
            orig = dist_by_date.get(op.date)
            if op.dist_km is not None and orig and op.dist_km < orig * min_frac:
                continue
            kept.append(op)
    return kept


def _recent_compliance(compliance: dict, weeks: int = ADAPT_COMPLIANCE_WEEKS) -> dict:
    """Slice a ``weekly_compliance`` dict down to the most recent ``weeks`` ISO weeks
    (week strings sort lexically in date order)."""
    if not compliance:
        return {}
    return dict(sorted(compliance.items())[-weeks:])


def _in_adapt_window(date_s, today: dt.date, window_days: int) -> bool:
    try:
        d = dt.date.fromisoformat(date_s)
    except (TypeError, ValueError):
        return False
    return today <= d <= today + dt.timedelta(days=window_days)


def _filter_ops_to_window(ops: list, today: dt.date, window_days: int) -> list:
    """Drop operations whose target date falls outside the adaptation window — a
    guardrail so the model can't rewrite the whole plan (see EP-02 pitfalls)."""
    return [op for op in ops if _in_adapt_window(op.date, today, window_days)]


def plan_adapt_with_stats(
    context: dict, api_key: Optional[str] = None
) -> Tuple[PlanEdit, CallStats]:
    """Propose a plan correction (or none) from recovery/compliance signals — same JSON
    schema as ``plan_edit_with_stats`` (``PlanEdit``). One retry on a parse miss."""
    return _plan_ops_with_stats(
        context, api_key,
        system=SYSTEM_PLAN_ADAPT, kind="adapt", log_label="PLAN_ADAPT",
        error_msg="Не вдалось сформувати пропозицію адаптації плану.",
    )


async def run_plan_adaptation(
    session, *, user_id: int, api_key: Optional[str] = None,
    trigger: str = "weekly", window_days: int = ADAPT_WINDOW_DAYS_DEFAULT,
    risk: Optional[dict] = None,
):
    """Look at the active plan's upcoming window, compliance (EP-01) and recovery/load
    signals; propose a correction (empty ``operations`` if the plan is fine). Does NOT
    apply the change — the caller confirms via bot buttons, same as :func:`run_plan_edit`.

    Returns ``(plan, PlanEdit)``, ``(None, None)`` when there's no active plan, or
    ``(plan, None)`` when the plan's adjust level is "off" (no Claude call, no log —
    adaptation is disabled for this plan). Logs ``ReportLog(kind="adapt")`` on every
    real call (even a no-op) so adaptation cost is tracked. ``trigger`` picks the
    prompt framing ("weekly" review of the next ``window_days`` vs a "morning" one-off
    nudge, called with ``window_days=0`` so only today's session is in scope, or
    "deload" — NF-09, an already-confirmed injury/health risk signal turned into a
    concrete correction, called with ``risk`` set);
    ``window_days`` also bounds which operation dates are kept — anything the model
    proposes outside ``today..today+window_days`` is dropped. The plan's adjust level
    (ST-07) further bounds *what* the kept operations may do — see
    :func:`_filter_ops_to_level`. ``risk`` (NF-09, optional) is the pre-computed
    ``{"injury": injury.to_context(...), "health": health.to_context(...)["alerts"]}``
    slice from the zero-LLM detectors — folded into the prompt as already-confirmed
    evidence, not re-derived here.
    """
    from app.garmin import repository

    plan = await repository.get_active_plan(session, user_id)
    if plan is None:
        return None, None
    level = plan_adjust_level(plan)
    if level == "off":
        logger.debug(f"ADAPT skip user={user_id}: adjust_level=off")
        return plan, None

    today = dt.date.today()
    window_end = (today + dt.timedelta(days=window_days)).isoformat()
    ws = [w for w in await repository.list_workouts(session, plan.id, upcoming_only=True)
          if w.date <= window_end]
    compliance = _recent_compliance(await repository.weekly_compliance(session, plan.id))
    ex = await repository.get_recent_extra(session, user_id)
    fitness = _build_fitness_snapshot(ex)
    multisport = await _build_multisport(session, user_id)
    days_to_target = _days_to_target(plan.target_date, today)
    # Subjective check-ins (EP-12 phase 2): rising effort for the same pace / a recurring
    # niggle is a reason to ease even when the objective load looks fine. Not cached (adapt
    # is deliberately un-cached), so no cache-key wiring needed.
    from app import subjective as subjective_mod
    subj_runs = await repository.recent_subjective_runs(
        session, user_id, days=subjective_mod.WINDOW_DAYS)
    # Step-level plan-vs-actual (NF-14): a low hit-rate on structured sessions is a
    # calibration signal (targets are set too fast/slow), not the same thing as a missed
    # or partial session — feed it alongside compliance, not instead of it.
    from app import stepmatch
    step_match = stepmatch.aggregate(await repository.recent_step_match(session, plan.id))
    context = {
        "today": today.isoformat(),
        "trigger": trigger,
        "window_days": window_days,
        "adjust_level": level,
        "target_date": plan.target_date,
        "days_to_target": days_to_target,
        "upcoming": [{"date": w.date, "type": w.type, "dist_km": w.dist_km,
                      "description": w.description} for w in ws],
        "compliance": compliance or None,
        "fitness": fitness or None,
        "multisport": multisport,
        "season": (plan.intake or {}).get("season") or None,
        "subjective": subjective_mod.summarize(subj_runs),
        "step_match": step_match,
        "risk": risk or None,
    }
    try:
        edit, stats = await _run_claude(plan_adapt_with_stats, context, api_key)
    except AnalystError as e:
        await repository.log_report(
            session, user_id=user_id, kind="adapt", model=MODEL_PLAN, ok=False,
            question=f"adapt:{trigger}", error=str(e)[:512],
        )
        raise
    dist_by_date = {w.date: w.dist_km for w in ws}
    edit.operations = _filter_ops_to_level(
        _filter_ops_to_window(edit.operations, today, window_days),
        level, dist_by_date, days_to_target)
    if edit.alt_operations:
        edit.alt_operations = _filter_ops_to_level(
            _filter_ops_to_window(edit.alt_operations, today, window_days),
            level, dist_by_date, days_to_target)
    await repository.log_report(
        session, user_id=user_id, kind=stats.kind, model=stats.model,
        input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd, ok=True, cached=stats.cached,
        question=f"adapt:{trigger}", report_text=edit.summary,
    )
    return plan, edit


# ---------- WEATHER-AWARE PLANNING (EP-13) ----------

WEATHER_CONTEXT_DAYS = 7          # how far ahead the forecast context reaches
_WEATHER_ALLOWED_ACTIONS = {"move", "modify"}   # never skip/add for weather


def _filter_weather_ops(ops: list, today: dt.date, decision_days: int) -> list:
    """Keep only move/modify operations dated within the decision window — the guard
    behind the prompt (EP-02/EP-13 pitfall: the model may overstep). Weather is never a
    reason to cancel (skip) or invent (add) a session."""
    return [op for op in _filter_ops_to_window(ops, today, decision_days)
            if op.action in _WEATHER_ALLOWED_ACTIONS]


def weather_plan_with_stats(
    context: dict, api_key: Optional[str] = None
) -> Tuple[PlanEdit, CallStats]:
    """Propose a weather-driven plan correction (or none) — same JSON schema as the plan
    edit/adapt calls (``PlanEdit``). One retry on a parse miss."""
    model = MODEL_PLAN
    text, stats = _complete(
        model, SYSTEM_WEATHER_PLAN, context, "weather", api_key, max_tokens=1500)
    try:
        return _coerce_edit(text), stats
    except Exception:
        retry = dict(context, _note="Поверни ЛИШЕ валідний JSON за схемою, без тексту навколо.")
        text, stats2 = _complete(
            model, SYSTEM_WEATHER_PLAN, retry, "weather", api_key, max_tokens=1500
        )
        stats.input_tokens += stats2.input_tokens
        stats.output_tokens += stats2.output_tokens
        stats.cost_usd += stats2.cost_usd
        try:
            return _coerce_edit(text), stats
        except Exception as e:
            logger.error(f"WEATHER parse failed: {e}")
            raise AnalystError("Не вдалось сформувати погодну пропозицію.")


async def run_weather_plan_check(
    session, *, user_id: int, forecast: list, conflicts: list,
    decision_days: int, api_key: Optional[str] = None,
):
    """Given a pre-computed weather ``conflicts`` list (a key session on an extreme day),
    ask Claude to propose a minimal move/modify. Callers must only invoke this when
    ``conflicts`` is non-empty (so the no-conflict path stays silent + free — EP-13 AC).

    Returns ``(plan, PlanEdit)``, or ``(None, None)`` when there's no active plan. Ops are
    filtered to move/modify within ``today..today+decision_days`` (never skip/add — weather
    doesn't cancel training). Logs ``ReportLog(kind="weather")``. Does NOT apply the change
    — the caller confirms via the same bot buttons as EP-02 adaptation."""
    from app.garmin import repository

    plan = await repository.get_active_plan(session, user_id)
    if plan is None:
        return None, None

    today = dt.date.today()
    window_end = (today + dt.timedelta(days=WEATHER_CONTEXT_DAYS)).isoformat()
    ws = [w for w in await repository.list_workouts(session, plan.id, upcoming_only=True)
          if w.date <= window_end]
    context = {
        "today": today.isoformat(),
        "decision_days": decision_days,
        "upcoming": [{"date": w.date, "type": w.type, "dist_km": w.dist_km,
                      "description": w.description} for w in ws],
        "forecast": forecast,
        "conflicts": conflicts,
    }
    try:
        edit, stats = await _run_claude(weather_plan_with_stats, context, api_key)
    except AnalystError as e:
        await repository.log_report(
            session, user_id=user_id, kind="weather", model=MODEL_PLAN, ok=False,
            question="weather", error=str(e)[:512],
        )
        raise
    edit.operations = _filter_weather_ops(edit.operations, today, decision_days)
    edit.alt_operations = None   # weather proposals are already the safe option
    await repository.log_report(
        session, user_id=user_id, kind=stats.kind, model=stats.model,
        input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd, ok=True, cached=stats.cached,
        question="weather", report_text=edit.summary,
    )
    return plan, edit


# ---------- SICK / TRAVEL MODE (NF-03) ----------

SICK_WINDOW_DAYS = 14        # how far ahead the rebuild may touch dates
SICK_LOOKBACK_DAYS = 14      # how far back an already-missed (still "planned") date may be
_SICK_ALLOWED_ACTIONS = {"move", "modify", "skip"}   # never add/swap_exercise


def _filter_sick_ops(ops: list, today: dt.date) -> list:
    """Keep only move/modify/skip operations dated within
    today-SICK_LOOKBACK_DAYS..today+SICK_WINDOW_DAYS — a rebuild touches the current
    block, never the whole plan (mirrors the weather/adapt window guards)."""
    lo = today - dt.timedelta(days=SICK_LOOKBACK_DAYS)
    hi = today + dt.timedelta(days=SICK_WINDOW_DAYS)
    kept = []
    for op in ops:
        if op.action not in _SICK_ALLOWED_ACTIONS:
            continue
        try:
            d = dt.date.fromisoformat(op.date)
        except (TypeError, ValueError):
            continue
        if lo <= d <= hi:
            kept.append(op)
    return kept


def sick_with_stats(
    context: dict, api_key: Optional[str] = None
) -> Tuple[PlanEdit, CallStats]:
    """Propose a sick/travel-mode block rebuild — same JSON schema as the other plan-ops
    calls (``PlanEdit``). One retry on a parse miss."""
    return _plan_ops_with_stats(
        context, api_key,
        system=SYSTEM_SICK, kind="sick", log_label="SICK",
        error_msg="Не вдалось сформувати пропозицію перебудови плану.",
    )


async def run_sick_check(
    session, *, user_id: int, api_key: Optional[str] = None, days_missed: int = 0,
):
    """NF-03: rebuild the current plan block after illness/travel — shift the long run,
    cancel this week's intensity, re-ramp by the 10%-rule. Triggered by the ``/sick``
    command (``days_missed`` from free text, 0 if unspecified) or, in the future, an
    EP-08-style detector. Returns ``(plan, PlanEdit)``, or ``(None, None)`` when there's
    no active plan. Does NOT apply the change — the caller confirms via the same bot
    buttons as a regular plan edit. Logs ``ReportLog(kind="sick")``. Not dedup-cached
    (like the other plan-ops calls) — a repeat call may see fresher upcoming workouts."""
    from app.garmin import repository

    plan = await repository.get_active_plan(session, user_id)
    if plan is None:
        return None, None

    today = dt.date.today()
    lo = (today - dt.timedelta(days=SICK_LOOKBACK_DAYS)).isoformat()
    hi = (today + dt.timedelta(days=SICK_WINDOW_DAYS)).isoformat()
    ws = [w for w in await repository.list_workouts(session, plan.id)
          if lo <= w.date <= hi and w.status != "done"]
    days_to_target = _days_to_target(plan.target_date, today)
    context = {
        "today": today.isoformat(),
        "days_missed": max(0, days_missed),
        "window_days": SICK_WINDOW_DAYS,
        "target_date": plan.target_date,
        "days_to_target": days_to_target,
        "upcoming": [{"date": w.date, "type": w.type, "dist_km": w.dist_km,
                      "description": w.description} for w in ws],
    }
    try:
        edit, stats = await _run_claude(sick_with_stats, context, api_key)
    except AnalystError as e:
        await repository.log_report(
            session, user_id=user_id, kind="sick", model=MODEL_PLAN, ok=False,
            question=f"sick:{days_missed}", error=str(e)[:512],
        )
        raise
    edit.operations = _filter_sick_ops(edit.operations, today)
    edit.alt_operations = None   # sick-mode proposals are already the conservative option
    await repository.log_report(
        session, user_id=user_id, kind=stats.kind, model=stats.model,
        input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd, ok=True, cached=stats.cached,
        question=f"sick:{days_missed}", report_text=edit.summary,
    )
    return plan, edit
