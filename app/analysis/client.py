"""Anthropic client pool, model/pricing config, cost accounting and error mapping.

The single place that talks to the Anthropic SDK. Everything cost- or model-related
lives here: the per-key client cache (``_get_client``), the price table (``PRICES``),
the ``CallStats`` accounting record, the user-facing ``AnalystError`` + status mapping,
the dedicated Claude thread pool (``_run_claude``), and the shared ``_complete`` helper
that turns one ``messages.create`` into ``(text, stats)``.

Split out of the old flat ``analysis.service`` (CODE-01). Importing this module never
requires a key — the client is built lazily on first use.
"""
import asyncio
import json
import logging
import os
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional, Tuple

from app.core.config import settings

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
SONNET_4_6 = "claude-sonnet-4-6"
SONNET_5 = "claude-sonnet-5"
OPUS_4_8 = "claude-opus-4-8"
FABLE_5 = "claude-fable-5"

MODEL_DAILY = SONNET_5       # daily report: small, mechanical → Sonnet
MODEL_DEEP = OPUS_4_8        # deep-dive analysis: reasoning-heavy + rare → Opus
MODEL_ASK = SONNET_5         # follow-up Q&A: cheap, grounded in recent reports
MODEL_ACTIVITY = SONNET_5    # single-activity analysis (/activity)
MODEL_DIGEST = SONNET_5      # weekly digest (EP-07): compact payload, once/week
MODEL_COMPARE = SONNET_5     # compare-past-self (NF-06): narrate two windows, on request
MODEL_INJURY = SONNET_5      # injury-radar advisory (NF-04): narrate signals, rare
MODEL_PLAN_GEN = OPUS_4_8    # plan generation default: reasoning-heavy + rare → Opus
MODEL_PLAN_GEN_ALT = FABLE_5 # alternative plan-gen engine (form toggle)
MODEL_PLAN = SONNET_5        # plan edits (/plan <text>): small, mechanical → Sonnet

# Which models the plan-setup form may pick from, keyed by the form's short slug.
PLAN_GEN_MODELS = {"opus": MODEL_PLAN_GEN, "fable": MODEL_PLAN_GEN_ALT}


def resolve_plan_model(slug: Optional[str]) -> str:
    """Map the form's model slug ('opus'/'fable') to a real model id; default Opus."""
    return PLAN_GEN_MODELS.get((slug or "").lower(), MODEL_PLAN_GEN)


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


# Claude calls run on their OWN small thread pool (PERF-04b), kept separate from
# the shared anyio threadpool that Garmin logins/fetches and DB work use. An LLM
# call holds its thread for seconds; if it shared the ~40-thread anyio pool, a
# burst of reports could starve the pool that fast Garmin fetches need, so quick
# operations would queue behind slow ones. A handful of workers is plenty for a
# personal deployment (concurrency here is also bounded by Anthropic rate limits).
_claude_executor = ThreadPoolExecutor(
    max_workers=max(1, settings.CLAUDE_MAX_WORKERS), thread_name_prefix="claude"
)


async def _run_claude(fn, *args):
    """Run a blocking ``*_with_stats`` Claude call on the dedicated pool.

    Drop-in for ``run_in_threadpool`` on the LLM path so it no longer competes
    with Garmin work for anyio's threads. The Claude functions take positional
    args only (no ContextVar dependency — unlike the Garmin provider path), so a
    bare ``run_in_executor`` is enough."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_claude_executor, fn, *args)


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
