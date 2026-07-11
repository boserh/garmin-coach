"""Claude analysis: turn the compact payload into a Ukrainian report.

Moved from the old flat ``claude_analyst``. Keeps the model split (Sonnet for
daily, Opus for deep), the dedup cache (identical data+question+model → reuse the
answer, volatile ``generated`` excluded from the key), per-call cost logging, and
the user-facing ``AnalystError``. Every call is also written to the ``ReportLog``
table for cost/metrics (via :func:`run_analysis`).

The dedup cache lives in the DB (``llm_cache`` table, PERF-02) so the bot and the
web process share hits. The sync ``*_with_stats`` functions run in a threadpool and
can't touch the async DB, so the cache get/put lives in the async ``run_*``
wrappers (which have the session); the key functions here are unchanged.
"""
import datetime as dt
import hashlib
import json
import logging
import os
import warnings
from dataclasses import dataclass
from typing import Optional, Tuple, Union

from app.analysis.prompts import (
    SYSTEM,
    SYSTEM_ACTIVITY,
    SYSTEM_ASK,
    SYSTEM_DIGEST,
    SYSTEM_PLAN,
    SYSTEM_PLAN_ADAPT,
    SYSTEM_PLAN_EDIT,
    SYSTEM_STRENGTH_GEN,
    SYSTEM_WEATHER_PLAN,
)
from app.core.config import settings
from app.garmin import exercises
from app.garmin.schemas import GeneratedPlan, Payload, PlanEdit, StrengthSession

logger = logging.getLogger("claude")
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

PRICES = {
    # Anthropic list prices (platform.claude.com/docs/en/about-claude/pricing), $/1M in/out.
    # Sonnet 5 introductory pricing through 2026-08-31 — bump to (3.0, 15.0) on 2026-09-01.
    "claude-sonnet-5":   (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8":   (5.0, 25.0),   # 4.8 dropped to $5/$25 (was $15/$75 on Opus 4.1)
    "claude-fable-5":    (10.0, 50.0),  # newer flagship — 2× Opus 4.8
}
MODEL_DAILY = "claude-sonnet-5"
MODEL_DEEP = "claude-opus-4-8"
MODEL_ASK = "claude-sonnet-5"   # follow-up Q&A: cheap, grounded in recent reports
MODEL_ACTIVITY = "claude-sonnet-5"   # single-activity analysis (/activity)
MODEL_DIGEST = "claude-sonnet-5"     # weekly digest (EP-07): compact payload, once/week
MODEL_PLAN_GEN = MODEL_DEEP          # plan generation default: reasoning-heavy + rare → Opus
MODEL_PLAN_GEN_ALT = "claude-fable-5"   # alternative plan-gen engine (form toggle)
MODEL_PLAN = "claude-sonnet-5"       # plan edits (/plan <text>): small, mechanical → Sonnet

# Which models the plan-setup form may pick from, keyed by the form's short slug.
PLAN_GEN_MODELS = {"opus": MODEL_PLAN_GEN, "fable": MODEL_PLAN_GEN_ALT}


def resolve_plan_model(slug: Optional[str]) -> str:
    """Map the form's model slug ('opus'/'fable') to a real model id; default Opus."""
    return PLAN_GEN_MODELS.get((slug or "").lower(), MODEL_PLAN_GEN)

ASK_DEFAULT_N = 3   # how many recent daily reports to feed as /ask context
ASK_CONTEXT_MIN = 5  # include /ask exchanges from the last N minutes as a conversation thread
RECORDS_CONTEXT_DAYS = 3  # mention a personal record set within the last N days (EP-14)

_DEFAULT_DAILY_Q = (
    "Дай щоденний статус відновлення. "
    "Детальну пораду до пробіжки — лише якщо вона сьогодні/завтра."
)

CACHE_TTL_S = 7 * 24 * 3600  # one week

_clients: dict = {}


def _get_client(api_key: Optional[str] = None):
    """Lazily build (and cache per key) an Anthropic client, so importing this
    module never requires a key. ``api_key`` is the per-user key; it falls back to
    the global .env key for the legacy single-user path."""
    key = api_key or settings.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AnalystError("🔑 Невірний або відсутній ANTHROPIC_API_KEY.")
    client = _clients.get(key)
    if client is None:
        from anthropic import Anthropic

        client = _clients[key] = Anthropic(api_key=key)
    return client


# ---------- DEDUP CACHE KEYS ----------
# The cache itself is the llm_cache table (app.db.llm_cache) — get/put happens in
# the async run_* wrappers below, which have the DB session.

def _as_dict(payload: Union[Payload, dict]) -> dict:
    d = payload.model_dump() if isinstance(payload, Payload) else payload
    # Strip per-point pace/HR series from activities — it's used only for single-activity
    # analysis (activity_payload/_segments), not for daily reports, and adds 5-6 KB per run.
    acts = d.get("recent_activities")
    if acts:
        d = {**d, "recent_activities": [
            {k: v for k, v in a.items() if k != "series"} for a in acts
        ]}
    return d


_FITNESS_KEYS = (
    "vo2max", "fitness_age",
    "race_5k_s", "race_10k_s", "race_half_s", "race_marathon_s",
    "endurance_score", "endurance_class",
    "acwr_pct", "acwr_feedback", "acute_load", "recovery_time_h",
    "readiness_score", "readiness_level",
    "hrv_baseline_low", "hrv_baseline_high",
    "resting_hr", "spo2_avg", "respiration_avg", "breathing_disruption_sev",
)


def _build_fitness_snapshot(ex: dict) -> Optional[dict]:
    """Filter a get_recent_extra coalesced dict down to the fitness keys used in analysis.
    Returns None when no relevant data is present (new user, no history)."""
    snap = {k: ex[k] for k in _FITNESS_KEYS if ex.get(k) is not None}
    return snap or None


def _cache_key(data: dict, question: str, model: str, previous_report: Optional[dict] = None,
               weather: Optional[dict] = None,
               plan_today: Optional[list] = None,
               fitness: Optional[dict] = None,
               records: Optional[list] = None) -> str:
    material = {
        "today": dt.date.today().isoformat(),
        "daily": data.get("daily"),
        "activities": data.get("recent_activities"),
        "planned": data.get("planned_runs"),
        "question": question,
        "model": model,
        "prev": previous_report,
        "weather": weather,
        "plan_today": plan_today,
        "fitness": fitness,
        "records": records,
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AnalystError(Exception):
    """User-facing analysis error (its text is shown in Telegram / the API)."""


def _status_error(e) -> "AnalystError":
    """Map an Anthropic APIStatusError to a user-facing AnalystError."""
    status = getattr(e, "status_code", None)
    body = str(getattr(e, "message", e)).lower()
    if status == 400 and "credit balance is too low" in body:
        return AnalystError(
            "❗️ Закінчились кредити Anthropic API.\n"
            "Поповни баланс на console.anthropic.com → Billing і повтори запит."
        )
    if status == 429:
        return AnalystError("⏳ Ліміт запитів перевищено. Спробуй за хвилину.")
    if status == 401:
        return AnalystError("🔑 Невірний або відсутній ANTHROPIC_API_KEY.")
    if status == 529:
        return AnalystError("🛠 Сервіс Anthropic тимчасово перевантажений. Спробуй пізніше.")
    logger.error(f"CLAUDE ERR {status}: {body[:150]}")
    return AnalystError(f"Помилка API ({status}): {body[:200]}")


@dataclass
class CallStats:
    kind: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    ok: bool = True
    cached: bool = False
    error: Optional[str] = None


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


def _ask_cache_key(reports: list, question: str, model: str, recent_asks: list) -> str:
    material = {
        "today": dt.date.today().isoformat(),
        "reports": reports,
        "recent_asks": recent_asks,
        "question": question,
        "model": model,
        "ask": True,
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
    from fastapi.concurrency import run_in_threadpool

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
        text, stats = await run_in_threadpool(
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


def _activity_cache_key(data: dict, model: str) -> str:
    blob = json.dumps({"activity": data, "model": model, "act": True},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
    from fastapi.concurrency import run_in_threadpool

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
            text, stats = await run_in_threadpool(analyze_activity_with_stats, data, api_key)
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
    from fastapi.concurrency import run_in_threadpool

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

    # Dedup-cache check — same key inputs as analyze_with_stats builds its prompt from
    # (the README pitfall: every piece of Claude context must be part of the key).
    cache_key = _cache_key(_as_dict(payload), question or _DEFAULT_DAILY_Q, model,
                           previous_report, weather, plan_today, fitness, records)
    cached = await llm_cache.get(session, cache_key)
    if cached is not None:
        logger.info(f"CLAUDE CACHE HIT  {model}")
        text, stats = cached, CallStats(kind=kind, model=model, cached=True)
    else:
        try:
            text, stats = await run_in_threadpool(
                analyze_with_stats, payload, question, deep, kind, previous_report, api_key,
                weather, plan_today, fitness, records
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


# ---------- TRAINING PLAN GENERATION ----------

def _complete(model: str, system: str, user_content: dict, kind: str,
              api_key: Optional[str], max_tokens: int = 1200) -> Tuple[str, CallStats]:
    """One Claude completion → (text, stats). Centralises usage accounting + error
    mapping (used by the plan calls; the older report calls keep their inline copies)."""
    from anthropic import APIConnectionError, APIStatusError

    try:
        msg = _get_client(api_key).messages.create(
            model=model, max_tokens=max_tokens, system=system,
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
                f"CLAUDE OK  {model} ({kind})  in={usage.input_tokens} "
                f"out={usage.output_tokens} ~${stats.cost_usd:.4f}"
            )
        text = "".join(b.text for b in msg.content if b.type == "text")
        return text, stats
    except APIStatusError as e:
        raise _status_error(e)
    except APIConnectionError:
        raise AnalystError("🌐 Не вдалось з'єднатися з API. Перевір інтернет і спробуй ще.")


def _coerce_plan(text: str) -> GeneratedPlan:
    """Parse Claude's reply into a GeneratedPlan, tolerating ``` fences / surrounding
    prose by slicing to the outermost {...}."""
    s = text.strip()
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        s = s[i:j + 1]
    return GeneratedPlan(**json.loads(s))


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
    from fastapi.concurrency import run_in_threadpool

    from app.garmin import repository

    recent_runs = [a for a in await repository.list_activities(session, user_id, n=10)
                   if "run" in (a.get("type") or "")]
    recovery = await repository.read_history(session, user_id, days=30)
    weekly_volume = await repository.weekly_run_volume(session, user_id, weeks=8)
    ex = await repository.get_recent_extra(session, user_id, days=21)
    fitness = _build_fitness_snapshot(ex)
    context = {
        "today": dt.date.today().isoformat(),
        "goal": goal, "start_date": start_date, "target_date": target_date,
        "days_per_week": days_per_week, "intensity": intensity,
        "run_days": run_days, "long_run_day": long_run_day, "intake": intake,
        "recent_runs": recent_runs, "recovery": recovery[-14:],
        "weekly_volume": weekly_volume or None,
        "fitness": fitness or None,
    }
    logger.info(f"PLAN generating user={user_id} goal={goal} ({len(recent_runs)} recent runs)")
    try:
        plan_out, stats = await run_in_threadpool(
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
    # Optional strength on the chosen weekdays. Two sources, both best-effort (never fail
    # plan creation): saved Day 1/Day 2 workouts the user picked (cloned to our own copies
    # on push), and free-text "інше…" descriptions we generate from scratch here.
    strength = (intake or {}).get("strength") or {}
    assignments = strength.get("assignments") or {}
    custom = strength.get("custom") or {}
    if strength.get("enabled") and (assignments or custom):
        try:
            from app.garmin import client, workout_export
            amap: dict = {}
            snapshots: dict = {}
            if assignments:
                saved = {w["id"]: workout_export.clean_workout_name(w["name"])
                         for w in await run_in_threadpool(client.fetch_workouts)}
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
                        }
            # Generate each distinct free-text session once, sanitise categories, and lay it
            # on its weekday as a from-scratch strength_plan (built natively on push).
            custom_plans: dict = {}
            gen_cache: dict = {}
            for slug, desc in custom.items():
                key = (desc or "").strip().lower()
                if not key:
                    continue
                if key not in gen_cache:
                    try:
                        sess, _ = await run_in_threadpool(
                            generate_strength_with_stats,
                            {"description": desc, "fitness": fitness or None,
                             "exercise_categories": exercises.CATEGORIES},
                            api_key, gen_model)
                        gen_cache[key] = repository._sanitize_strength(sess)
                    except Exception:
                        logger.exception(f"PLAN strength gen failed user={user_id}")
                        gen_cache[key] = None
                if gen_cache[key]:
                    custom_plans[slug] = gen_cache[key]
            n = await repository.add_strength_workouts(
                session, plan, amap, snapshots, custom_plans)
            logger.info(f"PLAN strength user={user_id}: +{n} sessions "
                        f"({len(amap)} saved, {len(custom_plans)} custom)")
        except Exception:
            logger.exception(f"PLAN strength add failed user={user_id}")
    await repository.log_report(
        session, user_id=user_id, kind=stats.kind, model=stats.model,
        input_tokens=stats.input_tokens, output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd, ok=True, cached=stats.cached,
        question=f"plan: {goal}", report_text=plan_out.summary,
    )
    return plan


def _coerce_edit(text: str) -> PlanEdit:
    s = text.strip()
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        s = s[i:j + 1]
    return PlanEdit(**json.loads(s))


def plan_edit_with_stats(
    context: dict, api_key: Optional[str] = None
) -> Tuple[PlanEdit, CallStats]:
    """Turn a free-text instruction + current workouts into a structured PlanEdit
    (proposed only — not applied). One retry on a parse miss, else AnalystError."""
    model = MODEL_PLAN
    text, stats = _complete(model, SYSTEM_PLAN_EDIT, context, "plan_edit", api_key, max_tokens=1500)
    try:
        return _coerce_edit(text), stats
    except Exception:
        retry = dict(context, _note="Поверни ЛИШЕ валідний JSON за схемою, без тексту навколо.")
        text, stats2 = _complete(
            model, SYSTEM_PLAN_EDIT, retry, "plan_edit", api_key, max_tokens=1500
        )
        stats.input_tokens += stats2.input_tokens
        stats.output_tokens += stats2.output_tokens
        stats.cost_usd += stats2.cost_usd
        try:
            return _coerce_edit(text), stats
        except Exception as e:
            logger.error(f"PLAN_EDIT parse failed: {e}")
            raise AnalystError("Не вдалось зрозуміти зміну. Спробуй переформулювати.")


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
        edit, stats = await run_in_threadpool(plan_edit_with_stats, context, api_key)
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
    model = MODEL_PLAN
    text, stats = _complete(model, SYSTEM_PLAN_ADAPT, context, "adapt", api_key, max_tokens=1500)
    try:
        return _coerce_edit(text), stats
    except Exception:
        retry = dict(context, _note="Поверни ЛИШЕ валідний JSON за схемою, без тексту навколо.")
        text, stats2 = _complete(
            model, SYSTEM_PLAN_ADAPT, retry, "adapt", api_key, max_tokens=1500
        )
        stats.input_tokens += stats2.input_tokens
        stats.output_tokens += stats2.output_tokens
        stats.cost_usd += stats2.cost_usd
        try:
            return _coerce_edit(text), stats
        except Exception as e:
            logger.error(f"PLAN_ADAPT parse failed: {e}")
            raise AnalystError("Не вдалось сформувати пропозицію адаптації плану.")


async def run_plan_adaptation(
    session, *, user_id: int, api_key: Optional[str] = None,
    trigger: str = "weekly", window_days: int = ADAPT_WINDOW_DAYS_DEFAULT,
):
    """Look at the active plan's upcoming window, compliance (EP-01) and recovery/load
    signals; propose a correction (empty ``operations`` if the plan is fine). Does NOT
    apply the change — the caller confirms via bot buttons, same as :func:`run_plan_edit`.

    Returns ``(plan, PlanEdit)``, ``(None, None)`` when there's no active plan, or
    ``(plan, None)`` when the plan's adjust level is "off" (no Claude call, no log —
    adaptation is disabled for this plan). Logs ``ReportLog(kind="adapt")`` on every
    real call (even a no-op) so adaptation cost is tracked. ``trigger`` picks the
    prompt framing ("weekly" review of the next ``window_days`` vs a "morning" one-off
    nudge, called with ``window_days=0`` so only today's session is in scope);
    ``window_days`` also bounds which operation dates are kept — anything the model
    proposes outside ``today..today+window_days`` is dropped. The plan's adjust level
    (ST-07) further bounds *what* the kept operations may do — see
    :func:`_filter_ops_to_level`.
    """
    from fastapi.concurrency import run_in_threadpool

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
    days_to_target = _days_to_target(plan.target_date, today)
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
    }
    try:
        edit, stats = await run_in_threadpool(plan_adapt_with_stats, context, api_key)
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
    from fastapi.concurrency import run_in_threadpool

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
        edit, stats = await run_in_threadpool(weather_plan_with_stats, context, api_key)
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


# ---------- WEEKLY DIGEST (EP-07) ----------

DIGEST_VOLUME_WEEKS = 4        # weekly_run_volume window fed as the volume trend
DIGEST_COMPLIANCE_WEEKS = 2    # how many recent ISO weeks of compliance to include
DIGEST_RECOVERY_DAYS = 14      # recovery trend window
DIGEST_RECORDS_DAYS = 30       # personal records set in the last month (EP-14)


def _digest_cache_key(context: dict, model: str) -> str:
    """Key the weekly digest on the ISO week + the computed data slice (not ``today``),
    so a repeat within the same week/data is a cache hit — see the README pitfall: every
    piece of Claude context must be in the key."""
    material = {
        "iso_week": context.get("iso_week"),
        "week": context.get("week"),
        "weekly_volume": context.get("weekly_volume"),
        "compliance": context.get("compliance"),
        "recovery": context.get("recovery"),
        "fitness": context.get("fitness"),
        "goal": context.get("goal"),
        "records": context.get("records"),
        "model": model,
        "digest": True,
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
    from fastapi.concurrency import run_in_threadpool

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
            text, stats = await run_in_threadpool(digest_with_stats, context, api_key)
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
