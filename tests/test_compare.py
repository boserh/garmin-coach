"""NF-06 compare-past-self: the pure date/window helpers, the repository window aggregates,
and the run_compare service call (None when there isn't history in both windows; cache hit on
repeat; ReportLog written)."""
import datetime as dt
from unittest.mock import patch

from sqlalchemy import select

from app import compare
from app.analysis import reports, service
from app.analysis.service import CallStats, _compare_cache_key, run_compare
from app.db.models import ActivityRecord, DailyMetric, ReportLog
from app.garmin import repository

U1 = 1


# --- period parsing -----------------------------------------------------------

def test_parse_period_default():
    assert compare.parse_period(None) == compare.DEFAULT_WEEKS
    assert compare.parse_period([]) == compare.DEFAULT_WEEKS
    assert compare.parse_period(["banana"]) == compare.DEFAULT_WEEKS


def test_parse_period_int_and_suffix():
    assert compare.parse_period(["8"]) == 8
    assert compare.parse_period(["8w"]) == 8
    assert compare.parse_period(["12тиж"]) == 12


def test_parse_period_clamped():
    assert compare.parse_period(["0"]) == compare.MIN_WEEKS
    assert compare.parse_period(["999"]) == compare.MAX_WEEKS


# --- window pairing -----------------------------------------------------------

def test_window_pair_current_and_past():
    today = dt.date(2026, 7, 11)
    cur_s, cur_e, past_s, past_e = compare.window_pair(today, weeks=4)
    assert cur_e == "2026-07-11"
    assert cur_s == "2026-06-14"     # inclusive 4-week window (28 days)
    assert past_e == "2025-07-11"
    assert past_s == "2025-06-14"


def test_window_pair_leap_day_safe():
    today = dt.date(2024, 2, 29)
    _, cur_e, _, past_e = compare.window_pair(today, weeks=1)
    assert cur_e == "2024-02-29"
    assert past_e == "2023-02-28"    # no Feb 29 in 2023 → clamped


# --- has_signal / fmt ---------------------------------------------------------

def test_has_signal_needs_both_windows():
    cur = {"runs": 5, "vo2max": None, "avg_hrv": None}
    past_empty = {"runs": 0, "vo2max": None, "avg_hrv": None}
    assert compare.has_signal(cur, past_empty) is False
    past_ok = {"runs": 0, "vo2max": 45, "avg_hrv": None}
    assert compare.has_signal(cur, past_ok) is True


def test_fmt_range_ukrainian_months():
    assert compare.fmt_range("2025-06-14", "2025-07-11") == "14 червня – 11 липня 2025"


# --- repository.window_stats --------------------------------------------------

async def test_window_stats_aggregates_runs_and_metrics(session):
    session.add_all([
        ActivityRecord(user_id=U1, activity_id=1, date="2026-06-20",
                       type="running", dist_km=5.0, dur_min=30.0, avg_hr=150),   # 6:00/km
        ActivityRecord(user_id=U1, activity_id=2, date="2026-06-25",
                       type="running", dist_km=10.0, dur_min=55.0, avg_hr=155),  # 5:30/km
        ActivityRecord(user_id=U1, activity_id=3, date="2026-06-22",
                       type="cycling", dist_km=30.0, dur_min=60.0, avg_hr=130),  # not a run
    ])
    session.add(DailyMetric(user_id=U1, date="2026-06-21", hrv_avg=60, sleep_score=80,
                            extra={"vo2max": 47, "resting_hr": 52, "race_5k_s": 1600}))
    await session.commit()

    s = await repository.window_stats(session, U1, "2026-06-14", "2026-07-11")
    assert s["runs"] == 2                 # cycling excluded
    assert s["run_km"] == 15.0
    assert s["longest_km"] == 10.0
    assert s["typical_pace"] == 5.75      # median of 6.0 and 5.5
    assert s["vo2max"] == 47
    assert s["avg_resting_hr"] == 52
    assert s["race"] == {"race_5k_s": 1600}


async def test_window_stats_empty_window(session):
    s = await repository.window_stats(session, U1, "2020-01-01", "2020-01-31")
    assert s["runs"] == 0 and s["run_km"] == 0.0
    assert s["typical_pace"] is None and s["vo2max"] is None and s["race"] is None


# --- run_compare service ------------------------------------------------------

async def _compare_logs(session):
    return list((await session.execute(
        select(ReportLog).where(ReportLog.kind == "compare")
    )).scalars().all())


async def test_run_compare_none_without_history(session):
    # No data at all → not enough signal in either window → None, no Claude, no log.
    text = await run_compare(session, user_id=U1, weeks=4)
    assert text is None
    assert await _compare_logs(session) == []


async def test_run_compare_narrates_and_logs(session):
    today = dt.date.today()
    cur_s, cur_e, past_s, past_e = compare.window_pair(today, weeks=4)
    # A run in the current window and one a year ago → signal in both.
    session.add_all([
        ActivityRecord(user_id=U1, activity_id=10, date=cur_e,
                       type="running", dist_km=5.0, dur_min=28.0, avg_hr=150),
        ActivityRecord(user_id=U1, activity_id=11, date=past_e,
                       type="running", dist_km=5.0, dur_min=32.0, avg_hr=150),
    ])
    await session.commit()

    stats = CallStats(kind="compare", model=service.MODEL_COMPARE,
                      input_tokens=40, output_tokens=15, cost_usd=0.001)
    with patch.object(reports, "compare_with_stats",
                      return_value=("ти зараз швидший", stats)) as m:
        text = await run_compare(session, user_id=U1, weeks=4, api_key="k")

    assert text == "ти зараз швидший"
    m.assert_called_once()
    logs = await _compare_logs(session)
    assert len(logs) == 1 and logs[0].ok is True and logs[0].cached is False


async def test_run_compare_cache_hit_on_repeat(session):
    today = dt.date.today()
    _, cur_e, _, past_e = compare.window_pair(today, weeks=4)
    session.add_all([
        ActivityRecord(user_id=U1, activity_id=20, date=cur_e,
                       type="running", dist_km=6.0, dur_min=33.0, avg_hr=150),
        ActivityRecord(user_id=U1, activity_id=21, date=past_e,
                       type="running", dist_km=6.0, dur_min=36.0, avg_hr=150),
    ])
    await session.commit()

    stats = CallStats(kind="compare", model=service.MODEL_COMPARE)
    with patch.object(reports, "compare_with_stats", return_value=("з кешу", stats)) as m:
        first = await run_compare(session, user_id=U1, weeks=4)
        second = await run_compare(session, user_id=U1, weeks=4)

    assert first == second == "з кешу"
    m.assert_called_once()
    logs = await _compare_logs(session)
    assert len(logs) == 2 and logs[1].cached is True


# --- monthly digest job hook --------------------------------------------------

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
    user = User(email="c@x.com", password_hash="x", telegram_chat_id=777)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def test_monthly_compare_sends_once_and_guards(session):
    from types import SimpleNamespace

    from bot import jobs

    user = await _make_user(session)
    ctx = _FakeCtx()
    creds = SimpleNamespace(anthropic_key="k")
    with patch.object(jobs, "run_compare", return_value="порівняння") as m:
        await jobs._monthly_compare_for_user(ctx, session, user, creds)
        await jobs._monthly_compare_for_user(ctx, session, user, creds)  # guard blocks 2nd

    assert len(ctx.bot.sent) == 1
    assert "порівняння" in ctx.bot.sent[0][1]
    m.assert_called_once()   # second call short-circuits on the month guard


async def test_monthly_compare_no_history_leaves_guard_unset(session):
    from types import SimpleNamespace

    from bot import jobs

    user = await _make_user(session)
    ctx = _FakeCtx()
    creds = SimpleNamespace(anthropic_key="k")
    with patch.object(jobs, "run_compare", return_value=None) as m:
        await jobs._monthly_compare_for_user(ctx, session, user, creds)
        await jobs._monthly_compare_for_user(ctx, session, user, creds)

    assert ctx.bot.sent == []          # nothing sent
    assert m.call_count == 2           # guard not set → retried (fires again next week)


# --- dedup-cache key ----------------------------------------------------------

def test_compare_cache_key_reflects_windows():
    base = {"weeks": 4, "years_back": 1,
            "current": {"run_km": 20}, "past": {"run_km": 15}}
    k1 = _compare_cache_key(base, "claude-sonnet-5")
    k2 = _compare_cache_key({**base, "current": {"run_km": 30}}, "claude-sonnet-5")
    assert k1 != k2
