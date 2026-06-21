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

from app.analysis.prompts import SYSTEM
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

CACHE_FILE = settings.CLAUDE_CACHE_FILE
CACHE_TTL_S = 7 * 24 * 3600  # one week

_client = None


def _get_client():
    """Lazily build the Anthropic client so importing this module never requires
    an API key (tests, CLI tooling)."""
    global _client
    if _client is None:
        from anthropic import Anthropic

        key = settings.ANTHROPIC_API_KEY or os.environ["ANTHROPIC_API_KEY"]
        _client = Anthropic(api_key=key)
    return _client


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


def _cache_key(data: dict, question: str, model: str) -> str:
    material = {
        "today": dt.date.today().isoformat(),
        "daily": data.get("daily"),
        "activities": data.get("recent_activities"),
        "planned": data.get("planned_runs"),
        "question": question,
        "model": model,
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
    error: Optional[str] = None


def analyze_with_stats(
    payload: Union[Payload, dict],
    question: str = "",
    deep: bool = False,
    kind: Optional[str] = None,
) -> Tuple[str, CallStats]:
    """Run analysis and return (text, stats). Raises AnalystError on API failure."""
    model = MODEL_DEEP if deep else MODEL_DAILY
    kind = kind or ("deep" if deep else "report")
    data = _as_dict(payload)
    effective_q = question or "Дай щоденний статус відновлення. Детальну пораду до пробіжки — лише якщо вона сьогодні/завтра."

    key = _cache_key(data, effective_q, model)
    cached = _cache.get(key)
    if cached and cached[1] > _time.time():
        logger.info(f"CLAUDE CACHE HIT  {model}")
        return cached[0], CallStats(kind=kind, model=model)  # cache hit: no tokens

    user_content = {
        "today": dt.date.today().isoformat(),
        "data": data,
        "question": effective_q,
    }
    try:
        from anthropic import APIConnectionError, APIStatusError

        msg = _get_client().messages.create(
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


async def run_analysis(
    session,
    payload: Union[Payload, dict],
    *,
    question: str = "",
    deep: bool = False,
    kind: Optional[str] = None,
) -> str:
    """Analyze, persist a ReportLog row (success or failure), return the text.

    Blocking API work runs in a threadpool; the failed-call log is best-effort.
    """
    from fastapi.concurrency import run_in_threadpool

    from app.garmin import repository

    model = MODEL_DEEP if deep else MODEL_DAILY
    kind = kind or ("deep" if deep else "report")
    try:
        text, stats = await run_in_threadpool(
            analyze_with_stats, payload, question, deep, kind
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
    )
    return text
