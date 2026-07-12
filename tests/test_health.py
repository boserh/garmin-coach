"""EP-08 proactive health alerts: the pure recovery-anomaly detector (app.health) on
synthetic series. The detector is a pure function (the AC: easy to test on synthetic rows);
thresholds are the user's own NF-01 percentile bands."""
import datetime as dt

from app import health

BASE = dt.date(2026, 6, 1)


def _row(i, **kw):
    """A daily row i days after BASE with sensible healthy defaults, overridable per metric."""
    d = (BASE + dt.timedelta(days=i)).isoformat()
    return {"date": d, "hrv_avg": 60.0, "resting_hr": 50.0, "sleep_score": 80.0,
            "sleep_h": 7.5, "stress_avg": 25.0, **kw}


def _healthy(n=30):
    return [_row(i) for i in range(n)]


# ---- cold-start / calibration ----------------------------------------------

def test_calibrating_under_min_history():
    r = health.detect(_healthy(5), min_history_days=7)
    assert r.level == "calibrating"
    assert not r.actionable
    assert r.history_days == 5


def test_calibrating_ignores_empty_rows():
    # Rows with no recovery scalar don't count toward the cold-start threshold.
    rows = [{"date": (BASE + dt.timedelta(days=i)).isoformat()} for i in range(20)]
    assert health.detect(rows, min_history_days=7).level == "calibrating"


# ---- healthy → no alerts ---------------------------------------------------

def test_stable_healthy_series_no_alerts():
    r = health.detect(_healthy(30))
    assert r.level == "none"
    assert r.alerts == []


# ---- individual rules ------------------------------------------------------

def test_hrv_low_fires_when_below_band_several_days():
    rows = _healthy(30)
    for i in (27, 28, 29):          # last 3 days HRV well below the personal band
        rows[i]["hrv_avg"] = 40.0
    r = health.detect(rows)
    kinds = [a.kind for a in r.alerts]
    assert r.actionable
    assert "hrv_low" in kinds


def test_hrv_low_not_fired_for_single_bad_day():
    rows = _healthy(30)
    rows[29]["hrv_avg"] = 40.0      # one dip is a blip, not sustained
    r = health.detect(rows)
    assert "hrv_low" not in [a.kind for a in r.alerts]


def test_rhr_up_fires_when_above_band_several_days():
    rows = _healthy(30)
    for i in (26, 27, 28, 29):
        rows[i]["resting_hr"] = 60.0
    r = health.detect(rows)
    assert "rhr_up" in [a.kind for a in r.alerts]


def test_sleep_debt_needs_four_of_seven():
    rows = _healthy(30)
    # only 3 short nights in the last week → below the SLEEP_DAYS=4 threshold
    for i in (27, 28, 29):
        rows[i]["sleep_score"] = 55.0
    assert "sleep_debt" not in [a.kind for a in health.detect(rows).alerts]
    # a fourth short night trips it
    rows[26]["sleep_score"] = 55.0
    assert "sleep_debt" in [a.kind for a in health.detect(rows).alerts]


def test_stress_high_fires_when_above_band_several_days():
    rows = _healthy(30)
    for i in (27, 28, 29):
        rows[i]["stress_avg"] = 45.0
    r = health.detect(rows)
    assert "stress_high" in [a.kind for a in r.alerts]


def test_severity_bumps_when_very_sustained():
    rows = _healthy(30)
    for i in range(23, 30):         # 7 days below band → severe
        rows[i]["hrv_avg"] = 40.0
    alert = next(a for a in health.detect(rows).alerts if a.kind == "hrv_low")
    assert alert.severity == 2


def test_alerts_sorted_by_severity():
    rows = _healthy(30)
    for i in range(23, 30):         # severe HRV (7 days)
        rows[i]["hrv_avg"] = 40.0
    for i in (27, 28, 29):          # milder stress (3 days)
        rows[i]["stress_avg"] = 45.0
    sevs = [a.severity for a in health.detect(rows).alerts]
    assert sevs == sorted(sevs, reverse=True)


# ---- formatting ------------------------------------------------------------

def test_summary_lists_signals_and_is_non_medical():
    rows = _healthy(30)
    for i in (27, 28, 29):
        rows[i]["hrv_avg"] = 40.0
    text = health.summary(health.detect(rows))
    assert "HRV" in text
    assert "лікаря" in text          # the non-medical escalation line


def test_to_context_shape():
    rows = _healthy(30)
    for i in (27, 28, 29):
        rows[i]["hrv_avg"] = 40.0
    ctx = health.to_context(health.detect(rows))
    assert ctx["level"] == "alert"
    assert ctx["alerts"][0]["kind"] == "hrv_low"
    assert "detail" in ctx["alerts"][0]


# ---- service layer + morning hook (session-backed) -------------------------

from types import SimpleNamespace  # noqa: E402
from unittest.mock import patch  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.analysis import reports, service  # noqa: E402
from app.analysis.service import build_health_alerts, run_health_alert  # noqa: E402
from app.db.models import DailyMetric, ReportLog, User  # noqa: E402

U1 = 1


async def test_build_health_alerts_calibrating_new_user(session):
    session.add(DailyMetric(user_id=U1, date="2026-06-10", hrv_avg=42))
    await session.commit()
    r = await build_health_alerts(session, user_id=U1)
    assert r.level == "calibrating"   # only 1 day of history


def _report_with_alert():
    return health.HealthReport(
        level="alert", history_days=60,
        alerts=[health.Alert("hrv_low", 2, "HRV нижче норми", "відпочинь")])


async def _health_logs(session):
    return list((await session.execute(
        select(ReportLog).where(ReportLog.kind == "health")
    )).scalars().all())


async def test_run_health_alert_narrates_and_logs(session):
    r = _report_with_alert()
    stats = service.CallStats(kind="health", model=service.MODEL_HEALTH,
                              input_tokens=20, output_tokens=15, cost_usd=0.001)
    with patch.object(reports, "health_with_stats", return_value=("бережи себе", stats)) as m:
        text = await run_health_alert(session, user_id=U1, report=r, api_key="k")
    assert text == "бережи себе"
    m.assert_called_once()
    logs = await _health_logs(session)
    assert len(logs) == 1 and logs[0].ok is True


async def test_run_health_alert_falls_back_on_llm_error(session):
    r = _report_with_alert()
    with patch.object(reports, "health_with_stats",
                      side_effect=service.AnalystError("боом")):
        text = await run_health_alert(session, user_id=U1, report=r, api_key="k")
    assert text == health.summary(r)          # deterministic fallback
    logs = await _health_logs(session)
    assert len(logs) == 1 and logs[0].ok is False


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


class _FakeCtx:
    def __init__(self):
        self.bot = _FakeBot()


async def _make_user(session, **kw):
    user = User(email="h@x.com", password_hash="x", telegram_chat_id=777, **kw)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def test_health_hook_sends_once_then_per_kind_cooldown(session):
    from bot import jobs

    user = await _make_user(session)
    ctx = _FakeCtx()
    creds = SimpleNamespace(anthropic_key="k")
    with patch.object(jobs, "build_health_alerts", return_value=_report_with_alert()), \
         patch.object(jobs, "run_health_alert", return_value="сигнал відновлення") as m:
        sent1 = await jobs._health_check_for_user(ctx, session, user, creds, "2026-07-11")
        sent2 = await jobs._health_check_for_user(ctx, session, user, creds, "2026-07-12")

    assert sent1 is True and sent2 is False    # same kind on cooldown the next day
    assert len(ctx.bot.sent) == 1
    m.assert_called_once()


async def test_health_hook_silent_when_disabled(session):
    from bot import jobs

    user = await _make_user(session, alerts_enabled=False)
    ctx = _FakeCtx()
    creds = SimpleNamespace(anthropic_key="k")
    with patch.object(jobs, "build_health_alerts", return_value=_report_with_alert()):
        sent = await jobs._health_check_for_user(ctx, session, user, creds, "2026-07-11")
    assert sent is False
    assert ctx.bot.sent == []


async def test_health_hook_silent_when_not_actionable(session):
    from bot import jobs

    user = await _make_user(session)
    ctx = _FakeCtx()
    creds = SimpleNamespace(anthropic_key="k")
    calm = health.HealthReport(level="none", history_days=60)
    with patch.object(jobs, "build_health_alerts", return_value=calm):
        sent = await jobs._health_check_for_user(ctx, session, user, creds, "2026-07-11")
    assert sent is False
    assert ctx.bot.sent == []
