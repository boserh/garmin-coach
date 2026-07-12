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
    _get_client,
    _run_claude,
    _status_error,
)
from app.analysis.plans import _days_to_target, _recent_compliance
from app.analysis.prompts import (
    SYSTEM,
    SYSTEM_ACTIVITY,
    SYSTEM_ASK,
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


def ask_with_stats(
    reports: list,
    question: str,
    api_key: Optional[str] = None,
    recent_asks: Optional[list] = None,
) -> Tuple[str, CallStats]:
    """Free-form follow-up Q&A grounded in the recent daily reports (no Garmin
    payload). ``recent_asks`` ([{question, answer}, ...]) is the last few minutes'
    /ask thread so a follow-up can build on it. Returns (text, stats); raises
    AnalystError on API failure. Shares the error handling with
    :func:`analyze_with_stats`; the dedup cache is checked in :func:`run_ask`."""
    model = MODEL_ASK
    recent_asks = recent_asks or []
    user_content = {
        "today": dt.date.today().isoformat(),
        "recent_reports": reports,
        "question": question,
    }
    if recent_asks:
        user_content["recent_qa"] = recent_asks
    try:
        from anthropic import APIConnectionError, APIStatusError

        msg = _get_client(api_key).messages.create(
            model=model,
            max_tokens=1000,
            system=SYSTEM_ASK,
            messages=[{"role": "user",
                       "content": json.dumps(user_content, ensure_ascii=False)}],
        )
        stats = CallStats(kind="ask", model=model)
        usage = getattr(msg, "usage", None)
        if usage:
            pin, pout = PRICES.get(model, (0, 0))
            stats.input_tokens = usage.input_tokens
            stats.output_tokens = usage.output_tokens
            stats.cost_usd = usage.input_tokens / 1e6 * pin + usage.output_tokens / 1e6 * pout
            logger.info(
                f"CLAUDE OK  {model} (ask)  in={usage.input_tokens} out={usage.output_tokens} "
                f"~${stats.cost_usd:.4f}"
            )
        text = "".join(b.text for b in msg.content if b.type == "text")
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


async def run_ask(
    session,
    question: str,
    *,
    user_id: Optional[int] = None,
    n: int = ASK_DEFAULT_N,
    api_key: Optional[str] = None,
) -> str:
    """Fetch the last ``n`` daily reports plus the recent /ask thread, answer
    ``question`` against them, persist a ReportLog row (kind="ask", with the
    question), return the text. Raises AnalystError if there are no reports to ground
    the answer in."""
    from app.db import llm_cache
    from app.garmin import repository

    reports = await repository.get_recent_reports(session, user_id, n=n)
    if not reports:
        raise AnalystError("Поки немає жодного звіту для контексту. Спершу зроби /report.")
    recent_asks = await repository.get_recent_asks(session, user_id, minutes=ASK_CONTEXT_MIN)

    key = _ask_cache_key(reports, question, MODEL_ASK, recent_asks)
    cached = await llm_cache.get(session, key)
    if cached is not None:
        logger.info(f"CLAUDE CACHE HIT  {MODEL_ASK} (ask)")
        text, stats = cached, CallStats(kind="ask", model=MODEL_ASK, cached=True)
        await repository.log_report(
            session, user_id=user_id, kind=stats.kind, model=stats.model, ok=True,
            cached=True, question=question, report_text=text,
        )
        return text

    try:
        text, stats = await _run_claude(
            ask_with_stats, reports, question, api_key, recent_asks
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
        cached=stats.cached,
        question=question,
        report_text=text,
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


def activity_payload(activity) -> dict:
    """Compact LLM input for one ActivityRecord — summary fields plus run segments."""
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
            model=model, max_tokens=1000, system=SYSTEM_ACTIVITY,
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

    data = activity_payload(activity)
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
