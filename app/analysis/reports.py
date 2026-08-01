"""Narrative Claude calls: the daily/deep report, ``/ask`` follow-ups, single-activity
analysis, the weekly digest, compare-past-self and the injury-radar advisory.

Everything that turns the compact payload (or already-computed numbers) into a
Ukrainian narration for the user, with the dedup-cache get/put fronting each ``run_*``
wrapper. Split out of the old flat ``analysis.service`` (CODE-01). The plan-adaptation
helpers ``_days_to_target``/``_recent_compliance`` are reused from ``plans`` for the
weekly digest's goal + compliance slices.
"""
import base64
import datetime as dt
import json
import logging
from typing import List, Optional, Tuple, Union

from app import daterel, gap
from app.analysis.cache import (
    CACHE_TTL_S,
    _activity_cache_key,
    _as_dict,
    _ask_cache_key,
    _build_fitness_snapshot,
    _build_multisport,
    _cache_key,
    _checkup_cache_key,
    _compare_cache_key,
    _digest_cache_key,
    _insights_cache_key,
    _race_cache_key,
    _supplement_cache_key,
    _wrapped_cache_key,
)
from app.analysis.client import (
    MODEL_ACTIVITY,
    MODEL_ASK,
    MODEL_CHECKUP,
    MODEL_CHECKUP_OCR,
    MODEL_COMPARE,
    MODEL_DAILY,
    MODEL_DEEP,
    MODEL_DIGEST,
    MODEL_HEALTH,
    MODEL_INJURY,
    MODEL_INSIGHTS,
    MODEL_RACE,
    MODEL_SUPPLEMENTS,
    MODEL_WRAPPED,
    PRICES,
    AnalystError,
    CallStats,
    _complete,
    _complete_tools,
    _complete_vision,
    _get_client,
    _run_claude,
    _status_error,
)
from app.analysis.plans import _days_to_target, _recent_compliance
from app.analysis.prompts import (
    SYSTEM,
    SYSTEM_ACTIVITY,
    SYSTEM_ASK_TOOLS,
    SYSTEM_CHECKUP,
    SYSTEM_CHECKUP_OCR,
    SYSTEM_COMPARE,
    SYSTEM_DIGEST,
    SYSTEM_HEALTH,
    SYSTEM_INJURY,
    SYSTEM_INSIGHTS,
    SYSTEM_RACE,
    SYSTEM_SUPPLEMENTS,
    SYSTEM_WRAPPED,
)
from app.core.config import settings
from app.db.models import HealthCheckup
from app.garmin.schemas import Payload, SupplementAdvice
from app.multisport import sport_bucket

logger = logging.getLogger("claude")


async def _run_cached_narration(
    session, *, user_id: Optional[int], kind: str, model: str, context: dict,
    cache_key: str, with_stats_fn, question: str, api_key: Optional[str] = None,
    force: bool = False,
) -> str:
    """Shared engine for the cached ``run_*`` narrations (A1).

    Every one of ``run_compare``/``run_wrapped``/``run_race_plan``/``run_insights``/
    ``run_digest``/``run_activity_analysis`` had this exact block copied verbatim: check the
    dedup cache, on a miss run ``with_stats_fn(context, api_key)`` on the Claude pool
    (logging an error ReportLog + re-raising on ``AnalystError``), store the text, then log
    the success ReportLog and return the text. The per-report differences (context assembly,
    ``has_signal`` gating, the ``cache_key``/``question`` strings) stay in the thin wrappers;
    only this mechanical tail is centralised. ``*_with_stats`` signatures are untouched — the
    tests monkeypatch them (the CODE-06 lesson).

    ``force=True`` (ST-19) skips the cache **get** (a deliberate "look again" for a paid
    re-run after resynced data / a bad first analysis) but still **writes** the fresh result
    back to the cache and logs a non-cached ReportLog, so the next non-force caller hits it."""
    from app.db import llm_cache
    from app.garmin import repository

    cached = None if force else await llm_cache.get(session, cache_key)
    if cached is not None:
        logger.info(f"CLAUDE CACHE HIT  {model} ({kind})")
        text, stats = cached, CallStats(kind=kind, model=model, cached=True)
    else:
        try:
            text, stats = await _run_claude(with_stats_fn, context, api_key)
        except AnalystError as e:
            await repository.log_report(
                session, user_id=user_id, kind=kind, model=model, ok=False,
                question=question, error=str(e)[:512],
            )
            raise
        await llm_cache.put(session, cache_key, text, CACHE_TTL_S)
    await repository.log_report(
        session, user_id=user_id, kind=stats.kind, model=stats.model,
        input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd, ok=True, cached=stats.cached,
        question=question, report_text=text,
    )
    return text


ASK_DEFAULT_N = 3   # how many recent daily reports to feed as /ask context
ASK_CONTEXT_MIN = 30  # include /ask exchanges from the last N minutes as a conversation thread
RECORDS_CONTEXT_DAYS = 3  # mention a personal record set within the last N days (EP-14)

_DEFAULT_DAILY_Q = (
    "Дай щоденний статус відновлення. "
    "Детальну пораду до пробіжки — лише якщо вона сьогодні/завтра."
)


def _strength_exercises(w) -> Optional[dict]:
    """Compact exercise list for a strength ``PlannedWorkout``, for the report's
    ``plan_today`` (ST-09) — so the analyst narrates the real session instead of guessing
    from history. From-scratch days read ``strength_plan.blocks``; clone days read the
    build-time ``strength_snapshot`` (both display-only, straight from the DB — no Garmin
    call on the report path). Returns ``{name?, exercises:[{category, exercise?, reps?}]}``
    or ``None``. NB the JSON-null gotcha: an empty snapshot deserialises to Python ``None``
    (falsy), so a plain truthiness check filters it."""
    if getattr(w, "type", None) != "strength":
        return None

    def _compact(e: dict) -> dict:
        return {k: v for k, v in (("category", e.get("category")),
                                  ("exercise", e.get("exercise")),
                                  ("reps", e.get("reps"))) if v is not None}

    sp = getattr(w, "strength_plan", None)
    if isinstance(sp, dict) and sp.get("blocks"):
        ex = [_compact(e) for b in sp["blocks"] for e in (b.get("exercises") or [])
              if e.get("category")]
        if ex:
            return {k: v for k, v in (("name", sp.get("name")), ("exercises", ex)) if v}

    snap = getattr(w, "strength_snapshot", None)
    if isinstance(snap, dict) and snap.get("exercises"):
        ex = [_compact(e) for e in snap["exercises"] if e.get("category")]
        if ex:
            return {k: v for k, v in (("name", snap.get("name")), ("exercises", ex)) if v}
    return None


def _labelled_data(data: dict, today: str) -> dict:
    """Payload copy with a relative-day ``day`` label on every dated list (see
    ``app.daterel``) — so the analyst reads "вчора (вт)" off the record instead of
    subtracting dates itself. Copy, never in-place: this dict is shared with the
    payload memo and the cache key."""
    out = dict(data)
    for k in ("daily", "recent_activities", "planned_runs"):
        if isinstance(out.get(k), list):
            out[k] = daterel.annotate(out[k], today)
    return out


def analyze_with_stats(
    payload: Union[Payload, dict],
    question: str = "",
    deep: bool = False,
    kind: Optional[str] = None,
    previous_report: Optional[dict] = None,
    api_key: Optional[str] = None,
    weather: Optional[dict] = None,
    plan_today: Optional[list] = None,
    fitness: Optional[dict] = None,
    records: Optional[list] = None,
    norm: Optional[dict] = None,
    subjective: Optional[dict] = None,
    health_alerts: Optional[dict] = None,
    fueling: Optional[dict] = None,
    today: Optional[str] = None,
) -> Tuple[str, CallStats]:
    """Run analysis and return (text, stats). Raises AnalystError on API failure.

    ``previous_report`` ({"date", "text"}) is yesterday's report passed as context
    for day-over-day continuity (incl. did-the-planned-workout-happen checks). It
    adds ~200-400 input tokens and no output growth.

    ``weather`` (today's compact forecast, see ``app.weather.fetch_forecast``) lets the
    analyst tailor advice for a run today/tomorrow (heat, rain, wind, run timing). Part
    of the cache key so a forecast change yields a fresh report.

    ``today`` (ISO) is the user's OWN today (their timezone, ST-14), not the process's.
    Every dated record in the context is labelled against it in Python (``app.daterel``)
    rather than left to the model's date arithmetic — the day-confusion fix.

    No dedup-cache check here — this runs sync in a threadpool with no DB access;
    :func:`run_analysis` fronts it with the shared ``llm_cache`` get/put.
    """
    model = MODEL_DEEP if deep else MODEL_DAILY
    kind = kind or ("deep" if deep else "report")
    today_iso = today or dt.date.today().isoformat()
    data = _labelled_data(_as_dict(payload), today_iso)
    effective_q = question or _DEFAULT_DAILY_Q

    user_content = {
        **daterel.today_context(today_iso),
        "data": data,
        "question": effective_q,
    }
    if previous_report:
        lab = daterel.label(previous_report.get("date"), today_iso)
        user_content["previous_report"] = (
            {**previous_report, "day": lab} if lab else previous_report
        )
    if weather:
        user_content["weather"] = weather
    if plan_today:
        user_content["plan_today"] = daterel.annotate(plan_today, today_iso)
    if fitness:
        user_content["fitness"] = fitness
    if records:
        user_content["records"] = daterel.annotate(records, today_iso)
    if norm:
        user_content["norm"] = norm
    if subjective:
        user_content["subjective"] = subjective
    if health_alerts:
        user_content["health_alerts"] = health_alerts
    if fueling:
        user_content["fueling"] = fueling
    try:
        from anthropic import APIConnectionError, APIStatusError

        msg = _get_client(api_key).messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM,
            # Sonnet 5 (MODEL_DAILY) runs adaptive thinking by default when `thinking`
            # is omitted (Opus/MODEL_DEEP already runs without it by default either
            # way) — with max_tokens=2000 that can consume the whole budget on
            # thinking and leave nothing for the actual report text (empty response,
            # stop_reason=max_tokens). This is narration over already-computed data,
            # not a reasoning task, so disable thinking explicitly.
            thinking={"type": "disabled"},
            messages=[{"role": "user",
                       "content": json.dumps(user_content, ensure_ascii=False)}],
        )
        stats = CallStats(kind=kind, model=model)
        usage = getattr(msg, "usage", None)
        if usage:
            pin, pout = PRICES.get(model, (0, 0))
            stats.input_tokens = usage.input_tokens
            stats.output_tokens = usage.output_tokens
            stats.cost_usd = usage.input_tokens / 1e6 * pin + usage.output_tokens / 1e6 * pout
            logger.info(
                f"CLAUDE OK  {model}  stop={msg.stop_reason}  "
                f"in={usage.input_tokens} out={usage.output_tokens} "
                f"~${stats.cost_usd:.4f}"
            )
        text = "".join(b.text for b in msg.content if b.type == "text")
        if not text:
            logger.error(f"CLAUDE empty response  model={model} stop={msg.stop_reason} "
                         f"content_types={[b.type for b in msg.content]}")
            raise AnalystError("Порожня відповідь від Claude. Спробуй ще раз.")
        return text, stats

    except APIStatusError as e:
        status = getattr(e, "status_code", None)
        body = str(getattr(e, "message", e)).lower()

        if status == 400 and "credit balance is too low" in body:
            raise AnalystError(
                "❗️ Закінчились кредити Anthropic API.\n"
                "Поповни баланс на console.anthropic.com → Billing і повтори запит."
            )
        if status == 429:
            raise AnalystError("⏳ Ліміт запитів перевищено. Спробуй за хвилину.")
        if status == 401:
            raise AnalystError("🔑 Невірний або відсутній ANTHROPIC_API_KEY.")
        if status == 529:
            raise AnalystError("🛠 Сервіс Anthropic тимчасово перевантажений. Спробуй пізніше.")
        logger.error(f"CLAUDE ERR {status}: {body[:150]}")
        raise AnalystError(f"Помилка API ({status}): {body[:200]}")

    except APIConnectionError:
        raise AnalystError("🌐 Не вдалось з'єднатися з API. Перевір інтернет і спробуй ще.")


async def run_analysis(
    session,
    payload: Union[Payload, dict],
    *,
    user_id: Optional[int] = None,
    question: str = "",
    deep: bool = False,
    kind: Optional[str] = None,
    api_key: Optional[str] = None,
    weather: Optional[dict] = None,
    today: Optional[Union[str, dt.date]] = None,
) -> str:
    """Analyze, persist a ReportLog row (success or failure), return the text.

    Blocking API work runs in a threadpool; the failed-call log is best-effort.
    ``weather`` (optional) is today's forecast passed through to the analyst.

    ``today`` (optional, ISO string or ``date``) is the user's own current date in THEIR
    timezone (``app.core.tz.user_today``) — it decides which plan sessions count as
    today's, which day the fueling advice is for, and the relative labels the analyst
    reads. Defaults to the process date for callers with no user in hand.
    """
    from app.garmin import repository

    model = MODEL_DEEP if deep else MODEL_DAILY
    kind = kind or ("deep" if deep else "report")
    today_d = daterel.parse(today) or dt.date.today()
    today_iso = today_d.isoformat()

    # Day-over-day continuity: feed yesterday's report as context (daily/morning
    # only — /deep is a one-off deep dive that doesn't need it). Fetched before the
    # new ReportLog is written, so it never picks up the report we're about to make.
    previous_report = None
    plan_today = None
    fitness = None
    records = None
    norm = None
    subjective = None
    health_alerts = None
    fueling = None
    if kind != "deep":
        last = await repository.get_last_report(session, user_id)
        if last:
            text_prev, date_prev = last
            previous_report = {"date": date_prev, "text": text_prev}

        if user_id is not None:
            ws = await repository.upcoming_plan_workouts(
                session, user_id, days=2, today=today_d)
            if ws:
                plan_today = [
                    {k: v for k, v in {
                        "date": w.date,
                        "type": w.type,
                        "dist_km": w.dist_km,
                        "description": w.description,
                        "steps": w.steps,
                        "exercises": _strength_exercises(w),
                    }.items() if v is not None}
                    for w in ws
                ]
            ex = await repository.get_recent_extra(session, user_id)
            fitness = _build_fitness_snapshot(ex)
            # Fresh personal records (EP-14) — mention a just-set PB in the report.
            from app import records as records_mod
            recent_pr = await repository.recent_records(session, user_id, days=RECORDS_CONTEXT_DAYS)
            records = records_mod.to_context(recent_pr) or None
            # Personal baselines (NF-01) — "today vs your norm" from the last ~90 days.
            from app import baselines
            history = await repository.read_history(session, user_id, days=baselines.WINDOW_DAYS)
            norm = baselines.compute_baselines(history)
            # Subjective check-ins (EP-12 phase 3): surface a recurring niggle / rising effort
            # so the daily report acknowledges felt state, not only the objective numbers.
            from app import subjective as subjective_mod
            subj_runs = await repository.recent_subjective_runs(
                session, user_id, days=subjective_mod.WINDOW_DAYS)
            subjective = subjective_mod.summarize(subj_runs)
            # Proactive health alerts (ST-10, future extension of EP-08): reuse the SAME
            # 90-day history slice `norm` was just built from (no second DB read) and only
            # surface an actionable report — calibrating/none stay silent, same as the alert
            # DM itself. The report only *aligns* with an already-sent alert, never a second
            # warning channel of its own.
            from app import health
            health_report = health.detect(
                history, min_history_days=settings.HEALTH_MIN_HISTORY_DAYS)
            if health_report.actionable:
                health_alerts = health.to_context(health_report)
            # Heat/duration fueling advisor (NF-11): only for TODAY's session (the ST-03
            # proximity rule — no gel math for Friday) and only when we have today's
            # forecast already (no extra network call). Zero-LLM; the analyst just narrates.
            today_session = next((s for s in plan_today or [] if s.get("date") == today_iso),
                                  None)
            if weather and today_session:
                from app import fueling as fueling_mod
                anchor_pace = await repository.typical_run_pace(session, user_id)
                fueling = fueling_mod.advise(
                    today_session, weather, heat_feels_c=settings.FUELING_HEAT_FEELS_C,
                    min_duration_min=settings.FUELING_MIN_DURATION_MIN, anchor_pace=anchor_pace,
                )

    # Dedup-cache check — same key inputs as analyze_with_stats builds its prompt from
    # (the README pitfall: every piece of Claude context must be part of the key).
    cache_key = _cache_key(_as_dict(payload), question or _DEFAULT_DAILY_Q, model,
                           previous_report, weather, plan_today, fitness, records, norm,
                           subjective, health_alerts, fueling, today_iso)

    # analyze_with_stats takes its context as positional args (not a single ``context`` dict
    # like the other narrations), so bind them in a closure that matches the engine's
    # ``(context, api_key)`` call shape — the shared cache/log tail (A1) then applies as-is,
    # and ``analyze_with_stats`` stays a module-global lookup so the tests' monkeypatch works.
    def _analyze(_context, _api_key):
        return analyze_with_stats(
            payload, question, deep, kind, previous_report, _api_key, weather,
            plan_today, fitness, records, norm, subjective, health_alerts, fueling,
            today_iso,
        )

    # ``question or None``: /report's default daily prompt is logged as NULL (CLAUDE.md), so
    # an empty question must reach ``log_report`` as None, not "".
    return await _run_cached_narration(
        session, user_id=user_id, kind=kind, model=model, context=None,
        cache_key=cache_key, with_stats_fn=_analyze, question=question or None, api_key=api_key,
    )


# EP-09: /ask is a bounded tool-use agent, not a single completion — the first tool-use
# loop in the project (a deliberate departure from the "prompt-for-JSON, no SDK tool-use"
# choice elsewhere; see CLAUDE.md). A question already answered by recent_reports/recent_qa
# resolves in round 1 with no tool calls (the old cheap path still happens, it's just no
# longer a separate code path); anything needing more history drives query_activities /
# query_daily / aggregate_weekly / get_activity_detail against the FULL stored history.
MAX_ASK_ROUNDS = 5             # hard cap on tool-use round trips per question
MAX_ASK_TOTAL_TOKENS = 60_000  # combined in+out tokens across all rounds — runaway guard
ASK_TOOL_MAX_TOKENS = 2000     # a tool-call round is just a JSON stub, but a detailed final
                                # answer (e.g. a multi-week breakdown) needs real room — same
                                # ceiling as the daily report (analyze_with_stats)

ASK_LIMIT_TEXT = (
    "Це питання вимагає забагато кроків, щоб чесно відповісти з наявних даних. "
    "Спробуй звузити період або сформулювати конкретніше."
)


def _ask_tools() -> list:
    """Anthropic tool schemas for the /ask agent loop — read-only, user-scoped DB queries
    over the full stored history (never raw Garmin/API calls). Built on each call (cheap)
    rather than as a module constant, so the field lists always match
    ``app.garmin.repository.ASK_DAILY_FIELDS``/``ASK_WEEKLY_METRICS``."""
    from app.garmin import repository

    fields = ", ".join(sorted(repository.ASK_DAILY_FIELDS))
    weekly_metrics = ", ".join(repository.ASK_WEEKLY_METRICS)
    return [
        {
            "name": "query_activities",
            "description": (
                "List this user's activities in a date range (both dates inclusive, ISO "
                "yyyy-mm-dd; omit either end for an open range), optionally filtered by "
                "type (substring match, e.g. 'running') or a minimum distance in km. "
                "Returns compact rows: id, date, type, dist_km, dur_min, avg_hr, max_hr, "
                "avg_pace_minkm. Capped at 200 rows, newest first. Use get_activity_detail "
                "with the returned id to drill into one activity."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "date_from": {"type": "string", "description": "ISO date, inclusive"},
                    "date_to": {"type": "string", "description": "ISO date, inclusive"},
                    "type": {"type": "string", "description": "substring match, e.g. 'running'"},
                    "min_dist_km": {"type": "number"},
                },
            },
        },
        {
            "name": "query_daily",
            "description": (
                "Daily recovery/sleep metrics in a date range (both dates inclusive; omit "
                "either end for an open range), oldest first. `fields` picks which metrics "
                f"to return (default: all). Available fields: {fields}. A day with no "
                "stored data yet is simply absent from the result."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "fields": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        {
            "name": "aggregate_weekly",
            "description": (
                "One metric bucketed per ISO week (oldest first) over the last `weeks` "
                f"weeks (default 12, max 26). Valid metrics: {weekly_metrics}."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string"},
                    "weeks": {"type": "integer"},
                },
                "required": ["metric"],
            },
        },
        {
            "name": "get_activity_detail",
            "description": (
                "Full detail on one activity by its DB id (from query_activities): for "
                "runs, pace/HR broken into ~6 segments (not the raw point series); "
                "strength exercises; the runner's subjective RPE/pain check-in if any; "
                "plan-vs-actual comparison if it was matched to a planned session."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            },
        },
        {
            "name": "get_training_plan",
            "description": (
                "This user's ACTIVE training plan/program: goal, target date, days/week, "
                "intensity, the coach's approach summary, and its dated sessions (date, "
                "week, type, dist_km, description, status: planned/done/partial/missed/"
                "skipped) in a date range (both dates inclusive; omit either end for an "
                "open range — omit both for the whole plan). Use this for anything about "
                "\"the program\" itself — upcoming sessions, the goal, adherence — not "
                "query_activities, which is actual completed workouts. Returns "
                "{\"plan\": null} if there's no active plan."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "date_from": {"type": "string", "description": "ISO date, inclusive"},
                    "date_to": {"type": "string", "description": "ISO date, inclusive"},
                },
            },
        },
    ]


async def _run_ask_tool(session, user_id: Optional[int], name: str, args: dict) -> dict:
    """Dispatch one tool call to the matching user-scoped, read-only repository query.
    Never raises: an unknown tool name, bad arguments, or a DB hiccup becomes a compact
    ``{"error": ...}`` the model can see and react to (retry differently, or give up
    honestly) instead of aborting the whole answer."""
    from app.garmin import repository

    try:
        if name == "query_activities":
            rows = await repository.query_activities(
                session, user_id,
                date_from=args.get("date_from"), date_to=args.get("date_to"),
                type=args.get("type"), min_dist_km=args.get("min_dist_km"),
            )
            return {"activities": rows}
        if name == "query_daily":
            rows = await repository.query_daily(
                session, user_id,
                date_from=args.get("date_from"), date_to=args.get("date_to"),
                fields=args.get("fields"),
            )
            return {"days": rows}
        if name == "aggregate_weekly":
            metric = args.get("metric")
            if not metric:
                return {"error": "metric is required"}
            return await repository.aggregate_weekly(
                session, user_id, metric, weeks=args.get("weeks") or 12
            )
        if name == "get_activity_detail":
            try:
                aid = int(args.get("id"))
            except (TypeError, ValueError):
                return {"error": "id must be an integer (the id from query_activities)"}
            act = await repository.get_activity(session, user_id, aid)
            if act is None or getattr(act, "is_hidden", False):   # ST-17: hidden → not found
                return {"error": f"no activity with id={aid} for this user"}
            return activity_payload(act)
        if name == "get_training_plan":
            return await repository.query_training_plan(
                session, user_id,
                date_from=args.get("date_from"), date_to=args.get("date_to"),
            )
        return {"error": f"unknown tool '{name}'"}
    except Exception as e:
        logger.exception(f"ASK tool {name} failed")
        return {"error": str(e)[:200]}


async def run_ask_agent(
    session, user_id: Optional[int], question: str,
    reports: list, recent_asks: list, api_key: Optional[str],
) -> Tuple[str, CallStats, int]:
    """The EP-09 tool-use loop: up to ``MAX_ASK_ROUNDS`` round trips, each either
    answering (``stop_reason != "tool_use"``) or requesting one or more of
    :func:`_ask_tools`. Returns ``(text, cumulative_stats, rounds_used)``. Hitting the
    round or token budget with the model still mid-tool-use returns
    :data:`ASK_LIMIT_TEXT` instead of a partial/guessed answer. Raises AnalystError on
    an API failure (same mapping as every other Claude call)."""
    model = MODEL_ASK
    tools = _ask_tools()
    user_content = {
        "today": dt.date.today().isoformat(),
        "recent_reports": reports,
        "question": question,
    }
    if recent_asks:
        user_content["recent_qa"] = recent_asks
    messages = [{"role": "user", "content": json.dumps(user_content, ensure_ascii=False)}]

    total = CallStats(kind="ask", model=model)
    for round_n in range(1, MAX_ASK_ROUNDS + 1):
        msg, stats = await _run_claude(
            _complete_tools, model, SYSTEM_ASK_TOOLS, messages, tools, api_key,
            ASK_TOOL_MAX_TOKENS,
        )
        total.input_tokens += stats.input_tokens
        total.output_tokens += stats.output_tokens
        total.cost_usd += stats.cost_usd

        if msg.stop_reason != "tool_use":
            text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
            return text, total, round_n

        if total.input_tokens + total.output_tokens > MAX_ASK_TOTAL_TOKENS:
            return ASK_LIMIT_TEXT, total, round_n

        messages.append({"role": "assistant", "content": msg.content})
        tool_results = []
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use":
                result = await _run_ask_tool(session, user_id, block.name, block.input or {})
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False),
                })
        messages.append({"role": "user", "content": tool_results})

    return ASK_LIMIT_TEXT, total, MAX_ASK_ROUNDS


async def run_ask(
    session,
    question: str,
    *,
    user_id: Optional[int] = None,
    n: int = ASK_DEFAULT_N,
    api_key: Optional[str] = None,
) -> str:
    """Answer a free-form question about this user's training/recovery history (EP-09).
    Starts from the last ``n`` daily reports plus the recent /ask thread (so a question
    already answered there resolves in one round, no tool calls); anything needing more
    drives :func:`run_ask_agent`'s bounded tool-use loop over the FULL stored history.
    Persists a ReportLog (kind="ask", ``tool_rounds`` set on a fresh call) and returns the
    text. Dedup-cached on the question + a coarse daily-data slice (``last_data_date`` —
    a pure-DB, no-Garmin proxy for "has anything changed"): a repeat the same day the data
    last changed is a cache hit."""
    from app.db import llm_cache
    from app.garmin import repository

    reports = await repository.get_recent_reports(session, user_id, n=n)
    recent_asks = await repository.get_recent_asks(session, user_id, minutes=ASK_CONTEXT_MIN)
    last_data_date = await repository.latest_daily_date(session, user_id)

    key = _ask_cache_key(reports, question, MODEL_ASK, recent_asks, last_data_date)
    cached = await llm_cache.get(session, key)
    if cached is not None:
        logger.info(f"CLAUDE CACHE HIT  {MODEL_ASK} (ask)")
        text = cached
        await repository.log_report(
            session, user_id=user_id, kind="ask", model=MODEL_ASK, ok=True,
            cached=True, question=question, report_text=text,
        )
        return text

    try:
        text, stats, rounds = await run_ask_agent(
            session, user_id, question, reports, recent_asks, api_key,
        )
    except AnalystError as e:
        await repository.log_report(
            session, user_id=user_id, kind="ask", model=MODEL_ASK, ok=False,
            question=question, error=str(e)[:512]
        )
        raise
    await llm_cache.put(session, key, text, CACHE_TTL_S)
    await repository.log_report(
        session,
        user_id=user_id,
        kind=stats.kind,
        model=stats.model,
        input_tokens=stats.input_tokens,
        output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd,
        ok=True,
        cached=False,
        question=question,
        report_text=text,
        tool_rounds=rounds,
    )
    return text


# ---------- SINGLE ACTIVITY ANALYSIS ----------

#  EP-10 phase 1: a series point may carry pace (running, key "p") or speed/power
#  (cycling, keys "spd"/"pw") — map each raw key to its segment-average output name so
#  _segments works for either sport without the caller having to know which.
_SEGMENT_AVG_KEYS = {
    "p": "avg_pace", "hr": "avg_hr", "spd": "avg_speed_kmh", "pw": "avg_power_w",
}


def _segments(series: list, n: int = 6) -> list:
    """Collapse an activity's per-point series into ~n segments (avg of whatever metrics
    are present — pace/HR for a run, speed/power/HR for a ride) so the LLM sees pacing/
    effort drift without the full point cloud.

    EP-15: when the series carries elevation (``e``, running only — ``avg_pace`` present),
    each segment also gets its climb/descent (``gain_m``/``loss_m``) and grade-adjusted
    pace (``grade_pct``/``gap_pace``) — a hilly split reads as effort, not "slow". Series
    without elevation (old, pre-backfill runs) get exactly the segments they got before."""
    pts = [p for p in series if any(p.get(k) is not None for k in _SEGMENT_AVG_KEYS)]
    if not pts:
        return []
    size = max(1, len(pts) // n)
    elevs = [p.get("e") for p in pts]
    has_elev = any(v is not None for v in elevs)
    smoothed = gap.smooth_elevation(elevs) if has_elev else None
    segs = []
    for i in range(0, len(pts), size):
        chunk = pts[i:i + size]
        ds = [c["d"] for c in chunk if c.get("d") is not None]
        seg = {
            "from_km": round(ds[0], 2) if ds else None,
            "to_km": round(ds[-1], 2) if ds else None,
        }
        for raw_key, out_key in _SEGMENT_AVG_KEYS.items():
            vals = [c[raw_key] for c in chunk if c.get(raw_key) is not None]
            if vals:
                seg[out_key] = round(sum(vals) / len(vals), 2)
        if has_elev and "avg_pace" in seg and len(ds) >= 2:
            chunk_elev = smoothed[i:i + size]
            dist_km = ds[-1] - ds[0]
            gain, loss = gap.elevation_delta(chunk_elev)
            grade = gap.segment_grade_pct(chunk_elev, dist_km)
            if gain or loss:
                seg["gain_m"], seg["loss_m"] = gain, loss
            if grade is not None:
                seg["grade_pct"] = grade
                seg["gap_pace"] = gap.gap_pace_min_km(seg["avg_pace"], grade)
        segs.append(seg)
    return segs


def _planned_payload(workout) -> dict:
    """Compact planned-vs-actual slice for a matched PlannedWorkout (see matching.py)."""
    info = workout.match_info or {}
    return {
        "type": workout.type, "planned_dist_km": workout.dist_km,
        "description": workout.description,
        "plan_pace_minkm": info.get("plan_pace_minkm"),
        "actual_pace_minkm": info.get("actual_pace_minkm"),
        "dist_delta_km": info.get("dist_delta_km"),
        "status": workout.status,  # done | partial
    }


def activity_payload(activity, planned=None) -> dict:
    """Compact LLM input for one ActivityRecord — summary fields plus run segments.
    ``planned`` (optional PlannedWorkout matched by matching.match_activities) adds a
    planned-vs-actual slice so the analysis can judge adherence, not just the raw effort."""
    data = {
        "type": activity.type, "date": activity.date,
        "dur_min": activity.dur_min, "dist_km": activity.dist_km,
        "avg_hr": activity.avg_hr, "max_hr": activity.max_hr, "load": activity.load,
    }
    if activity.exercises:
        data["exercises"] = activity.exercises
    if activity.series:
        data["segments"] = _segments(activity.series)
        if activity.dist_km and activity.dur_min:
            # EP-10 phase 1: a ride reads in km/h, a run in min/km — pick by sport bucket
            # rather than sniffing the series shape, so a series-less-but-typed row still
            # gets the right unit if that ever changes.
            if sport_bucket(activity.type) == "bike":
                data["avg_speed_kmh"] = round(activity.dist_km / (activity.dur_min / 60.0), 1)
            else:
                data["avg_pace"] = round(activity.dur_min / activity.dist_km, 2)
        # EP-15: whole-activity climb/descent, when the series carries elevation — the
        # "hilly" flag is what tells SYSTEM_ACTIVITY whether to mention terrain at all.
        elevation = gap.activity_elevation_summary(activity.series)
        if elevation:
            data["elevation_gain_m"] = elevation["gain_m"]
            data["elevation_loss_m"] = elevation["loss_m"]
            data["hilly"] = elevation["hilly"]
    # EP-12: the runner's subjective check-in (RPE + niggle). Part of the payload, so it
    # also enters the dedup-cache key automatically (_activity_cache_key hashes `data`).
    if getattr(activity, "subjective", None):
        data["subjective"] = activity.subjective
    # NF-14: step-level plan-vs-actual (app.stepmatch) — whether the runner actually hit
    # the planned pace inside each structured interval, not just "the session happened".
    if getattr(activity, "step_match", None):
        data["step_match"] = activity.step_match
    if planned is not None:
        data["planned"] = _planned_payload(planned)
    return data


def analyze_activity_with_stats(
    activity_data: dict, api_key: Optional[str] = None
) -> Tuple[str, CallStats]:
    """Analyze one activity. Returns (text, stats); raises AnalystError on API failure.
    The dedup cache (keyed on the activity payload + model) is checked in
    :func:`run_activity_analysis`."""
    model = MODEL_ACTIVITY
    today_iso = dt.date.today().isoformat()
    user_content = {**daterel.today_context(today_iso), "activity": activity_data}
    # The label rides OUTSIDE ``activity`` on purpose: the dedup cache keys on the activity
    # payload alone, so a relative label inside it would expire a stored analysis every
    # midnight (a paid re-run of an unchanged activity).
    day = daterel.label(activity_data.get("date"), today_iso)
    if day:
        user_content["activity_day"] = day
    try:
        from anthropic import APIConnectionError, APIStatusError

        msg = _get_client(api_key).messages.create(
            model=model, max_tokens=1500, system=SYSTEM_ACTIVITY,
            # See analyze_with_stats above: Sonnet 5 (MODEL_ACTIVITY) defaults to
            # adaptive thinking when omitted, which can eat the whole max_tokens
            # budget and leave no room for the actual text.
            thinking={"type": "disabled"},
            messages=[{"role": "user",
                       "content": json.dumps(user_content, ensure_ascii=False)}],
        )
        stats = CallStats(kind="activity", model=model)
        usage = getattr(msg, "usage", None)
        if usage:
            pin, pout = PRICES.get(model, (0, 0))
            stats.input_tokens = usage.input_tokens
            stats.output_tokens = usage.output_tokens
            stats.cost_usd = usage.input_tokens / 1e6 * pin + usage.output_tokens / 1e6 * pout
            logger.info(
                f"CLAUDE OK  {model} (activity)  in={usage.input_tokens} "
                f"out={usage.output_tokens} ~${stats.cost_usd:.4f}"
            )
        text = "".join(b.text for b in msg.content if b.type == "text")
        return text, stats
    except APIStatusError as e:
        raise _status_error(e)
    except APIConnectionError:
        raise AnalystError("🌐 Не вдалось з'єднатися з API. Перевір інтернет і спробуй ще.")


async def run_activity_analysis(
    session, activity, *, user_id: Optional[int] = None, api_key: Optional[str] = None,
    force: bool = False,
) -> str:
    """Analyze one activity, store the text on the row (``analysis``) for the web detail
    page, log a ReportLog (kind="activity"), and return the text.

    ``force=True`` (ST-19) regenerates even when a valid cached analysis exists — for an
    explicit "подивись ще раз" after resynced data or a poor first write. It still writes the
    fresh text to both the dedup cache and ``activity.analysis`` (a following non-force call
    is a cache hit)."""
    from app.garmin import repository

    planned = await repository.get_workout_for_activity(session, user_id, activity.id) \
        if user_id is not None else None
    data = activity_payload(activity, planned)
    q = f"activity #{activity.id} ({activity.type})"
    text = await _run_cached_narration(
        session, user_id=user_id, kind="activity", model=MODEL_ACTIVITY, context=data,
        cache_key=_activity_cache_key(data, MODEL_ACTIVITY),
        with_stats_fn=analyze_activity_with_stats, question=q, api_key=api_key,
        force=force,
    )
    activity.analysis = text
    return text


# ---------- WEEKLY DIGEST (EP-07) ----------

DIGEST_VOLUME_WEEKS = 4        # weekly_run_volume window fed as the volume trend
DIGEST_COMPLIANCE_WEEKS = 2    # how many recent ISO weeks of compliance to include
DIGEST_RECOVERY_DAYS = 14      # recovery trend window
DIGEST_RECORDS_DAYS = 30       # personal records set in the last month (EP-14)


def digest_with_stats(
    context: dict, api_key: Optional[str] = None
) -> Tuple[str, CallStats]:
    """Narrate the week's already-computed numbers into a Sunday digest (Sonnet).
    Returns (text, stats); raises AnalystError on API failure. The dedup cache is
    checked in :func:`run_digest`."""
    return _complete(MODEL_DIGEST, SYSTEM_DIGEST, context, "digest", api_key, max_tokens=1200)


def _week_volume_summary(weekly_volume: Optional[list], this_week: str, prev_week: str) -> dict:
    """This-week vs last-week running numbers (computed here, not by the LLM), from the
    per-ISO-week ``weekly_run_volume`` buckets. Missing weeks read as zero."""
    by_week = {w["week"]: w for w in (weekly_volume or [])}
    cur = by_week.get(this_week) or {}
    prev = by_week.get(prev_week) or {}
    cur_km, prev_km = cur.get("km", 0.0), prev.get("km", 0.0)
    return {
        "run_km": cur_km,
        "run_km_prev": prev_km,
        "delta_km": round(cur_km - prev_km, 1),
        "runs": cur.get("runs", 0),
        "runs_prev": prev.get("runs", 0),
        "longest_km": cur.get("longest_km", 0.0),
        "longest_km_prev": prev.get("longest_km", 0.0),
    }


async def run_digest(
    session, *, user_id: int, api_key: Optional[str] = None
) -> Optional[str]:
    """Assemble the week's plan/fact + trends, narrate them via Sonnet, cache and log
    (``ReportLog(kind="digest")``), and return the text. Returns ``None`` (nothing to
    send) for a user with no history and no plan. Numbers are computed here; the LLM
    only interprets them (EP-07)."""
    from app.garmin import repository

    today = dt.date.today()
    this_week = today.strftime("%G-W%V")
    prev_week = (today - dt.timedelta(days=7)).strftime("%G-W%V")

    from app import records as records_mod

    weekly_volume = await repository.weekly_run_volume(session, user_id, weeks=DIGEST_VOLUME_WEEKS)
    recovery = await repository.read_history(session, user_id, days=DIGEST_RECOVERY_DAYS)
    ex = await repository.get_recent_extra(session, user_id)
    fitness = _build_fitness_snapshot(ex)
    multisport = await _build_multisport(session, user_id)
    month_records = records_mod.to_context(
        await repository.recent_records(session, user_id, days=DIGEST_RECORDS_DAYS)
    ) or None

    # NF-19: aerobic-efficiency trend (pace@HR, GAP-honest) — plan-independent, so it's
    # built here regardless of whether there's an active plan. Only the "ok"/"calibrating"
    # states carry into context (None → the field is simply absent, prompt stays silent).
    from app import efficiency as eff_mod
    efficiency_trend = eff_mod.build_trend(
        await repository.runs_for_efficiency(session, user_id)
    )

    # NF-21: bedtime regularity (std of sleep_start over the last 14 nights) — None below
    # sleepnudge.TIMING_MIN_NIGHTS of timing data, same as the evening nudge itself.
    from app import sleepnudge
    sleep_regularity = sleepnudge.sleep_regularity(recovery)

    plan = await repository.get_active_plan(session, user_id)
    compliance = None
    goal = None
    goal_projection = None
    if plan is not None:
        compliance = _recent_compliance(
            await repository.weekly_compliance(session, plan.id), weeks=DIGEST_COMPLIANCE_WEEKS
        ) or None
        goal = {k: v for k, v in {
            "goal": plan.goal,
            "goal_label": plan.goal_label,
            "target_date": plan.target_date,
            "days_to_target": _days_to_target(plan.target_date, today),
            "summary": plan.summary,
        }.items() if v is not None}
        # NF-10: a quantified read on the same question ("чи на треку до цілі") — a
        # weekly-median trend of Garmin's own race-time prediction (or VO2max for the
        # open-ended goal), projected to target_date when that's not too far out.
        from app import goal as goal_mod
        metric_key, _label, higher_better = goal_mod.metric_for_goal(plan.goal)
        fitness_history = await repository.read_fitness_history(session, user_id)
        goal_projection = goal_mod.project(
            fitness_history, metric_key=metric_key, higher_better=higher_better,
            target_date=plan.target_date,
            target_s=(plan.intake or {}).get("target_time_s"),   # NF-17: number on "чи на треку"
        )

    # Nothing worth saying for a brand-new user with no runs, no metrics and no plan.
    if not weekly_volume and not fitness and plan is None:
        logger.info(f"DIGEST skip user={user_id}: no history and no plan")
        return None

    context = {
        "today": today.isoformat(),
        "iso_week": this_week,
        "week": _week_volume_summary(weekly_volume, this_week, prev_week),
        "weekly_volume": weekly_volume or None,
        "compliance": compliance,
        "recovery": recovery or None,
        "fitness": fitness or None,
        "multisport": multisport,
        "goal": goal,
        "goal_projection": goal_projection,
        "efficiency": efficiency_trend,
        "records": month_records,
        "sleep_regularity": sleep_regularity,
        "has_plan": plan is not None,
    }

    return await _run_cached_narration(
        session, user_id=user_id, kind="digest", model=MODEL_DIGEST, context=context,
        cache_key=_digest_cache_key(context, MODEL_DIGEST),
        with_stats_fn=digest_with_stats, question=f"digest:{this_week}", api_key=api_key,
    )


# ---------- COMPARE PAST SELF (NF-06) ----------

def compare_with_stats(
    context: dict, api_key: Optional[str] = None
) -> Tuple[str, CallStats]:
    """Narrate a two-window self-comparison (Sonnet). Returns (text, stats); raises
    AnalystError on API failure. The dedup cache is checked in :func:`run_compare`."""
    return _complete(MODEL_COMPARE, SYSTEM_COMPARE, context, "compare", api_key, max_tokens=900)


async def run_compare(
    session, *, user_id: int, weeks: int, years_back: int = 1,
    api_key: Optional[str] = None,
) -> Optional[str]:
    """Compare the user's last ``weeks`` weeks with the same calendar span ``years_back`` years
    ago (NF-06). Assembles both windows' numbers in Python, narrates via Sonnet, caches + logs
    (``ReportLog(kind="compare")``), and returns the text. Returns ``None`` when there isn't
    enough in BOTH windows to compare (a new user, or no history a year back) — the caller
    turns that into a friendly "not enough history yet" message."""
    from app import compare as compare_mod
    from app.garmin import repository

    today = dt.date.today()
    cur_start, cur_end, past_start, past_end = compare_mod.window_pair(today, weeks, years_back)
    current = await repository.window_stats(session, user_id, cur_start, cur_end)
    past = await repository.window_stats(session, user_id, past_start, past_end)
    if not compare_mod.has_signal(current, past):
        logger.info(f"COMPARE skip user={user_id}: not enough history in both windows")
        return None

    context = compare_mod.build_context(weeks, years_back, current, past)
    return await _run_cached_narration(
        session, user_id=user_id, kind="compare", model=MODEL_COMPARE, context=context,
        cache_key=_compare_cache_key(context, MODEL_COMPARE),
        with_stats_fn=compare_with_stats,
        question=f"compare:{weeks}w/{years_back}y", api_key=api_key,
    )


# ---------- WRAPPED — QUARTERLY/YEARLY REVIEW (NF-07) ----------

def wrapped_with_stats(
    context: dict, api_key: Optional[str] = None
) -> Tuple[str, CallStats]:
    """Narrate a season Wrapped recap (Opus — one aesthetic longread). Returns (text, stats);
    raises AnalystError on API failure. The dedup cache is checked in :func:`run_wrapped`."""
    return _complete(MODEL_WRAPPED, SYSTEM_WRAPPED, context, "wrapped", api_key, max_tokens=1200)


async def run_wrapped(
    session, *, user_id: int, period: str = "year", api_key: Optional[str] = None,
) -> Optional[str]:
    """Assemble the period's numbers (NF-07) and narrate a "Wrapped" recap via one Opus call.
    Caches + logs (``ReportLog(kind="wrapped")``) and returns the text. Returns ``None`` when
    the window is too empty to recap (a new user) — the caller shows a friendly message."""
    from app import wrapped as wrapped_mod
    from app.garmin import repository

    start, end = wrapped_mod.period_window(dt.date.today(), period)
    stats = await repository.wrapped_stats(session, user_id, start, end)
    if not wrapped_mod.has_signal(stats):
        logger.info(f"WRAPPED skip user={user_id}: not enough history in {period}")
        return None
    records = await repository.records_in_range(session, user_id, start, end)
    context = wrapped_mod.build_context(period, start, end, stats, records)
    return await _run_cached_narration(
        session, user_id=user_id, kind="wrapped", model=MODEL_WRAPPED, context=context,
        cache_key=_wrapped_cache_key(context, MODEL_WRAPPED),
        with_stats_fn=wrapped_with_stats, question=f"wrapped:{period}", api_key=api_key,
    )


# ---------- RACE PACK (EP-05) ----------

def race_plan_with_stats(
    context: dict, api_key: Optional[str] = None
) -> Tuple[str, CallStats]:
    """Narrate a pre-race pacing/fueling/checklist synthesis (Opus). Returns (text, stats);
    raises AnalystError on API failure. The dedup cache is checked in :func:`run_race_plan`."""
    return _complete(MODEL_RACE, SYSTEM_RACE, context, "race", api_key, max_tokens=1600)


async def run_race_plan(
    session, *, user_id: int, api_key: Optional[str] = None,
) -> Optional[str]:
    """Assemble the active plan's race-day context (EP-05: target/fitness/taper sessions/
    weather) and narrate a race pack via one Opus call. Caches + logs
    (``ReportLog(kind="race")``) and returns the text. Returns ``None`` when there's no
    active plan with both a target date and a race distance (no plan, or an open-ended
    ``general`` plan) — the caller shows a friendly explanation, not an error."""
    from fastapi.concurrency import run_in_threadpool

    from app import race as race_mod
    from app import weather as weather_mod
    from app.db.models import User
    from app.garmin import repository

    plan = await repository.get_active_plan(session, user_id)
    if not race_mod.has_target(plan):
        return None

    workouts = await repository.list_workouts(session, plan.id, upcoming_only=True)
    recent_sessions = [
        {"date": w.date, "type": w.type, "dist_km": w.dist_km, "description": w.description}
        for w in workouts if w.date <= plan.target_date
    ]
    fitness = _build_fitness_snapshot(await repository.get_recent_extra(session, user_id))

    forecast_day = None
    days_left = race_mod.days_to_target(plan.target_date)
    if days_left is not None and 0 <= days_left <= race_mod.WEATHER_WINDOW_DAYS:
        user = await session.get(User, user_id)
        if user is not None and user.latitude is not None and user.longitude is not None:
            week = await run_in_threadpool(
                weather_mod.fetch_forecast_week, user.latitude, user.longitude)
            if week:
                forecast_day = next(
                    (d for d in week if d.get("date") == plan.target_date), None)

    context = race_mod.build_context(plan, fitness, recent_sessions, forecast_day)
    return await _run_cached_narration(
        session, user_id=user_id, kind="race", model=MODEL_RACE, context=context,
        cache_key=_race_cache_key(context, MODEL_RACE),
        with_stats_fn=race_plan_with_stats, question=f"race:{plan.id}", api_key=api_key,
    )


# ---------- CORRELATION INSIGHTS (NF-02) ----------

INSIGHTS_WINDOW_DAYS = 120   # how much recovery history the correlation pass looks over


def insights_with_stats(
    context: dict, api_key: Optional[str] = None
) -> Tuple[str, CallStats]:
    """Narrate the significant correlations (Sonnet). Returns (text, stats); raises
    AnalystError on API failure. The dedup cache is checked in :func:`run_insights`."""
    return _complete(MODEL_INSIGHTS, SYSTEM_INSIGHTS, context, "insights", api_key,
                     max_tokens=700)


async def run_insights(
    session, *, user_id: int, api_key: Optional[str] = None,
) -> Optional[str]:
    """Run the NF-02 correlation pass over the user's recovery history and narrate the
    significant findings via one Sonnet call. Caches + logs (``ReportLog(kind="insights")``)
    and returns the text. Returns ``None`` when no association is statistically defensible
    (the honest "not enough data" path) — the caller shows a friendly message and never
    spends a Claude call in that case."""
    from app import correlations
    from app.garmin import repository

    history = await repository.read_history(session, user_id, days=INSIGHTS_WINDOW_DAYS)
    findings = correlations.find_correlations(history)
    if not findings:
        logger.info(f"INSIGHTS skip user={user_id}: no significant correlations")
        return None
    context = correlations.build_context(findings, INSIGHTS_WINDOW_DAYS)
    return await _run_cached_narration(
        session, user_id=user_id, kind="insights", model=MODEL_INSIGHTS, context=context,
        cache_key=_insights_cache_key(context, MODEL_INSIGHTS),
        with_stats_fn=insights_with_stats,
        question=f"insights:{len(findings)}", api_key=api_key,
    )


# ---------- INJURY-RISK RADAR (NF-04) ----------

async def build_injury_assessment(session, *, user_id: int):
    """Fetch the injury radar's windowed inputs and run the pure detector (``app.injury``).
    Returns an ``injury.Assessment`` — ``level="calibrating"`` until the user has enough
    history (the EP-08 anti-false-positive gate). No LLM, no network; used by both the
    ``/risk`` command (display only) and the morning warning hook (which then narrates an
    actionable result)."""
    from app import injury
    from app.garmin import repository

    daily = await repository.read_load_history(session, user_id, days=injury.WINDOW_DAYS)
    runs = await repository.recent_subjective_runs(session, user_id, days=injury.WINDOW_DAYS)
    history_days = await repository.count_daily_metrics(session, user_id)
    return injury.assess(
        daily, runs, history_days=history_days,
        min_history_days=settings.INJURY_MIN_HISTORY_DAYS,
    )


def injury_with_stats(
    context: dict, api_key: Optional[str] = None
) -> Tuple[str, CallStats]:
    """Narrate an actionable injury assessment into a short advisory (Sonnet)."""
    return _complete(MODEL_INJURY, SYSTEM_INJURY, context, "injury", api_key, max_tokens=600)


async def run_injury_check(
    session, *, user_id: int, assessment, api_key: Optional[str] = None,
) -> str:
    """Turn an actionable ``injury.Assessment`` into a user-facing advisory. Narrates via
    Sonnet (``SYSTEM_INJURY``) but falls back to the deterministic ``injury.summary`` if the
    LLM call fails — the warning must never depend on the LLM. Logs ``ReportLog(kind="injury")``
    on success. Not dedup-cached (rare, and the caller guards frequency). Callers must only
    invoke this for an actionable assessment (``assessment.actionable``)."""
    from app import injury
    from app.garmin import repository

    context = injury.to_context(assessment)
    try:
        text, stats = await _run_claude(injury_with_stats, context, api_key)
    except AnalystError as e:
        logger.warning(f"INJURY narration failed user={user_id}, using fallback: {e}")
        await repository.log_report(
            session, user_id=user_id, kind="injury", model=MODEL_INJURY, ok=False,
            question=f"injury:{assessment.level}", error=str(e)[:512],
        )
        return injury.summary(assessment)
    await repository.log_report(
        session, user_id=user_id, kind=stats.kind, model=stats.model,
        input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd, ok=True, cached=stats.cached,
        question=f"injury:{assessment.level}", report_text=text,
    )
    return text


# ---------- PROACTIVE HEALTH ALERTS (EP-08) ----------

async def build_health_alerts(session, *, user_id: int):
    """Fetch the recovery history and run the pure health detector (``app.health``). Returns
    a ``health.HealthReport`` — ``level="calibrating"`` until the user has enough history (the
    anti-false-positive cold-start gate). No LLM, no network; used by both the ``/health``
    command (display only) and the morning alert hook (which then narrates an actionable
    result). Thresholds are the user's own NF-01 percentile bands, computed inside the
    detector from the same 90-day slice."""
    from app import baselines, health
    from app.garmin import repository

    history = await repository.read_history(session, user_id, days=baselines.WINDOW_DAYS)
    return health.detect(
        history, min_history_days=settings.HEALTH_MIN_HISTORY_DAYS
    )


def health_with_stats(
    context: dict, api_key: Optional[str] = None
) -> Tuple[str, CallStats]:
    """Narrate an actionable health report into a short advisory (Sonnet)."""
    return _complete(MODEL_HEALTH, SYSTEM_HEALTH, context, "health", api_key, max_tokens=600)


async def run_health_alert(
    session, *, user_id: int, report, api_key: Optional[str] = None,
) -> str:
    """Turn an actionable ``health.HealthReport`` into a user-facing advisory. Narrates via
    Sonnet (``SYSTEM_HEALTH``) but falls back to the deterministic ``health.summary`` if the
    LLM call fails — the warning must never depend on the LLM (same contract as the injury
    radar). Logs ``ReportLog(kind="health")`` on success. Not dedup-cached (rare, and the
    caller guards frequency per-rule). Callers must only invoke this for an actionable report."""
    from app import health
    from app.garmin import repository

    context = health.to_context(report)
    try:
        text, stats = await _run_claude(health_with_stats, context, api_key)
    except AnalystError as e:
        logger.warning(f"HEALTH narration failed user={user_id}, using fallback: {e}")
        await repository.log_report(
            session, user_id=user_id, kind="health", model=MODEL_HEALTH, ok=False,
            question=f"health:{report.level}", error=str(e)[:512],
        )
        return health.summary(report)
    await repository.log_report(
        session, user_id=user_id, kind=stats.kind, model=stats.model,
        input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd, ok=True, cached=stats.cached,
        question=f"health:{report.level}", report_text=text,
    )
    return text


# ---------- HEALTH-CHECKUP INTERPRETATION (the "Аналізи" tab's analysis step) ----------

CHECKUP_HISTORY_LIMIT = 3   # prior same-category checkups fed as trend context


def checkup_payload(checkup, history: Optional[list] = None) -> dict:
    """Compact LLM input for one ``HealthCheckup`` — its own results/notes plus, when
    given, up to :data:`CHECKUP_HISTORY_LIMIT` prior same-category checkups (see
    ``app.db.checkups.similar_history``) so the model can speak to a trend, not just a
    single snapshot."""
    data = {"date": checkup.date, "title": checkup.title}
    if checkup.category:
        data["category"] = checkup.category
    if checkup.results:
        data["results"] = checkup.results
    if checkup.notes:
        data["notes"] = checkup.notes
    if history:
        data["history"] = [
            {"date": h.date, "title": h.title, "results": h.results}
            for h in history if h.results
        ]
    return data


def checkup_with_stats(
    context: dict, api_key: Optional[str] = None
) -> Tuple[str, CallStats]:
    """Interpret one health checkup's results (Sonnet). Returns (text, stats); raises
    AnalystError on API failure. The dedup cache is checked in
    :func:`run_checkup_analysis`."""
    return _complete(MODEL_CHECKUP, SYSTEM_CHECKUP, context, "checkup", api_key,
                     max_tokens=700)


async def run_checkup_analysis(
    session, checkup, *, user_id: int, api_key: Optional[str] = None,
) -> str:
    """Interpret one ``HealthCheckup``'s results, store the text on the row (``analysis``)
    for the web detail page, log a ``ReportLog(kind="checkup")``, and return the text.
    Only ever called on an explicit user request (a button tap) — never from a background
    job, unlike most other narrations here, since it's a real (if cheap) paid Claude call
    the user didn't necessarily ask to repeat automatically."""
    from app.db import checkups as checkups_db

    history = await checkups_db.similar_history(
        session, user_id, checkup, limit=CHECKUP_HISTORY_LIMIT)
    data = checkup_payload(checkup, history)
    q = f"checkup #{checkup.id} ({checkup.title})"
    text = await _run_cached_narration(
        session, user_id=user_id, kind="checkup", model=MODEL_CHECKUP, context=data,
        cache_key=_checkup_cache_key(data, MODEL_CHECKUP),
        with_stats_fn=checkup_with_stats, question=q, api_key=api_key,
    )
    checkup.analysis = text
    return text


# ---------- CHECKUP UPLOAD (photo/PDF → structured checkup, "Аналізи" entry shortcut) ----------

CHECKUP_UPLOAD_BATCH_MAX = 5  # files per Claude call — one request beats N (see below)

# Per-file token budget: scales with batch size (more files → more possible output),
# capped so a pathological batch can't run away the cost. A single file keeps the
# exact same numbers as before batching existed (4096/8192) — no regression there.
CHECKUP_OCR_MAX_TOKENS_PER_FILE = 4096
CHECKUP_OCR_MAX_TOKENS_CAP = 16000
CHECKUP_OCR_RETRY_MAX_TOKENS_PER_FILE = 8192
CHECKUP_OCR_RETRY_MAX_TOKENS_CAP = 32000


def _ocr_max_tokens(n_files: int) -> int:
    return min(CHECKUP_OCR_MAX_TOKENS_PER_FILE * max(1, n_files), CHECKUP_OCR_MAX_TOKENS_CAP)


def _ocr_retry_max_tokens(n_files: int) -> int:
    return min(
        CHECKUP_OCR_RETRY_MAX_TOKENS_PER_FILE * max(1, n_files), CHECKUP_OCR_RETRY_MAX_TOKENS_CAP)


def checkup_ocr_with_stats(
    files: list, api_key: Optional[str] = None, max_tokens: Optional[int] = None,
) -> Tuple[str, CallStats]:
    """Read 1+ uploaded lab-report photos/PDFs in ONE Claude vision call (``files`` —
    ``[(media_type, data_b64), ...]``, up to :data:`CHECKUP_UPLOAD_BATCH_MAX`) and
    return raw JSON text; parsing lives in :func:`_coerce_checkup_ocr_batch`, called
    from :func:`run_checkup_ocr_batch`."""
    return _complete_vision(
        MODEL_CHECKUP_OCR, SYSTEM_CHECKUP_OCR, "checkup_ocr", files,
        api_key, max_tokens=max_tokens or _ocr_max_tokens(len(files)),
    )


def _coerce_one_checkup(data: dict) -> dict:
    """Field-level extraction shared by every item in the batch — one Claude-returned
    checkup object → ``create_checkup``-shaped kwargs. Drops any result row with no
    name rather than raising: a partially-legible document should still produce a
    usable (if incomplete) checkup."""
    results = []
    for r in (data.get("results") or []):
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        results.append({
            "name": name,
            "value": str(r.get("value") or "").strip(),
            "unit": str(r.get("unit") or "").strip(),
            "ref_range": str(r.get("ref_range") or "").strip(),
        })
    return {
        "title": (str(data.get("title") or "").strip() or None),
        "date": (str(data.get("date") or "").strip() or None),
        "category": (str(data.get("category") or "").strip() or None),
        "results": results or None,
        "notes": (str(data.get("notes") or "").strip() or None),
    }


def _coerce_checkup_ocr_batch(text: str) -> list:
    """Parse Claude's OCR JSON into a list of ``create_checkup``-shaped kwargs,
    tolerating ``` fences / surrounding prose (same outermost-``{...}`` slice as
    ``_coerce_supplement_advice``). The schema is ``{"checkups": [...]}``; a model
    that (against instructions) returns one bare checkup object instead of the array
    is still accepted as a single-item batch rather than failing the whole upload."""
    s = text.strip()
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        s = s[i:j + 1]
    data = json.loads(s)
    items = data.get("checkups") if isinstance(data, dict) and "checkups" in data else [data]
    return [_coerce_one_checkup(item) for item in items if isinstance(item, dict)]


async def run_checkup_ocr_batch(
    session, *, user_id: int, files: list, fallback_date: str,
    api_key: Optional[str] = None,
) -> List[HealthCheckup]:
    """Turn 1+ uploaded lab-report photos/PDFs into normal, editable ``HealthCheckup``
    rows via ONE Claude vision call — ``files`` is ``[(file_bytes, media_type), ...]``,
    at most :data:`CHECKUP_UPLOAD_BATCH_MAX`. Batching lets Claude tell whether several
    files are pages of the SAME report (merged into one checkup) or separate documents
    (one checkup each), and costs one system-prompt + one round-trip instead of N.
    Rows are saved immediately but left fully editable — same review-before-trust
    posture as ``supplement_advice_to_checkup_template``. Only ever triggered by an
    explicit upload, never automatically. Not dedup-cached (each upload is a unique
    file set, not a repeatable question) but every attempt still logs a
    ``ReportLog(kind="checkup_ocr")`` for cost tracking, success or failure."""
    from app.db import checkups as checkups_db
    from app.garmin import repository

    b64_files = [(media_type, base64.b64encode(fb).decode("ascii")) for fb, media_type in files]
    q = f"checkup upload (OCR, {len(files)} file{'s' if len(files) != 1 else ''})"
    try:
        text, stats = await _run_claude(checkup_ocr_with_stats, b64_files, api_key, None)
    except AnalystError as e:
        await repository.log_report(
            session, user_id=user_id, kind="checkup_ocr", model=MODEL_CHECKUP_OCR, ok=False,
            question=q, error=str(e)[:512],
        )
        raise
    try:
        parsed_items = _coerce_checkup_ocr_batch(text)
    except Exception as e:
        # A parse failure here is almost always the reply getting cut off mid-JSON by
        # hitting the token budget on a big batch (stop_reason=max_tokens) — one retry
        # with a much larger budget recovers it instead of losing the whole upload.
        logger.warning(f"CHECKUP_OCR parse failed, retrying with larger budget: {e}")
        try:
            text2, stats2 = await _run_claude(
                checkup_ocr_with_stats, b64_files, api_key, _ocr_retry_max_tokens(len(files)),
            )
        except AnalystError as e2:
            await repository.log_report(
                session, user_id=user_id, kind="checkup_ocr", model=MODEL_CHECKUP_OCR, ok=False,
                question=q, error=str(e2)[:512],
            )
            raise
        stats.input_tokens += stats2.input_tokens
        stats.output_tokens += stats2.output_tokens
        stats.cost_usd += stats2.cost_usd
        try:
            parsed_items = _coerce_checkup_ocr_batch(text2)
            text = text2
        except Exception as e2:
            logger.error(f"CHECKUP_OCR parse failed after retry: {e2}")
            await repository.log_report(
                session, user_id=user_id, kind=stats.kind, model=stats.model,
                input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
                cost_usd=stats.cost_usd, ok=False, question=q, error="parse failed",
            )
            raise AnalystError(
                "Не вдалось розпізнати документ(и) — спробуй чіткіше фото або введи вручну."
            )
    await repository.log_report(
        session, user_id=user_id, kind=stats.kind, model=stats.model,
        input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd, ok=True, question=q, report_text=text,
    )
    if not parsed_items:
        raise AnalystError(
            "Не вдалось розпізнати документ(и) — спробуй чіткіше фото або введи вручну."
        )
    return [
        await checkups_db.create_checkup(
            session, user_id,
            date=parsed["date"] or fallback_date,
            title=parsed["title"] or "Аналіз (розпізнано)",
            category=parsed["category"],
            results=parsed["results"],
            notes=parsed["notes"],
        )
        for parsed in parsed_items
    ]


# ---------- SUPPLEMENT → LAB-MONITORING ADVICE (the "Аналізи" tab's third follow-up) ----------

def supplement_payload(supplements: list, recent_categories: Optional[list] = None) -> dict:
    """Compact LLM input: each active ``Supplement``'s name/dosage/frequency/started_date/
    notes, plus (when given) the categories of checkups the user is already logging, so
    the model can skip recommending a test that's already tracked."""
    data = {
        "supplements": [
            {k: v for k, v in (
                ("name", s.name), ("dosage", s.dosage), ("frequency", s.frequency),
                ("started_date", s.started_date), ("notes", s.notes),
            ) if v is not None}
            for s in supplements
        ],
    }
    if recent_categories:
        data["recent_checkup_categories"] = recent_categories
    return data


def _coerce_supplement_advice(text: str) -> SupplementAdvice:
    """Parse Claude's reply into a SupplementAdvice, tolerating ``` fences / surrounding
    prose by slicing to the outermost {...} (same trick as ``_coerce_plan``/``_coerce_edit``)."""
    s = text.strip()
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        s = s[i:j + 1]
    return SupplementAdvice(**json.loads(s))


def supplement_advice_with_stats(
    context: dict, api_key: Optional[str] = None
) -> Tuple[str, CallStats]:
    """Get Claude's structured supplement → lab-marker advice (Sonnet), one retry on a
    parse miss (else AnalystError — same shape as ``_plan_ops_with_stats``). Returns the
    validated ``SupplementAdvice`` serialised back to JSON (not prose) as the "text": a
    fixed, machine-parseable shape is what lets the "create a checkup template" button
    turn ``marker`` values into pre-filled result rows, and it's exactly what the dedup
    cache and ``ReportLog.report_text`` end up storing. The dedup cache is checked in
    :func:`run_supplement_advice`.

    ``max_tokens`` scales with the supplement count — a flat 700 was tuned for a
    handful of items and silently truncated mid-JSON (``stop_reason=max_tokens``) once a
    user logged a dozen supplements, since each gets its own marker+frequency+note.
    Capped so a pathological list can't run away the cost."""
    n = len(context.get("supplements") or [])
    max_tokens = min(2200, 900 + 130 * n)
    text, stats = _complete(MODEL_SUPPLEMENTS, SYSTEM_SUPPLEMENTS, context, "supplements",
                            api_key, max_tokens=max_tokens)
    try:
        advice = _coerce_supplement_advice(text)
    except Exception:
        retry = dict(context, _note="Поверни ЛИШЕ валідний JSON за схемою, без тексту навколо.")
        text2, stats2 = _complete(MODEL_SUPPLEMENTS, SYSTEM_SUPPLEMENTS, retry, "supplements",
                                  api_key, max_tokens=max_tokens)
        stats.input_tokens += stats2.input_tokens
        stats.output_tokens += stats2.output_tokens
        stats.cost_usd += stats2.cost_usd
        try:
            advice = _coerce_supplement_advice(text2)
        except Exception as e:
            logger.error(f"SUPPLEMENTS parse failed: {e}")
            raise AnalystError("Не вдалось отримати пораду — спробуй ще раз.")
    return advice.model_dump_json(), stats


def parse_supplement_advice(text: str) -> Optional[SupplementAdvice]:
    """Safely parse a stored ``ReportLog(kind="supplements").report_text`` back into a
    ``SupplementAdvice`` — ``None`` on any failure, which also covers pre-existing rows
    written before this JSON format (plain prose): those just stop showing structured
    items/a template button rather than crashing the page."""
    try:
        return SupplementAdvice(**json.loads(text))
    except Exception:
        return None


def supplement_advice_to_checkup_template(advice: SupplementAdvice) -> Optional[dict]:
    """Turn a ``SupplementAdvice`` into ``app.db.checkups.create_checkup`` kwargs for a
    ready-to-fill checkup: one empty result row per DISTINCT recommended marker (later
    filled in by hand via the normal checkup edit form once the real lab report is back).
    Returns ``None`` when no item carries a marker (nothing to template)."""
    seen: dict = {}
    for item in advice.items:
        if not item.marker or item.marker in seen:
            continue
        seen[item.marker] = item
    if not seen:
        return None
    notes_lines = [
        f"- {marker}" + (f" — {item.frequency}" if item.frequency else "")
        + (f" ({item.supplement})" if item.supplement else "")
        for marker, item in seen.items()
    ]
    return {
        "title": "Рекомендовані аналізи (за добавками)",
        "category": "рекомендовано",
        "results": [{"name": marker, "value": "", "unit": "", "ref_range": ""} for marker in seen],
        "notes": "Згенеровано з поради щодо добавок:\n" + "\n".join(notes_lines),
    }


async def run_supplement_advice(
    session, *, user_id: int, api_key: Optional[str] = None, force: bool = False,
) -> Optional[str]:
    """Advise which lab markers are worth tracking given the user's currently active
    supplements, and roughly how often. Logs ``ReportLog(kind="supplements")`` — read
    back via ``repository.get_last_report_of_kind`` rather than stored on any one row
    (there's no single row this advice belongs to; it's regenerated whenever the
    supplement list changes). Returns ``None`` when there are no active supplements to
    advise on — the caller shows a friendly message, never spends a Claude call. The
    returned string is the ``SupplementAdvice`` JSON — pass it through
    :func:`parse_supplement_advice` for display/template use.

    ``force=True`` (mirrors ST-19's activity regenerate) skips the dedup-cache *get* —
    for an explicit "спробуй ще раз", since an unchanged supplement list would otherwise
    always replay whatever is already cached, a stale truncated response included. Still
    writes the fresh text back to the cache, so a following non-force call is a hit of
    the new text."""
    from app.db import checkups as checkups_db
    from app.db import supplements as supplements_db

    active = await supplements_db.list_supplements(session, user_id, active_only=True)
    if not active:
        return None
    categories = await checkups_db.recent_categories(session, user_id)
    data = supplement_payload(active, categories)
    return await _run_cached_narration(
        session, user_id=user_id, kind="supplements", model=MODEL_SUPPLEMENTS, context=data,
        cache_key=_supplement_cache_key(data, MODEL_SUPPLEMENTS),
        with_stats_fn=supplement_advice_with_stats,
        question=f"supplements:{len(active)}", api_key=api_key, force=force,
    )
