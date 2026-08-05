"""OPS-11 · hard ceilings on Claude spend — a breaker, not another cost report.

Spend was already *measured* (``ReportLog.cost_usd``, ``/costs``, ``/me/report_logs``) and
never *bounded*: between "expensive" and "catastrophic" stood nothing but human discipline
— an adaptation loop that doesn't terminate, an ``/ask`` agent burning its maximum rounds,
a job retry storm, or plan generation on Opus with ``max_tokens=16000`` fired three times
in a row. This module is the technical guard that was missing.

Two independent ceilings, checked at the two places that can actually see what they need:

* **period ceilings** (``LLM_BUDGET_MONTH_USD`` / ``LLM_BUDGET_DAY_USD``) —
  :func:`enforce`, called from ``client._run_claude``, the single choke point every
  ``*_with_stats`` call passes through. Needs a DB session, so it lives on the async side.
* **per-call ceiling** (``LLM_MAX_CALL_USD``) — :func:`check_call_estimate`, called from
  ``client._complete*``, the only place where model, system prompt, payload AND
  ``max_tokens`` are all known. Pure; no DB, no network. This is the one that actually
  stops a single Opus-16k call, which no monthly average catches in time.

**Priorities, not a blanket block.** Background work (morning report, weekly digest, plan
adaptation, auto-analysis) is cut off first, at ``LLM_BUDGET_SOFT_PCT`` of the ceiling, so
automation can't eat the budget the human's own ``/report`` needs. Interactive paths keep
running to 100% and then get an honest refusal with the current number — never a silent
downgrade to a cheaper model (a quiet substitution is worse than a clear "no").

A **cache hit costs nothing**, so it is neither counted nor blocked: the check runs inside
``_run_claude``, which a cache hit never reaches.

The month/day boundaries are the *user's* (ST-14), not UTC's.
"""
import contextvars
import datetime as dt
import json
import logging
import time
from typing import Optional

from app.analysis.client import PRICES, AnalystError
from app.core.config import settings
from app.core.tz import user_tz

logger = logging.getLogger("claude")

# Totals are memoised for this long so a burst of calls doesn't issue a SELECT each. The
# window is also why a parallel burst can overshoot the ceiling slightly — accepted by the
# ticket (LLM_MAX_CALL_USD bounds the tail; DB locking for exactness isn't worth it).
TOTALS_TTL_S = 60.0

# Rough chars-per-token for a cost *estimate*. Deliberately low (pessimistic): the prompts
# are Ukrainian, which tokenizes far worse than English, and an estimate that under-counts
# would defeat the guard it exists to provide.
CHARS_PER_TOKEN = 2.5


class BudgetExceeded(AnalystError):
    """A ceiling from OPS-11 stopped this call before it was sent.

    An ``AnalystError`` subclass on purpose: every caller (bot handlers, routers, jobs)
    already maps that to a user-visible message and an errored ``ReportLog`` row, so the
    breaker surfaces as a plain explanation rather than a traceback."""


# Set for the whole per-user worker by ``bot.jobs.for_each_user``: everything a scheduled
# job does is background, everything a command/route does is interactive. A ContextVar
# rather than a parameter threaded through ~20 ``run_*`` signatures — the distinction is a
# property of *who called*, and the call stack is exactly what a ContextVar tracks.
_background: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "llm_background", default=False
)


def set_background(value: bool):
    """Mark the current context as background (scheduled) LLM work. Returns the token
    for :func:`reset_background`."""
    return _background.set(value)


def reset_background(token) -> None:
    _background.reset(token)


def is_background() -> bool:
    return _background.get()


# ---------- per-call estimate (pure) ----------

def estimate_tokens(text: str) -> int:
    """Very rough token count for a cost estimate. Never used for accounting — the real
    numbers come back from the API in ``usage`` — only to refuse an obviously huge call."""
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


def estimate_call_usd(model: str, system: str, user_text: str, max_tokens: int,
                      extra_input_tokens: int = 0) -> float:
    """Upper-bound cost of one completion: estimated input tokens at the input price plus
    the FULL ``max_tokens`` at the output price. Pricing the output budget rather than a
    guess at the real length is the point — the runaway case is precisely a call that is
    allowed to generate 16k tokens. ``extra_input_tokens`` covers input that isn't prompt
    text (uploaded images/PDFs, whose base64 length says nothing about their token cost)."""
    pin, pout = PRICES.get(model, (0.0, 0.0))
    in_tok = estimate_tokens(system) + estimate_tokens(user_text) + max(0, extra_input_tokens)
    return in_tok / 1e6 * pin + max(0, max_tokens) / 1e6 * pout


def check_call_estimate(model: str, system: str, user_text: str, max_tokens: int,
                        extra_input_tokens: int = 0) -> None:
    """Raise :class:`BudgetExceeded` when one call's estimated cost exceeds
    ``LLM_MAX_CALL_USD`` (0 disables). Called from ``client._complete*`` before the request
    leaves the process."""
    cap = float(settings.LLM_MAX_CALL_USD or 0)
    if cap <= 0:
        return
    est = estimate_call_usd(model, system, user_text, max_tokens, extra_input_tokens)
    if est <= cap:
        return
    logger.error(
        f"CLAUDE BUDGET blocked one call {model} est~${est:.2f} > cap ${cap:.2f}"
    )
    raise BudgetExceeded(
        f"🛑 Виклик відхилено запобіжником: оцінка вартості ~${est:.2f} "
        f"перевищує ліміт на один запит ${cap:.2f}."
    )


def payload_text(user_content) -> str:
    """Serialise a ``_complete`` user payload for the estimate. Vision uploads pass a list
    of (media_type, base64) pairs — base64 is not prompt text and its length says nothing
    about tokens, so those are measured by count, not by size."""
    if isinstance(user_content, str):
        return user_content
    try:
        return json.dumps(user_content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(user_content)


# ---------- period ceilings (DB) ----------

# user_id -> (fetched_at_monotonic, month_usd, day_usd, month_start_iso, day_start_iso)
_totals: dict = {}


def reset_cache() -> None:
    """Drop the memoised totals (tests, and after a manual budget change)."""
    _totals.clear()


def _bounds(user) -> tuple:
    """(month_start, day_start) as UTC datetimes for the *user's* calendar (ST-14)."""
    tz = user_tz(user)
    now = dt.datetime.now(tz)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    return month_start.astimezone(dt.timezone.utc), day_start.astimezone(dt.timezone.utc)


async def spend_totals(session, user_id: Optional[int], *, fresh: bool = False) -> dict:
    """``{month_usd, day_usd, month_limit, day_limit}`` for this user, memoised for
    :data:`TOTALS_TTL_S`. One SELECT over the current month's ``report_logs`` rows; the day
    total is summed from the same rows in Python instead of a second query.

    ``user_id=None`` (the legacy single-user path) aggregates every row — the ceiling then
    behaves process-wide, which is the safe reading when we can't attribute spend."""
    from sqlalchemy import select

    from app.db.models import ReportLog, User

    now = time.monotonic()
    cached = _totals.get(user_id)
    if not fresh and cached and now - cached[0] < TOTALS_TTL_S:
        month_usd, day_usd = cached[1], cached[2]
    else:
        user = await session.get(User, user_id) if user_id is not None else None
        month_start, day_start = _bounds(user)
        stmt = select(ReportLog.created_at, ReportLog.cost_usd).where(
            ReportLog.created_at >= month_start
        )
        if user_id is not None:
            stmt = stmt.where(ReportLog.user_id == user_id)
        rows = (await session.execute(stmt)).all()
        month_usd = day_usd = 0.0
        # SQLite hands back naive datetimes; compare on a common (naive UTC) basis.
        day_cut = day_start.replace(tzinfo=None)
        for created_at, cost in rows:
            cost = cost or 0.0
            month_usd += cost
            if created_at is None:
                continue
            ts = created_at.astimezone(dt.timezone.utc).replace(tzinfo=None) \
                if created_at.tzinfo else created_at
            if ts >= day_cut:
                day_usd += cost
        _totals[user_id] = (now, month_usd, day_usd)
    return {
        "month_usd": round(month_usd, 4),
        "day_usd": round(day_usd, 4),
        "month_limit": float(settings.LLM_BUDGET_MONTH_USD or 0),
        "day_limit": float(settings.LLM_BUDGET_DAY_USD or 0),
    }


def note_spend(user_id: Optional[int], usd: float) -> None:
    """Add a just-billed call to the memoised totals so consecutive calls inside the same
    TTL window accumulate instead of all seeing the same stale number — without this the
    breaker would be blind for a full minute, which is exactly how long a runaway loop
    needs to do its damage."""
    entry = _totals.get(user_id)
    if entry and usd:
        _totals[user_id] = (entry[0], entry[1] + usd, entry[2] + usd)


def _pct(spent: float, limit: float) -> float:
    return spent / limit if limit > 0 else 0.0


def status(totals: dict) -> dict:
    """Pure view-model over :func:`spend_totals`: shares of each ceiling, the tighter of
    the two, and a month-end projection at the current pace. Feeds the ``/costs`` line and
    the dashboard banner — no DB, so it's trivially testable."""
    month_pct = _pct(totals["month_usd"], totals["month_limit"])
    day_pct = _pct(totals["day_usd"], totals["day_limit"])
    today = dt.date.today()
    days_in_month = (today.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - \
        dt.timedelta(days=1)
    projected = (totals["month_usd"] / today.day) * days_in_month.day if today.day else 0.0
    worst = max(month_pct, day_pct)
    return {
        **totals,
        "month_pct": round(100 * month_pct, 1),
        "day_pct": round(100 * day_pct, 1),
        "pct": round(100 * worst, 1),
        "projected_month_usd": round(projected, 2),
        "warn": worst >= float(settings.LLM_BUDGET_WARN_PCT or 0) > 0,
        "soft_blocked": worst >= float(settings.LLM_BUDGET_SOFT_PCT or 0) > 0,
        "blocked": worst >= 1.0 and (totals["month_limit"] > 0 or totals["day_limit"] > 0),
    }


async def enforce(session, user_id: Optional[int]) -> None:
    """Raise :class:`BudgetExceeded` when this call must not be sent.

    Background work stops at ``LLM_BUDGET_SOFT_PCT``; interactive work runs to 100%. Both
    ceilings (month and day) are checked — whichever is tighter wins. Every limit at 0
    disables the check entirely, which is the documented "behaves exactly as before" mode.
    """
    month_limit = float(settings.LLM_BUDGET_MONTH_USD or 0)
    day_limit = float(settings.LLM_BUDGET_DAY_USD or 0)
    if month_limit <= 0 and day_limit <= 0:
        return
    if session is None:
        # No ledger to read (only reachable from a test harness — every real caller has a
        # session, which ``_run_claude`` makes keyword-REQUIRED precisely so it can't be
        # forgotten). The per-call ceiling in ``check_call_estimate`` still applies.
        return

    totals = await spend_totals(session, user_id)
    background = is_background()
    soft = float(settings.LLM_BUDGET_SOFT_PCT or 0)
    threshold = soft if (background and soft > 0) else 1.0

    for label, spent, limit in (
        ("місячний", totals["month_usd"], month_limit),
        ("денний", totals["day_usd"], day_limit),
    ):
        if limit <= 0 or spent < limit * threshold:
            continue
        logger.error(
            f"CLAUDE BUDGET stop user={user_id} {label} ${spent:.2f}/${limit:.2f} "
            f"({'background' if background else 'interactive'})"
        )
        if background:
            raise BudgetExceeded(
                f"budget: {label} ліміт ${limit:.2f}, витрачено ${spent:.2f} — "
                f"фонові звіти призупинені"
            )
        raise BudgetExceeded(
            f"🛑 Вичерпано {label} бюджет на Claude: ${spent:.2f} з ${limit:.2f}.\n"
            f"Підніми ліміт (LLM_BUDGET_MONTH_USD / LLM_BUDGET_DAY_USD) "
            f"або зачекай до наступного періоду."
        )
