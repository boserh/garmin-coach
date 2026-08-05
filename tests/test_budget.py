"""OPS-11 · the LLM budget circuit breaker.

Covers the two ceilings (period + per-call), the priority rule that stops background work
before interactive work, and — the important one — a parametrized sweep asserting that
**every** ``_run_claude`` call site passes the session through, so a new LLM path added
later can't quietly slip past the breaker.
"""
import ast
import datetime as dt
import inspect
import pathlib

import pytest

from app.analysis import budget
from app.analysis.client import SONNET_5, AnalystError, _run_claude
from app.core.config import settings
from app.db.models import ReportLog, User

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "analysis"


@pytest.fixture(autouse=True)
def _clean_budget_cache():
    budget.reset_cache()
    yield
    budget.reset_cache()


async def _user(session, tz: str = "Europe/Warsaw") -> User:
    u = User(email=f"b{tz}@example.com", password_hash="x", timezone=tz)
    session.add(u)
    await session.commit()
    return u


async def _spend(session, user_id: int, usd: float, *, when=None, cached: bool = False):
    session.add(ReportLog(
        user_id=user_id, kind="report", model=SONNET_5, cost_usd=usd, cached=cached,
        created_at=when or dt.datetime.now(dt.timezone.utc),
    ))
    await session.commit()


# ---------- per-call estimate ----------

def test_call_estimate_prices_the_whole_output_budget():
    """The runaway case is a call ALLOWED to generate 16k tokens, so the estimate prices
    max_tokens in full rather than guessing the real reply length."""
    small = budget.estimate_call_usd("claude-opus-4-8", "sys", "hi", 1000)
    big = budget.estimate_call_usd("claude-opus-4-8", "sys", "hi", 16000)
    assert big > small * 10


def test_per_call_ceiling_blocks_opus_16k(monkeypatch):
    monkeypatch.setattr(settings, "LLM_MAX_CALL_USD", 0.10)
    with pytest.raises(budget.BudgetExceeded):
        budget.check_call_estimate("claude-opus-4-8", "system", "payload", 16000)


def test_per_call_ceiling_disabled_by_zero(monkeypatch):
    monkeypatch.setattr(settings, "LLM_MAX_CALL_USD", 0)
    budget.check_call_estimate("claude-opus-4-8", "system", "payload", 16000)  # no raise


def test_budget_exceeded_is_an_analyst_error():
    """Every caller already maps AnalystError to a user-visible message + an errored
    ReportLog row, so the breaker surfaces as an explanation, never a traceback."""
    assert issubclass(budget.BudgetExceeded, AnalystError)


def test_payload_text_survives_unserialisable_content():
    class Weird:
        pass

    assert budget.payload_text({"x": Weird()})   # falls back to str(), doesn't raise


# ---------- period ceilings ----------

@pytest.mark.asyncio
async def test_under_the_ceiling_nothing_happens(session, monkeypatch):
    monkeypatch.setattr(settings, "LLM_BUDGET_MONTH_USD", 10.0)
    monkeypatch.setattr(settings, "LLM_BUDGET_DAY_USD", 5.0)
    u = await _user(session)
    await _spend(session, u.id, 0.5)
    await budget.enforce(session, u.id)   # no raise


@pytest.mark.asyncio
async def test_month_ceiling_blocks_interactive(session, monkeypatch):
    monkeypatch.setattr(settings, "LLM_BUDGET_MONTH_USD", 1.0)
    monkeypatch.setattr(settings, "LLM_BUDGET_DAY_USD", 0)
    u = await _user(session)
    await _spend(session, u.id, 1.5)
    with pytest.raises(budget.BudgetExceeded):
        await budget.enforce(session, u.id)


@pytest.mark.asyncio
async def test_background_stops_first(session, monkeypatch):
    """The whole point of the soft threshold: at 92% of the ceiling the morning job is
    already off, so the human's own /report still has budget left to run."""
    monkeypatch.setattr(settings, "LLM_BUDGET_MONTH_USD", 10.0)
    monkeypatch.setattr(settings, "LLM_BUDGET_DAY_USD", 0)
    monkeypatch.setattr(settings, "LLM_BUDGET_SOFT_PCT", 0.9)
    u = await _user(session)
    await _spend(session, u.id, 9.2)

    await budget.enforce(session, u.id)          # interactive: still allowed

    token = budget.set_background(True)
    try:
        with pytest.raises(budget.BudgetExceeded):
            await budget.enforce(session, u.id)  # background: stopped
    finally:
        budget.reset_background(token)


@pytest.mark.asyncio
async def test_zero_limits_disable_everything(session, monkeypatch):
    monkeypatch.setattr(settings, "LLM_BUDGET_MONTH_USD", 0)
    monkeypatch.setattr(settings, "LLM_BUDGET_DAY_USD", 0)
    u = await _user(session)
    await _spend(session, u.id, 999.0)
    await budget.enforce(session, u.id)   # no raise — behaves exactly as before OPS-11


@pytest.mark.asyncio
async def test_day_ceiling_uses_the_users_own_midnight(session, monkeypatch):
    """ST-14: a call made at 23:30 in Kyiv belongs to the Kyiv day, not the UTC one."""
    monkeypatch.setattr(settings, "LLM_BUDGET_MONTH_USD", 0)
    monkeypatch.setattr(settings, "LLM_BUDGET_DAY_USD", 1.0)
    u = await _user(session, tz="Europe/Kyiv")
    kyiv = dt.datetime.now(budget.user_tz(u))
    # Yesterday, 30 minutes before the user's local midnight — outside today's window
    # even though in UTC it can still read as "today" for part of the year.
    yesterday_late = (kyiv - dt.timedelta(days=1)).replace(hour=23, minute=30)
    await _spend(session, u.id, 5.0, when=yesterday_late.astimezone(dt.timezone.utc))
    await budget.enforce(session, u.id)   # yesterday's spend doesn't count against today


@pytest.mark.asyncio
async def test_note_spend_accumulates_inside_the_ttl(session, monkeypatch):
    """Without folding each call's cost into the memo, the breaker would be blind for a
    full TTL window — exactly how long a runaway loop needs to do its damage."""
    monkeypatch.setattr(settings, "LLM_BUDGET_MONTH_USD", 1.0)
    monkeypatch.setattr(settings, "LLM_BUDGET_DAY_USD", 0)
    u = await _user(session)
    await budget.spend_totals(session, u.id)     # prime the memo at $0
    budget.note_spend(u.id, 2.0)                 # a call just billed, DB not re-read
    with pytest.raises(budget.BudgetExceeded):
        await budget.enforce(session, u.id)


@pytest.mark.asyncio
async def test_totals_are_user_scoped(session, monkeypatch):
    monkeypatch.setattr(settings, "LLM_BUDGET_MONTH_USD", 1.0)
    a = await _user(session, tz="Europe/Warsaw")
    b = await _user(session, tz="Europe/Kyiv")
    await _spend(session, a.id, 5.0)
    with pytest.raises(budget.BudgetExceeded):
        await budget.enforce(session, a.id)
    await budget.enforce(session, b.id)   # b's own ledger is empty


# ---------- status view-model ----------

def test_status_flags_and_projection(monkeypatch):
    monkeypatch.setattr(settings, "LLM_BUDGET_WARN_PCT", 0.8)
    monkeypatch.setattr(settings, "LLM_BUDGET_SOFT_PCT", 0.9)
    st = budget.status({"month_usd": 8.5, "day_usd": 0.0,
                        "month_limit": 10.0, "day_limit": 0.0})
    assert st["warn"] and not st["blocked"]
    assert st["month_pct"] == 85.0
    assert st["projected_month_usd"] > 0

    full = budget.status({"month_usd": 10.0, "day_usd": 0.0,
                          "month_limit": 10.0, "day_limit": 0.0})
    assert full["blocked"]


def test_status_with_no_limits_never_warns():
    st = budget.status({"month_usd": 999.0, "day_usd": 999.0,
                        "month_limit": 0.0, "day_limit": 0.0})
    assert not st["warn"] and not st["blocked"]


# ---------- the checkpoint itself ----------

@pytest.mark.asyncio
async def test_run_claude_enforces_before_calling(session, monkeypatch):
    monkeypatch.setattr(settings, "LLM_BUDGET_MONTH_USD", 1.0)
    u = await _user(session)
    await _spend(session, u.id, 2.0)
    called = []

    with pytest.raises(budget.BudgetExceeded):
        await _run_claude(lambda _: called.append(1), None,
                          session=session, user_id=u.id)
    assert not called, "the breaker must stop the call BEFORE it is sent"


@pytest.mark.asyncio
async def test_cache_hit_is_neither_counted_nor_blocked(session, monkeypatch):
    """A dedup-cache hit is free, so blocking it would be pure loss: the user gets no
    answer and the budget saves nothing. It never reaches the checkpoint by construction —
    this pins that down."""
    from app.analysis.reports import _run_cached_narration
    from app.db import llm_cache

    monkeypatch.setattr(settings, "LLM_BUDGET_MONTH_USD", 1.0)
    u = await _user(session)
    await _spend(session, u.id, 99.0)          # budget long gone
    await llm_cache.put(session, "k-cached", "старий звіт", 3600)

    def _must_not_run(*a, **kw):
        raise AssertionError("cache hit must not reach Claude")

    text = await _run_cached_narration(
        session, user_id=u.id, kind="report", model=SONNET_5, context={},
        cache_key="k-cached", with_stats_fn=_must_not_run, question="q",
    )
    assert text == "старий звіт"


def test_run_claude_requires_a_session():
    """Keyword-required on purpose: a future call path that forgets it fails loudly at the
    call site instead of quietly slipping past the ceiling."""
    sig = inspect.signature(_run_claude)
    p = sig.parameters["session"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is inspect.Parameter.empty


def _run_claude_calls():
    """Every ``await _run_claude(...)`` in the analysis package, as (file, lineno, node)."""
    out = []
    for path in sorted(ANALYSIS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_run_claude"):
                out.append((path.name, node.lineno, node))
    return out


def test_every_llm_path_goes_through_the_breaker():
    """The parametrized sweep the ticket asks for, done structurally: no ``_run_claude``
    call site anywhere in app/analysis may omit ``session=`` — that is what makes the
    breaker unbypassable rather than merely well-intentioned. A new LLM path added later
    fails HERE, at review time, not on the bill."""
    calls = _run_claude_calls()
    assert calls, "no _run_claude call sites found — did the checkpoint move?"
    missing = [
        f"{name}:{lineno}" for name, lineno, node in calls
        if "session" not in {kw.arg for kw in node.keywords}
    ]
    assert not missing, f"_run_claude call sites bypassing the budget check: {missing}"


def test_every_llm_path_reports_its_user():
    """``user_id=`` too — a ceiling that can't attribute spend degrades to a process-wide
    one, which is not what the per-user ledger promises."""
    missing = [
        f"{name}:{lineno}" for name, lineno, node in _run_claude_calls()
        if "user_id" not in {kw.arg for kw in node.keywords}
    ]
    assert not missing, f"_run_claude call sites without a user_id: {missing}"
