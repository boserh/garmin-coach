"""Claude analysis: turn the compact payload into a Ukrainian report.

Moved from the old flat ``claude_analyst``. Keeps the model split (Sonnet for
daily, Opus for deep), the on-disk dedup cache (identical data+question+model →
reuse the answer, volatile ``generated`` excluded from the key), per-call cost
logging, and the user-facing ``AnalystError``. New: every call is also written to
the ``ReportLog`` table for cost/metrics (via :func:`run_analysis`).
"""
import datetime as dt
import hashlib
import json
import logging
import os
import time as _time
import warnings
from dataclasses import dataclass
from typing import Optional, Tuple, Union

from app.analysis.prompts import (
    SYSTEM,
    SYSTEM_ACTIVITY,
    SYSTEM_ASK,
    SYSTEM_PLAN,
    SYSTEM_PLAN_EDIT,
    SYSTEM_STRENGTH_GEN,
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

_DEFAULT_DAILY_Q = (
    "Дай щоденний статус відновлення. "
    "Детальну пораду до пробіжки — лише якщо вона сьогодні/завтра."
)

CACHE_FILE = settings.CLAUDE_CACHE_FILE
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


# ---------- DEDUP CACHE ----------

def _load_cache() -> dict:
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}  # missing or empty/corrupt — start fresh
    except Exception as e:
        logger.warning(f"CACHE load failed: {e}")
        return {}
    now = _time.time()
    return {k: (v[0], v[1]) for k, v in raw.items() if v[1] > now}


def _save_cache() -> None:
    now = _time.time()
    alive = {k: [v[0], v[1]] for k, v in _cache.items() if v[1] > now}
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(alive, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        logger.warning(f"CACHE save failed: {e}")


_cache = _load_cache()


def _as_dict(payload: Union[Payload, dict]) -> dict:
    return payload.model_dump() if isinstance(payload, Payload) else payload


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
               fitness: Optional[dict] = None) -> str:
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
) -> Tuple[str, CallStats]:
    """Run analysis and return (text, stats). Raises AnalystError on API failure.

    ``previous_report`` ({"date", "text"}) is yesterday's report passed as context
    for day-over-day continuity (incl. did-the-planned-workout-happen checks). It
    adds ~200-400 input tokens and no output growth.

    ``weather`` (today's compact forecast, see ``app.weather.fetch_forecast``) lets the
    analyst tailor advice for a run today/tomorrow (heat, rain, wind, run timing). Part
    of the cache key so a forecast change yields a fresh report.
    """
    model = MODEL_DEEP if deep else MODEL_DAILY
    kind = kind or ("deep" if deep else "report")
    data = _as_dict(payload)
    effective_q = question or _DEFAULT_DAILY_Q

    key = _cache_key(data, effective_q, model, previous_report, weather, plan_today, fitness)
    cached = _cache.get(key)
    if cached and cached[1] > _time.time():
        logger.info(f"CLAUDE CACHE HIT  {model}")
        return cached[0], CallStats(kind=kind, model=model, cached=True)  # no tokens

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
    try:
        from anthropic import APIConnectionError, APIStatusError

        msg = _get_client(api_key).messages.create(
            model=model,
            max_tokens=1200,
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
                f"CLAUDE OK  {model}  in={usage.input_tokens} out={usage.output_tokens} "
                f"~${stats.cost_usd:.4f}"
            )
        text = "".join(b.text for b in msg.content if b.type == "text")
        _cache[key] = (text, _time.time() + CACHE_TTL_S)
        _save_cache()
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
    """Back-compatible wrapper returning just the report text."""
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
    AnalystError on API failure. Shares the dedup cache and error handling with
    :func:`analyze_with_stats`."""
    model = MODEL_ASK
    recent_asks = recent_asks or []
    key = _ask_cache_key(reports, question, model, recent_asks)
    cached = _cache.get(key)
    if cached and cached[1] > _time.time():
        logger.info(f"CLAUDE CACHE HIT  {model} (ask)")
        return cached[0], CallStats(kind="ask", model=model, cached=True)

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
        _cache[key] = (text, _time.time() + CACHE_TTL_S)
        _save_cache()
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

    from app.garmin import repository

    reports = await repository.get_recent_reports(session, user_id, n=n)
    if not reports:
        raise AnalystError("Поки немає жодного звіту для контексту. Спершу зроби /report.")
    recent_asks = await repository.get_recent_asks(session, user_id, minutes=ASK_CONTEXT_MIN)

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
    return data


def _activity_cache_key(data: dict, model: str) -> str:
    blob = json.dumps({"activity": data, "model": model, "act": True},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def analyze_activity_with_stats(
    activity_data: dict, api_key: Optional[str] = None
) -> Tuple[str, CallStats]:
    """Analyze one activity. Returns (text, stats); raises AnalystError on API failure.
    Shares the dedup cache (keyed on the activity payload + model)."""
    model = MODEL_ACTIVITY
    key = _activity_cache_key(activity_data, model)
    cached = _cache.get(key)
    if cached and cached[1] > _time.time():
        logger.info(f"CLAUDE CACHE HIT  {model} (activity)")
        return cached[0], CallStats(kind="activity", model=model, cached=True)

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
        _cache[key] = (text, _time.time() + CACHE_TTL_S)
        _save_cache()
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

    from app.garmin import repository

    data = activity_payload(activity)
    q = f"activity #{activity.id} ({activity.type})"
    try:
        text, stats = await run_in_threadpool(analyze_activity_with_stats, data, api_key)
    except AnalystError as e:
        await repository.log_report(
            session, user_id=user_id, kind="activity", model=MODEL_ACTIVITY, ok=False,
            question=q, error=str(e)[:512],
        )
        raise
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

    from app.garmin import repository

    model = MODEL_DEEP if deep else MODEL_DAILY
    kind = kind or ("deep" if deep else "report")

    # Day-over-day continuity: feed yesterday's report as context (daily/morning
    # only — /deep is a one-off deep dive that doesn't need it). Fetched before the
    # new ReportLog is written, so it never picks up the report we're about to make.
    previous_report = None
    plan_today = None
    fitness = None
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

    try:
        text, stats = await run_in_threadpool(
            analyze_with_stats, payload, question, deep, kind, previous_report, api_key, weather,
            plan_today, fitness
        )
    except AnalystError as e:
        await repository.log_report(
            session, user_id=user_id, kind=kind, model=model, ok=False,
            question=question or None, error=str(e)[:512]
        )
        raise
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
