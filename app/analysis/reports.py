"""Narrative Claude calls: the daily/deep report, ``/ask`` follow-ups, single-activity
analysis, the weekly digest, compare-past-self and the injury-radar advisory.

Everything that turns the compact payload (or already-computed numbers) into a
Ukrainian narration for the user, with the dedup-cache get/put fronting each ``run_*``
wrapper. Split out of the old flat ``analysis.service`` (CODE-01). The plan-adaptation
helpers ``_days_to_target``/``_recent_compliance`` are reused from ``plans`` for the
weekly digest's goal + compliance slices.
"""
import datetime as dt
import json
import logging
from typing import Optional, Tuple, Union

from app.analysis.cache import (
    CACHE_TTL_S,
    _activity_cache_key,
    _as_dict,
    _ask_cache_key,
    _build_fitness_snapshot,
    _build_multisport,
    _cache_key,
    _compare_cache_key,
    _digest_cache_key,
)
from app.analysis.client import (
    MODEL_ACTIVITY,
    MODEL_ASK,
    MODEL_COMPARE,
    MODEL_DAILY,
    MODEL_DEEP,
    MODEL_DIGEST,
    MODEL_HEALTH,
    MODEL_INJURY,
    PRICES,
    AnalystError,
    CallStats,
    _complete,
    _complete_tools,
    _get_client,
    _run_claude,
    _status_error,
)
from app.analysis.plans import _days_to_target, _recent_compliance
from app.analysis.prompts import (
    SYSTEM,
    SYSTEM_ACTIVITY,
    SYSTEM_ASK_TOOLS,
    SYSTEM_COMPARE,
    SYSTEM_DIGEST,
    SYSTEM_HEALTH,
    SYSTEM_INJURY,
)
from app.core.config import settings
from app.garmin.schemas import Payload

logger = logging.getLogger("claude")

ASK_DEFAULT_N = 3   # how many recent daily reports to feed as /ask context
ASK_CONTEXT_MIN = 5  # include /ask exchanges from the last N minutes as a conversation thread
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
) -> Tuple[str, CallStats]:
    """Run analysis and return (text, stats). Raises AnalystError on API failure.

    ``previous_report`` ({"date", "text"}) is yesterday's report passed as context
    for day-over-day continuity (incl. did-the-planned-workout-happen checks). It
    adds ~200-400 input tokens and no output growth.

    ``weather`` (today's compact forecast, see ``app.weather.fetch_forecast``) lets the
    analyst tailor advice for a run today/tomorrow (heat, rain, wind, run timing). Part
    of the cache key so a forecast change yields a fresh report.

    No dedup-cache check here — this runs sync in a threadpool with no DB access;
    :func:`run_analysis` fronts it with the shared ``llm_cache`` get/put.
    """
    model = MODEL_DEEP if deep else MODEL_DAILY
    kind = kind or ("deep" if deep else "report")
    data = _as_dict(payload)
    effective_q = question or _DEFAULT_DAILY_Q

    user_content = {
        "today": dt.date.today().isoformat(),
        "data": data,
        "question": effective_q,
    }
    if previous_report:
        user_content["previous_report"] = previous_report
    if weather:
        user_content["weather"] = weather
    if plan_today:
        user_content["plan_today"] = plan_today
    if fitness:
        user_content["fitness"] = fitness
    if records:
        user_content["records"] = records
    if norm:
        user_content["norm"] = norm
    if subjective:
        user_content["subjective"] = subjective
    try:
        from anthropic import APIConnectionError, APIStatusError

        msg = _get_client(api_key).messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM,
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


def analyze(payload: Union[Payload, dict], question: str = "", deep: bool = False) -> str:
    """Back-compatible wrapper returning just the report text. NB: bypasses the
    dedup cache (that lives in :func:`run_analysis`, which needs a DB session)."""
    text, _ = analyze_with_stats(payload, question=question, deep=deep)
    return text


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
) -> str:
    """Analyze, persist a ReportLog row (success or failure), return the text.

    Blocking API work runs in a threadpool; the failed-call log is best-effort.
    ``weather`` (optional) is today's forecast passed through to the analyst.
    """
    from app.db import llm_cache
    from app.garmin import repository

    model = MODEL_DEEP if deep else MODEL_DAILY
    kind = kind or ("deep" if deep else "report")

    # Day-over-day continuity: feed yesterday's report as context (daily/morning
    # only — /deep is a one-off deep dive that doesn't need it). Fetched before the
    # new ReportLog is written, so it never picks up the report we're about to make.
    previous_report = None
    plan_today = None
    fitness = None
    records = None
    norm = None
    subjective = None
    if kind != "deep":
        last = await repository.get_last_report(session, user_id)
        if last:
            text_prev, date_prev = last
            previous_report = {"date": date_prev, "text": text_prev}

        if user_id is not None:
            ws = await repository.upcoming_plan_workouts(session, user_id, days=2)
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

    # Dedup-cache check — same key inputs as analyze_with_stats builds its prompt from
    # (the README pitfall: every piece of Claude context must be part of the key).
    cache_key = _cache_key(_as_dict(payload), question or _DEFAULT_DAILY_Q, model,
                           previous_report, weather, plan_today, fitness, records, norm,
                           subjective)
    cached = await llm_cache.get(session, cache_key)
    if cached is not None:
        logger.info(f"CLAUDE CACHE HIT  {model}")
        text, stats = cached, CallStats(kind=kind, model=model, cached=True)
    else:
        try:
            text, stats = await _run_claude(
                analyze_with_stats, payload, question, deep, kind, previous_report, api_key,
                weather, plan_today, fitness, records, norm, subjective
            )
        except AnalystError as e:
            await repository.log_report(
                session, user_id=user_id, kind=kind, model=model, ok=False,
                question=question or None, error=str(e)[:512]
            )
            raise
        await llm_cache.put(session, cache_key, text, CACHE_TTL_S)
    await repository.log_report(
        session,
        user_id=user_id,
        kind=stats.kind,
        model=stats.model,
        input_tokens=stats.input_tokens,
        output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd,
        ok=True,
        cached=stats.cached,
        question=question or None,
        report_text=text,
    )
    return text


# EP-09: /ask is a bounded tool-use agent, not a single completion — the first tool-use
# loop in the project (a deliberate departure from the "prompt-for-JSON, no SDK tool-use"
# choice elsewhere; see CLAUDE.md). A question already answered by recent_reports/recent_qa
# resolves in round 1 with no tool calls (the old cheap path still happens, it's just no
# longer a separate code path); anything needing more history drives query_activities /
# query_daily / aggregate_weekly / get_activity_detail against the FULL stored history.
MAX_ASK_ROUNDS = 5             # hard cap on tool-use round trips per question
MAX_ASK_TOTAL_TOKENS = 60_000  # combined in+out tokens across all rounds — runaway guard
ASK_TOOL_MAX_TOKENS = 1200     # answers are short; a tool-call round is just a JSON stub

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
            if act is None:
                return {"error": f"no activity with id={aid} for this user"}
            return activity_payload(act)
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

def _segments(series: list, n: int = 6) -> list:
    """Collapse a run's per-point series into ~n segments (avg pace + HR each) so the
    LLM sees pacing and HR drift without the full point cloud."""
    pts = [p for p in series if p.get("p") is not None or p.get("hr") is not None]
    if not pts:
        return []
    size = max(1, len(pts) // n)
    segs = []
    for i in range(0, len(pts), size):
        chunk = pts[i:i + size]
        paces = [c["p"] for c in chunk if c.get("p") is not None]
        hrs = [c["hr"] for c in chunk if c.get("hr") is not None]
        ds = [c["d"] for c in chunk if c.get("d") is not None]
        segs.append({
            "from_km": round(ds[0], 2) if ds else None,
            "to_km": round(ds[-1], 2) if ds else None,
            "avg_pace": round(sum(paces) / len(paces), 2) if paces else None,
            "avg_hr": round(sum(hrs) / len(hrs)) if hrs else None,
        })
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
            data["avg_pace"] = round(activity.dur_min / activity.dist_km, 2)
    # EP-12: the runner's subjective check-in (RPE + niggle). Part of the payload, so it
    # also enters the dedup-cache key automatically (_activity_cache_key hashes `data`).
    if getattr(activity, "subjective", None):
        data["subjective"] = activity.subjective
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
    user_content = {"today": dt.date.today().isoformat(), "activity": activity_data}
    try:
        from anthropic import APIConnectionError, APIStatusError

        msg = _get_client(api_key).messages.create(
            model=model, max_tokens=1500, system=SYSTEM_ACTIVITY,
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
    session, activity, *, user_id: Optional[int] = None, api_key: Optional[str] = None
) -> str:
    """Analyze one activity, store the text on the row (``analysis``) for the web detail
    page, log a ReportLog (kind="activity"), and return the text."""
    from app.db import llm_cache
    from app.garmin import repository

    planned = await repository.get_workout_for_activity(session, user_id, activity.id) \
        if user_id is not None else None
    data = activity_payload(activity, planned)
    q = f"activity #{activity.id} ({activity.type})"
    key = _activity_cache_key(data, MODEL_ACTIVITY)
    cached = await llm_cache.get(session, key)
    if cached is not None:
        logger.info(f"CLAUDE CACHE HIT  {MODEL_ACTIVITY} (activity)")
        text, stats = cached, CallStats(kind="activity", model=MODEL_ACTIVITY, cached=True)
    else:
        try:
            text, stats = await _run_claude(analyze_activity_with_stats, data, api_key)
        except AnalystError as e:
            await repository.log_report(
                session, user_id=user_id, kind="activity", model=MODEL_ACTIVITY, ok=False,
                question=q, error=str(e)[:512],
            )
            raise
        await llm_cache.put(session, key, text, CACHE_TTL_S)
    activity.analysis = text
    await repository.log_report(
        session, user_id=user_id, kind=stats.kind, model=stats.model,
        input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd, ok=True, cached=stats.cached,
        question=q, report_text=text,
    )
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
    from app.db import llm_cache
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

    plan = await repository.get_active_plan(session, user_id)
    compliance = None
    goal = None
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
        "records": month_records,
        "has_plan": plan is not None,
    }

    key = _digest_cache_key(context, MODEL_DIGEST)
    cached = await llm_cache.get(session, key)
    if cached is not None:
        logger.info(f"CLAUDE CACHE HIT  {MODEL_DIGEST} (digest)")
        text, stats = cached, CallStats(kind="digest", model=MODEL_DIGEST, cached=True)
    else:
        try:
            text, stats = await _run_claude(digest_with_stats, context, api_key)
        except AnalystError as e:
            await repository.log_report(
                session, user_id=user_id, kind="digest", model=MODEL_DIGEST, ok=False,
                question=f"digest:{this_week}", error=str(e)[:512],
            )
            raise
        await llm_cache.put(session, key, text, CACHE_TTL_S)
    await repository.log_report(
        session, user_id=user_id, kind=stats.kind, model=stats.model,
        input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd, ok=True, cached=stats.cached,
        question=f"digest:{this_week}", report_text=text,
    )
    return text


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
    from app.db import llm_cache
    from app.garmin import repository

    today = dt.date.today()
    cur_start, cur_end, past_start, past_end = compare_mod.window_pair(today, weeks, years_back)
    current = await repository.window_stats(session, user_id, cur_start, cur_end)
    past = await repository.window_stats(session, user_id, past_start, past_end)
    if not compare_mod.has_signal(current, past):
        logger.info(f"COMPARE skip user={user_id}: not enough history in both windows")
        return None

    context = compare_mod.build_context(weeks, years_back, current, past)
    key = _compare_cache_key(context, MODEL_COMPARE)
    cached = await llm_cache.get(session, key)
    if cached is not None:
        logger.info(f"CLAUDE CACHE HIT  {MODEL_COMPARE} (compare)")
        text, stats = cached, CallStats(kind="compare", model=MODEL_COMPARE, cached=True)
    else:
        try:
            text, stats = await _run_claude(compare_with_stats, context, api_key)
        except AnalystError as e:
            await repository.log_report(
                session, user_id=user_id, kind="compare", model=MODEL_COMPARE, ok=False,
                question=f"compare:{weeks}w/{years_back}y", error=str(e)[:512],
            )
            raise
        await llm_cache.put(session, key, text, CACHE_TTL_S)
    await repository.log_report(
        session, user_id=user_id, kind=stats.kind, model=stats.model,
        input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd, ok=True, cached=stats.cached,
        question=f"compare:{weeks}w/{years_back}y", report_text=text,
    )
    return text


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
