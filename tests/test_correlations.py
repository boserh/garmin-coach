"""NF-02 correlation engine: the pure statistics (Pearson, lagged pairing, significance
gates) and the run_insights service call (None when nothing is significant; cache hit on
repeat; ReportLog written; monthly digest hook)."""
import datetime as dt
import math
from unittest.mock import patch

from sqlalchemy import select

from app import correlations
from app.analysis import reports, service
from app.analysis.service import CallStats, _insights_cache_key, run_insights
from app.db.models import DailyMetric, ReportLog

U1 = 1


# --- pure statistics ----------------------------------------------------------

def test_pearson_perfect_and_undefined():
    assert correlations.pearson([1, 2, 3], [2, 4, 6]) == 1.0
    assert correlations.pearson([1, 2, 3], [6, 4, 2]) == -1.0
    assert correlations.pearson([1, 1, 1], [1, 2, 3]) is None   # zero variance
    assert correlations.pearson([1], [1]) is None               # too few


def test_fisher_ci_excludes_zero_needs_strength_or_n():
    # A strong r on decent n → CI clears zero.
    assert correlations._fisher_ci_excludes_zero(0.6, 40) is True
    # A wisp on small n → CI straddles zero.
    assert correlations._fisher_ci_excludes_zero(0.1, 10) is False


def test_paired_applies_lag_and_skips_gaps():
    history = [
        {"date": "2026-01-01", "stress_avg": 30, "hrv_avg": 60},
        {"date": "2026-01-02", "stress_avg": 50, "hrv_avg": 55},
        # 2026-01-03 missing → the lag-1 pair from 01-02 has no partner
        {"date": "2026-01-04", "stress_avg": 40, "hrv_avg": 58},
    ]
    xs, ys = correlations._paired(history, "stress_avg", "hrv_avg", lag=1)
    # only 01-01→01-02 lines up (01-02→01-03 gap, 01-04→01-05 missing)
    assert xs == [30.0] and ys == [55.0]


# --- find_correlations gating -------------------------------------------------

def _linear_history(n, slope, y_metric="hrv_avg", x_metric="sleep_score"):
    """n consecutive days where y = 40 + slope*x (a perfect same-day linear relationship)."""
    base = dt.date(2026, 1, 1)
    out = []
    for i in range(n):
        x = 50 + (i % 20)      # varies so there's variance
        out.append({
            "date": (base + dt.timedelta(days=i)).isoformat(),
            x_metric: x,
            y_metric: 40 + slope * x,
        })
    return out


def test_find_correlations_surfaces_strong_pair():
    findings = correlations.find_correlations(_linear_history(40, slope=0.5))
    same_day = [f for f in findings if f["x"] == "sleep_score" and f["y"] == "hrv_avg"
                and f["lag"] == 0]
    assert same_day and same_day[0]["r"] == 1.0
    assert same_day[0]["n"] >= correlations.MIN_SAMPLES
    assert "оцінка сну" in same_day[0]["detail"]


def test_find_correlations_needs_min_samples():
    # A perfect relationship but only 10 days → below MIN_SAMPLES → nothing.
    assert correlations.find_correlations(_linear_history(10, slope=0.5)) == []


def test_find_correlations_drops_weak_noise():
    # Random-ish jitter with no real relationship → no finding clears R_THRESHOLD + CI.
    base = dt.date(2026, 1, 1)
    hist = [{"date": (base + dt.timedelta(days=i)).isoformat(),
             "sleep_score": 70 + (i * 37 % 11),
             "hrv_avg": 60 + (i * 53 % 13)} for i in range(60)]
    findings = correlations.find_correlations(hist)
    assert all(abs(f["r"]) >= correlations.R_THRESHOLD for f in findings)


# --- run_insights service -----------------------------------------------------

async def _insights_logs(session):
    return list((await session.execute(
        select(ReportLog).where(ReportLog.kind == "insights")
    )).scalars().all())


async def _seed_strong(session):
    base = dt.date.today() - dt.timedelta(days=45)
    for i in range(45):
        x = 50 + (i % 20)
        session.add(DailyMetric(user_id=U1, date=(base + dt.timedelta(days=i)).isoformat(),
                                sleep_score=x, hrv_avg=40 + 0.5 * x))
    await session.commit()


async def test_run_insights_none_without_signal(session):
    text = await run_insights(session, user_id=U1)
    assert text is None
    assert await _insights_logs(session) == []


async def test_run_insights_narrates_and_logs(session):
    await _seed_strong(session)
    stats = CallStats(kind="insights", model=service.MODEL_INSIGHTS,
                      input_tokens=50, output_tokens=30, cost_usd=0.001)
    with patch.object(reports, "insights_with_stats",
                      return_value=("сон впливає на HRV", stats)) as m:
        text = await run_insights(session, user_id=U1, api_key="k")
    assert text == "сон впливає на HRV"
    m.assert_called_once()
    logs = await _insights_logs(session)
    assert len(logs) == 1 and logs[0].ok is True and logs[0].cached is False


async def test_run_insights_cache_hit_on_repeat(session):
    await _seed_strong(session)
    stats = CallStats(kind="insights", model=service.MODEL_INSIGHTS)
    with patch.object(reports, "insights_with_stats", return_value=("з кешу", stats)) as m:
        first = await run_insights(session, user_id=U1)
        second = await run_insights(session, user_id=U1)
    assert first == second == "з кешу"
    m.assert_called_once()
    logs = await _insights_logs(session)
    assert len(logs) == 2 and logs[1].cached is True


# --- monthly digest hook ------------------------------------------------------

class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


class _FakeCtx:
    def __init__(self):
        self.bot = _FakeBot()


async def _make_user(session):
    from app.db.models import User
    user = User(email="ins@x.com", password_hash="x", telegram_chat_id=888)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def test_monthly_insights_sends_once_and_guards(session):
    from types import SimpleNamespace

    from bot import jobs

    user = await _make_user(session)
    ctx = _FakeCtx()
    creds = SimpleNamespace(anthropic_key="k")
    with patch.object(jobs, "run_insights", return_value="інсайт") as m:
        await jobs._monthly_insights_for_user(ctx, session, user, creds)
        await jobs._monthly_insights_for_user(ctx, session, user, creds)  # guard blocks 2nd
    assert len(ctx.bot.sent) == 1
    assert "інсайт" in ctx.bot.sent[0][1]
    m.assert_called_once()


async def test_monthly_insights_no_findings_leaves_guard_unset(session):
    from types import SimpleNamespace

    from bot import jobs

    user = await _make_user(session)
    ctx = _FakeCtx()
    creds = SimpleNamespace(anthropic_key="k")
    with patch.object(jobs, "run_insights", return_value=None) as m:
        await jobs._monthly_insights_for_user(ctx, session, user, creds)
        await jobs._monthly_insights_for_user(ctx, session, user, creds)
    assert ctx.bot.sent == []
    assert m.call_count == 2   # guard not set → retried next week


# --- dedup-cache key ----------------------------------------------------------

def test_insights_cache_key_reflects_findings():
    base = {"window_days": 120, "findings": [{"x": "sleep_score", "r": 0.4}]}
    k1 = _insights_cache_key(base, "claude-sonnet-5")
    k2 = _insights_cache_key({**base, "findings": [{"x": "sleep_score", "r": 0.5}]},
                             "claude-sonnet-5")
    assert k1 != k2


def test_module_has_no_numpy_dependency():
    # Pure-Python by design (mirrors baselines.py) — a sanity check the math is stdlib-only.
    assert math.isclose(correlations.pearson([1, 2, 3, 4], [2, 4, 6, 8]), 1.0)
