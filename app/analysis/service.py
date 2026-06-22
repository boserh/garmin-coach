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

from app.analysis.prompts import SYSTEM, SYSTEM_ASK
from app.core.config import settings
from app.garmin.schemas import Payload

logger = logging.getLogger("claude")
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

PRICES = {
    "claude-sonnet-4-6": (3.0, 15.0),   # (input, output) $/1M
    "claude-opus-4-8":   (15.0, 75.0),
}
MODEL_DAILY = "claude-sonnet-4-6"
MODEL_DEEP = "claude-opus-4-8"
MODEL_ASK = "claude-sonnet-4-6"   # follow-up Q&A: cheap, grounded in recent reports

ASK_DEFAULT_N = 3   # how many recent daily reports to feed as /ask context

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


def _cache_key(data: dict, question: str, model: str, previous_report: Optional[str] = None) -> str:
    material = {
        "today": dt.date.today().isoformat(),
        "daily": data.get("daily"),
        "activities": data.get("recent_activities"),
        "planned": data.get("planned_runs"),
        "question": question,
        "model": model,
        "prev": previous_report,
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AnalystError(Exception):
    """User-facing analysis error (its text is shown in Telegram / the API)."""


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
) -> Tuple[str, CallStats]:
    """Run analysis and return (text, stats). Raises AnalystError on API failure.

    ``previous_report`` ({"date", "text"}) is yesterday's report passed as context
    for day-over-day continuity (incl. did-the-planned-workout-happen checks). It
    adds ~200-400 input tokens and no output growth.
    """
    model = MODEL_DEEP if deep else MODEL_DAILY
    kind = kind or ("deep" if deep else "report")
    data = _as_dict(payload)
    effective_q = question or _DEFAULT_DAILY_Q

    key = _cache_key(data, effective_q, model, previous_report)
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


def _ask_cache_key(reports: list, question: str, model: str) -> str:
    material = {
        "today": dt.date.today().isoformat(),
        "reports": reports,
        "question": question,
        "model": model,
        "ask": True,
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def ask_with_stats(
    reports: list, question: str, api_key: Optional[str] = None
) -> Tuple[str, CallStats]:
    """Free-form follow-up Q&A grounded in the recent daily reports (no Garmin
    payload). Returns (text, stats); raises AnalystError on API failure. Shares the
    dedup cache and the error handling with :func:`analyze_with_stats`."""
    model = MODEL_ASK
    key = _ask_cache_key(reports, question, model)
    cached = _cache.get(key)
    if cached and cached[1] > _time.time():
        logger.info(f"CLAUDE CACHE HIT  {model} (ask)")
        return cached[0], CallStats(kind="ask", model=model, cached=True)

    user_content = {
        "today": dt.date.today().isoformat(),
        "recent_reports": reports,
        "question": question,
    }
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
    session, question: str, *, n: int = ASK_DEFAULT_N, api_key: Optional[str] = None
) -> str:
    """Fetch the last ``n`` daily reports, answer ``question`` against them, persist
    a ReportLog row (kind="ask"), return the text. Raises AnalystError if there are
    no reports to ground the answer in."""
    from fastapi.concurrency import run_in_threadpool

    from app.garmin import repository

    reports = await repository.get_recent_reports(session, n=n)
    if not reports:
        raise AnalystError("Поки немає жодного звіту для контексту. Спершу зроби /report.")

    try:
        text, stats = await run_in_threadpool(ask_with_stats, reports, question, api_key)
    except AnalystError as e:
        await repository.log_report(
            session, kind="ask", model=MODEL_ASK, ok=False, error=str(e)[:512]
        )
        raise
    await repository.log_report(
        session,
        kind=stats.kind,
        model=stats.model,
        input_tokens=stats.input_tokens,
        output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd,
        ok=True,
        cached=stats.cached,
        report_text=text,
    )
    return text


async def run_analysis(
    session,
    payload: Union[Payload, dict],
    *,
    question: str = "",
    deep: bool = False,
    kind: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """Analyze, persist a ReportLog row (success or failure), return the text.

    Blocking API work runs in a threadpool; the failed-call log is best-effort.
    """
    from fastapi.concurrency import run_in_threadpool

    from app.garmin import repository

    model = MODEL_DEEP if deep else MODEL_DAILY
    kind = kind or ("deep" if deep else "report")

    # Day-over-day continuity: feed yesterday's report as context (daily/morning
    # only — /deep is a one-off deep dive that doesn't need it). Fetched before the
    # new ReportLog is written, so it never picks up the report we're about to make.
    previous_report = None
    if kind != "deep":
        last = await repository.get_last_report(session)
        if last:
            text_prev, date_prev = last
            previous_report = {"date": date_prev, "text": text_prev}

    try:
        text, stats = await run_in_threadpool(
            analyze_with_stats, payload, question, deep, kind, previous_report, api_key
        )
    except AnalystError as e:
        await repository.log_report(
            session, kind=kind, model=model, ok=False, error=str(e)[:512]
        )
        raise
    await repository.log_report(
        session,
        kind=stats.kind,
        model=stats.model,
        input_tokens=stats.input_tokens,
        output_tokens=stats.output_tokens,
        cost_usd=stats.cost_usd,
        ok=True,
        cached=stats.cached,
        report_text=text,
    )
    return text
