"""Morning-job gating: only fire once today's recovery data (HRV + sleep) is in."""
from app.garmin.schemas import DailySummary, Payload
from bot.jobs import _recovery_synced

TODAY = "2026-06-24"


def _payload(today_row: DailySummary) -> Payload:
    return Payload(
        generated="2026-06-24T08:00:00", window_days=3, synced_today=True,
        last_data_date=TODAY, daily=[today_row], recent_activities=[], planned_runs=[],
    )


def test_recovery_synced_requires_hrv_and_sleep():
    full = _payload(DailySummary(date=TODAY, hrv_avg=60, sleep_score=80, has_data=True))
    assert _recovery_synced(full, TODAY) is True


def test_recovery_not_synced_with_stress_only():
    # Garmin synced stress early but not HRV/sleep — too loose to fire the morning report
    stress_only = _payload(DailySummary(date=TODAY, stress_avg=25, has_data=True))
    assert _recovery_synced(stress_only, TODAY) is False


def test_recovery_not_synced_with_hrv_but_no_sleep():
    partial = _payload(DailySummary(date=TODAY, hrv_avg=60, has_data=True))
    assert _recovery_synced(partial, TODAY) is False


def test_recovery_not_synced_when_no_today_row():
    other = _payload(DailySummary(date="2026-06-23", hrv_avg=60, sleep_score=80, has_data=True))
    assert _recovery_synced(other, TODAY) is False
