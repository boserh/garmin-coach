"""NF-04 injury-risk radar: the pure-Python signal detector (ACWR / repeated pain / RPE
divergence / recovery drift), the calibration gate, the repository readers, and the
service assessment + advisory (Sonnet with a deterministic fallback)."""
import datetime as dt
from unittest.mock import patch

from sqlalchemy import select

from app import injury
from app.analysis import reports, service
from app.analysis.service import CallStats, build_injury_assessment, run_injury_check
from app.db.models import ActivityRecord, DailyMetric, ReportLog
from app.garmin import repository

U1 = 1


def _daily(n, *, acwr=None, hrv=None, hrv_base=None, rhr=None, start="2026-06-01"):
    """n oldest-first daily rows with optional per-day lists or constants."""
    d0 = dt.date.fromisoformat(start)
    rows = []
    for i in range(n):
        rows.append({
            "date": (d0 + dt.timedelta(days=i)).isoformat(),
            "acwr_pct": acwr[i] if isinstance(acwr, list) else acwr,
            "hrv_avg": hrv[i] if isinstance(hrv, list) else hrv,
            "hrv_baseline_low": hrv_base,
            "resting_hr": rhr[i] if isinstance(rhr, list) else rhr,
        })
    return rows


# --- calibration gate ---------------------------------------------------------

def test_calibrating_below_history_threshold():
    a = injury.assess([], [], history_days=5, min_history_days=14)
    assert a.level == "calibrating"
    assert a.actionable is False


def test_no_signals_is_level_none():
    daily = _daily(14, acwr=100, hrv=60, hrv_base=45)
    a = injury.assess(daily, [], history_days=60)
    assert a.level == "none"
    assert a.actionable is False


# --- ACWR signal --------------------------------------------------------------

def test_acwr_sustained_high_flags():
    daily = _daily(14, acwr=[100] * 11 + [145, 150, 148])  # 3 high readings
    a = injury.assess(daily, [], history_days=60)
    kinds = {s.kind for s in a.signals}
    assert "acwr" in kinds


def test_acwr_single_spike_not_enough():
    daily = _daily(14, acwr=[100] * 13 + [150])  # only 1 high reading
    a = injury.assess(daily, [], history_days=60)
    assert "acwr" not in {s.kind for s in a.signals}


# --- repeated pain (heaviest) -------------------------------------------------

def test_repeated_pain_flags_high():
    runs = [
        {"date": "2026-06-10", "pace": 6.0, "rpe": 5, "pain": True, "note": "коліно"},
        {"date": "2026-06-13", "pace": 6.0, "rpe": 5, "pain": True, "note": "коліно"},
    ]
    daily = _daily(14, acwr=100, hrv=60, hrv_base=45)
    a = injury.assess(daily, runs, history_days=60)
    pain = next(s for s in a.signals if s.kind == "pain")
    assert pain.severity >= 3
    assert a.level in ("elevated", "high")


def test_single_pain_not_repeated():
    runs = [{"date": "2026-06-10", "pace": 6.0, "rpe": 5, "pain": True, "note": "коліно"}]
    a = injury.assess(_daily(14, acwr=100), runs, history_days=60)
    assert "pain" not in {s.kind for s in a.signals}


# --- RPE / pace divergence ----------------------------------------------------

def test_rpe_rising_at_stable_pace_flags():
    runs = [
        {"date": "2026-06-02", "pace": 6.0, "rpe": 4, "pain": False, "note": None},
        {"date": "2026-06-05", "pace": 6.0, "rpe": 4, "pain": False, "note": None},
        {"date": "2026-06-09", "pace": 6.0, "rpe": 7, "pain": False, "note": None},
        {"date": "2026-06-12", "pace": 6.05, "rpe": 7, "pain": False, "note": None},
    ]
    a = injury.assess(_daily(14, acwr=100), runs, history_days=60)
    assert "rpe" in {s.kind for s in a.signals}


def test_rpe_rise_explained_by_slower_pace_not_flagged():
    # RPE up but pace much slower → not a divergence (higher effort explained).
    runs = [
        {"date": "2026-06-02", "pace": 6.0, "rpe": 4, "pain": False, "note": None},
        {"date": "2026-06-05", "pace": 6.0, "rpe": 4, "pain": False, "note": None},
        {"date": "2026-06-09", "pace": 5.0, "rpe": 7, "pain": False, "note": None},
        {"date": "2026-06-12", "pace": 5.0, "rpe": 7, "pain": False, "note": None},
    ]
    a = injury.assess(_daily(14, acwr=100), runs, history_days=60)
    assert "rpe" not in {s.kind for s in a.signals}


# --- recovery drift -----------------------------------------------------------

def test_hrv_below_baseline_flags_recovery():
    daily = _daily(14, acwr=100, hrv=[40, 40, 40] + [60] * 11, hrv_base=45)
    a = injury.assess(daily, [], history_days=60)
    assert "recovery" in {s.kind for s in a.signals}


# --- aggregate level ----------------------------------------------------------

def test_pain_plus_load_reaches_high():
    daily = _daily(14, acwr=[100] * 11 + [150, 150, 150], hrv=60, hrv_base=45)
    runs = [
        {"date": "2026-06-10", "pace": 6.0, "rpe": 5, "pain": True, "note": "гомілка"},
        {"date": "2026-06-13", "pace": 6.0, "rpe": 5, "pain": True, "note": "гомілка"},
    ]
    a = injury.assess(daily, runs, history_days=60)
    assert a.level == "high"
    # signals sorted by severity → pain (heaviest) first
    assert a.signals[0].kind == "pain"


def test_summary_and_context_shape():
    daily = _daily(14, acwr=[150] * 14)
    a = injury.assess(daily, [], history_days=60)
    txt = injury.summary(a)
    assert "ризик" in txt.lower()
    ctx = injury.to_context(a)
    assert ctx["level"] == a.level and len(ctx["signals"]) == len(a.signals)


# --- repository readers -------------------------------------------------------

async def test_readers_shape(session):
    session.add(DailyMetric(user_id=U1, date="2026-06-10", hrv_avg=42,
                            extra={"acwr_pct": 150, "hrv_baseline_low": 45, "resting_hr": 55}))
    a = ActivityRecord(user_id=U1, activity_id=1, date="2026-06-10", type="running",
                       dist_km=5.0, dur_min=30.0, subjective={"rpe": 6, "pain": True,
                                                               "note": "коліно"})
    session.add(a)
    await session.commit()

    daily = await repository.read_load_history(session, U1, days=400)
    assert daily and daily[0]["acwr_pct"] == 150 and daily[0]["hrv_baseline_low"] == 45
    runs = await repository.recent_subjective_runs(session, U1, days=400)
    assert runs and runs[0]["rpe"] == 6 and runs[0]["pain"] is True
    assert runs[0]["pace"] == 6.0
    assert await repository.count_daily_metrics(session, U1) == 1


# --- service: assessment + advisory ------------------------------------------

async def test_build_assessment_calibrating_new_user(session):
    session.add(DailyMetric(user_id=U1, date="2026-06-10", hrv_avg=42,
                            extra={"acwr_pct": 150}))
    await session.commit()
    a = await build_injury_assessment(session, user_id=U1)
    assert a.level == "calibrating"   # only 1 day of history


async def _injury_logs(session):
    return list((await session.execute(
        select(ReportLog).where(ReportLog.kind == "injury")
    )).scalars().all())


async def test_run_injury_check_narrates_and_logs(session):
    daily = _daily(14, acwr=[150] * 14)
    a = injury.assess(daily, [], history_days=60)
    stats = CallStats(kind="injury", model=service.MODEL_INJURY,
                      input_tokens=30, output_tokens=20, cost_usd=0.001)
    with patch.object(reports, "injury_with_stats", return_value=("бережи себе", stats)) as m:
        text = await run_injury_check(session, user_id=U1, assessment=a, api_key="k")
    assert text == "бережи себе"
    m.assert_called_once()
    logs = await _injury_logs(session)
    assert len(logs) == 1 and logs[0].ok is True


async def test_run_injury_check_falls_back_on_llm_error(session):
    daily = _daily(14, acwr=[150] * 14)
    a = injury.assess(daily, [], history_days=60)
    with patch.object(reports, "injury_with_stats",
                      side_effect=service.AnalystError("боом")):
        text = await run_injury_check(session, user_id=U1, assessment=a, api_key="k")
    # falls back to the deterministic pure-Python summary
    assert text == injury.summary(a)
    logs = await _injury_logs(session)
    assert len(logs) == 1 and logs[0].ok is False


# --- guard helper + job hook --------------------------------------------------

def test_within_guard():
    from bot import jobs
    assert jobs._within_guard("2026-07-10", "2026-07-11", days=5) is True
    assert jobs._within_guard("2026-07-01", "2026-07-11", days=5) is False
    assert jobs._within_guard(None, "2026-07-11", days=5) is False


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
    user = User(email="r@x.com", password_hash="x", telegram_chat_id=888)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def test_injury_hook_sends_once_then_guards(session):
    from types import SimpleNamespace

    from app import injury as injury_mod
    from bot import jobs

    user = await _make_user(session)
    ctx = _FakeCtx()
    creds = SimpleNamespace(anthropic_key="k")
    high = injury_mod.Assessment(level="high", score=6, history_days=60)
    with patch.object(jobs, "build_injury_assessment", return_value=high), \
         patch.object(jobs, "run_injury_check", return_value="увага, ризик") as m:
        await jobs._injury_check_for_user(ctx, session, user, creds, "2026-07-11")
        await jobs._injury_check_for_user(ctx, session, user, creds, "2026-07-12")  # guarded

    assert len(ctx.bot.sent) == 1
    m.assert_called_once()


async def test_injury_hook_silent_when_not_actionable(session):
    from types import SimpleNamespace

    from app import injury as injury_mod
    from bot import jobs

    user = await _make_user(session)
    ctx = _FakeCtx()
    creds = SimpleNamespace(anthropic_key="k")
    calm = injury_mod.Assessment(level="none", score=0, history_days=60)
    with patch.object(jobs, "build_injury_assessment", return_value=calm), \
         patch.object(jobs, "run_injury_check") as m:
        await jobs._injury_check_for_user(ctx, session, user, creds, "2026-07-11")

    assert ctx.bot.sent == []
    m.assert_not_called()
