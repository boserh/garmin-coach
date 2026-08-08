"""The single-day recovery page: /me/daily_metrics/{id}.

Guards the two things the redesign is about — every value the watch sent reaches the
page as a Ukrainian label (nothing falls through to a raw-key dump), and the personal
band only appears once there is real history behind it.
"""

import anyio

from app.db.base import async_session_maker
from tests.web_helpers import _seed_user, _user_id

EMAIL = "day@example.com"


def _extra(day: int) -> dict:
    return {
        "resting_hr": 65 + (day % 3),
        "readiness_score": 70,
        "steps": 6000 + day * 10,
        "distance_m": 4980,
        "active_kcal": 310,
        "vigorous_min": 4,
        "floors_up": 6,
        "min_hr": 60,
        "bb_low": 13, "bb_high": 62, "bb_change": 47,
        "avg_sleep_stress": 24.0,
        "sleep_need_h": 9.0, "sleep_need_feedback": "HIGHLY_INCREASED",
        "sleep_start": "02:10", "sleep_end": "09:42",
        "spo2_avg": 94.0, "spo2_low": 82,
        "respiration_avg": 20.0,
        "overnight_hrv": 31.0, "avg_hr_sleep": 68.0,
        "awake_count": 1, "restless_moments": 64,
        "hrv_baseline_low": 31, "hrv_baseline_high": 37,
        "hrv_weekly_avg": 33, "hrv_5min_high": 45,
        "recovery_time_h": 12, "acute_load": 210, "acwr_pct": 88,
        "race_5k_s": 1821, "vo2max": 41,
    }


def _seed_days(n: int = 20):
    """One user with ``n`` consecutive days ending 2026-08-08."""
    from app.garmin import repository
    from app.garmin.schemas import DailySummary

    _seed_user(email=EMAIL, password="pw", is_admin=False)
    uid = _user_id(EMAIL)

    async def seed():
        import datetime as dt
        last = dt.date(2026, 8, 8)
        async with async_session_maker() as s:
            for i in range(n):
                d = last - dt.timedelta(days=n - 1 - i)
                await repository.upsert_daily(s, uid, DailySummary(
                    date=d.isoformat(), sleep_score=66 + (i % 5), sleep_h=7.4,
                    deep_h=0.3, rem_h=1.03, light_h=6.05, awake_h=0.15,
                    hrv_avg=31 + (i % 4), hrv_status="BALANCED",
                    stress_avg=22, stress_max=68, bb_charged=49, bb_drained=1,
                    extra=_extra(i), has_data=True))
            await s.commit()

    anyio.run(seed)
    return uid


def _day_id(uid, date="2026-08-08"):
    from sqlalchemy import select

    from app.db.models import DailyMetric

    async def get():
        async with async_session_maker() as s:
            return (await s.execute(
                select(DailyMetric.id).where(DailyMetric.user_id == uid,
                                             DailyMetric.date == date)
            )).scalar_one()

    return anyio.run(get)


def _open(client, uid=None):
    uid = uid or _seed_days()
    client.post("/login", data={"email": EMAIL, "password": "pw"})
    r = client.get(f"/me/daily_metrics/{_day_id(uid)}")
    assert r.status_code == 200
    return r.text


def test_day_page_shows_every_extra_field_with_a_ukrainian_label(client):
    html = _open(client)
    # the four keys that used to fall through to an "Інше" dump of raw English names
    for label in ("активні ккал", "інтенсивна, хв", "поверхів угору", "SpO₂, мін. %"):
        assert label in html
    # Garmin's shouted enum is translated, never printed raw
    assert "HIGHLY_INCREASED" not in html
    assert "сильно підвищена" in html
    assert "sleep need feedback" not in html


def test_day_page_draws_the_personal_band_and_the_garmin_hrv_band(client):
    html = _open(client)
    assert "dv-band" in html
    assert "у звичному діапазоні" in html or "вище звичного" in html
    # HRV rides on Garmin's own balanced corridor, with the weekly average as a mark
    assert "тижд." in html


def test_day_page_renders_sleep_stages_as_one_bar(client):
    html = _open(client)
    for stage in ("dv-seg--deep", "dv-seg--rem", "dv-seg--light", "dv-seg--awake"):
        assert stage in html
    assert "02:10" in html and "09:42" in html          # bedtime → wake time


def test_day_page_invents_no_normal_range_without_history(client):
    """A first day has nothing to compare against: the numbers still render, but the
    only band is HRV's — that one comes from Garmin, not from our own sample."""
    _seed_user(email="dayfresh@example.com", password="pw", is_admin=False)
    uid = _user_id("dayfresh@example.com")

    async def seed():
        from app.garmin import repository
        from app.garmin.schemas import DailySummary
        async with async_session_maker() as s:
            await repository.upsert_daily(s, uid, DailySummary(
                date="2026-03-02", sleep_score=66, sleep_h=7.4, hrv_avg=31,
                stress_avg=22, extra=_extra(0), has_data=True))
            await s.commit()

    anyio.run(seed)
    client.post("/login", data={"email": "dayfresh@example.com", "password": "pw"})
    html = client.get(f"/me/daily_metrics/{_day_id(uid, '2026-03-02')}").text
    assert "Історії ще немає" in html
    assert html.count("dv-core") == 1          # HRV's Garmin band, nothing else
    assert "66" in html                        # the sleep score itself is still shown


def test_day_page_is_scoped_to_its_owner(client):
    uid = _seed_days()
    day_id = _day_id(uid)
    _seed_user(email="other@example.com", password="pw", is_admin=False)
    client.post("/login", data={"email": "other@example.com", "password": "pw"})
    assert client.get(f"/me/daily_metrics/{day_id}").status_code == 404
