"""Travel-aware weather location: the forecast follows where the athlete last trained.

The bug this closes is a quiet wrong answer, not a crash. ``weather_location`` is a HOME
address typed once at setup; during a training camp or a trip the morning report kept
advising about that city's heat and rain while the athlete was 600 km away in the Alps —
confidently, with numbers, and wrong. The evidence of where they actually are was already
in the database: the start point of the last activity.

The load-bearing tests here are the ones that keep the rule honest in both directions —
a nearby out-of-town run must NOT move the forecast, a week-old one must not either — and
the one that keeps coordinates out of the LLM context (the NF-33 rule, extended to the
place block that rides into the prompt and into ``report_logs``).
"""
import datetime as dt
import json

import pytest

from app import weather
from app.db.models import ActivityRecord, User
from app.garmin import repository

TODAY = dt.date(2026, 8, 21)
GDANSK = (54.372, 18.638, "Gdańsk, Польща")
ALPS = (47.259, 11.400)        # Innsbruck-ish — ~940 km away
SOPOT = (54.441, 18.560)       # ~9 km from home: same weather

# The DB half resolves "how old is this evidence" against the real clock (user_today), so
# its fixtures are dated relative to today rather than pinned — a test that only passes on
# the day it was written is a test that stops testing.
YDAY = (dt.date.today() - dt.timedelta(days=1)).isoformat()
LAST_WEEK = (dt.date.today() - dt.timedelta(days=7)).isoformat()


def _pick(home, recent, *, max_age_days=2, min_away_km=75.0):
    return weather.pick_location(home, recent, today=TODAY, max_age_days=max_age_days,
                                 min_away_km=min_away_km)


# ---------- the pure rule ----------

def test_far_and_fresh_activity_wins_over_the_profile_city():
    lat, lon, place = _pick(GDANSK, (ALPS[0], ALPS[1], "2026-08-20"))
    assert (lat, lon) == ALPS
    assert place["source"] == "activity"
    assert place["since"] == "2026-08-20"
    assert place["home"] == "Gdańsk, Польща"
    assert 900 <= place["away_km"] <= 980


def test_a_run_out_of_town_does_not_move_the_forecast():
    """Below the threshold it is the same weather — and flipping the location on GPS drift
    would make the report contradict itself day to day."""
    lat, lon, place = _pick(GDANSK, (SOPOT[0], SOPOT[1], "2026-08-20"))
    assert (lat, lon) == GDANSK[:2]
    assert place == {"source": "profile", "name": "Gdańsk, Польща"}


def test_a_stale_activity_loses_to_the_profile():
    """Six days ago says nothing about today: the flight home leaves no trace in Garmin."""
    _lat, _lon, place = _pick(GDANSK, (ALPS[0], ALPS[1], "2026-08-15"))
    assert place["source"] == "profile"


def test_a_future_dated_activity_is_ignored():
    """A watch with a wrong date must not pin the forecast to a place forever."""
    _lat, _lon, place = _pick(GDANSK, (ALPS[0], ALPS[1], "2026-09-01"))
    assert place["source"] == "profile"


def test_no_profile_location_falls_back_to_where_they_trained():
    lat, lon, place = _pick(None, (ALPS[0], ALPS[1], "2026-08-21"))
    assert (lat, lon) == ALPS
    assert place == {"source": "activity", "since": "2026-08-21"}


def test_nothing_known_at_all_is_none():
    assert _pick(None, None) is None
    assert _pick(None, (ALPS[0], ALPS[1], "2026-08-01")) is None   # stale, no home


def test_place_never_carries_coordinates():
    """``place`` rides into the prompt and into report_logs. NF-33's rule (a home address
    is the real risk of storing location) applies to it too: the SOURCE of the choice
    travels, the point itself never does."""
    for home, recent in ((GDANSK, (ALPS[0], ALPS[1], "2026-08-20")), (GDANSK, None)):
        place = _pick(home, recent)[2]
        blob = json.dumps(place, ensure_ascii=False)
        for coord in (*ALPS, GDANSK[0], GDANSK[1]):
            assert str(coord) not in blob
        assert not {"lat", "lon", "latitude", "longitude"} & set(place)


# ---------- the DB half ----------

async def _user(session, **kw) -> User:
    u = User(email="wx@example.com", password_hash="x",
             latitude=GDANSK[0], longitude=GDANSK[1], weather_location=GDANSK[2], **kw)
    session.add(u)
    await session.commit()
    return u


async def _activity(session, user_id, date, *, aid=1, type="running", **kw) -> ActivityRecord:
    act = ActivityRecord(user_id=user_id, activity_id=aid, date=date, type=type, **kw)
    session.add(act)
    await session.commit()
    return act


@pytest.mark.asyncio
async def test_location_for_user_follows_the_last_activity(session):
    u = await _user(session)
    await _activity(session, u.id, YDAY, start_lat=ALPS[0], start_lon=ALPS[1])
    lat, lon, place = await weather.location_for_user(session, u)
    assert (lat, lon) == ALPS
    assert place["source"] == "activity"


@pytest.mark.asyncio
async def test_location_for_user_reads_old_rows_from_their_series(session):
    """Rows synced before the start_lat/start_lon columns existed still know where they
    happened — the trip you are ALREADY on works without a backfill run."""
    u = await _user(session)
    await _activity(session, u.id, YDAY,
                    series=[{"d": 0.0, "lat": ALPS[0], "lon": ALPS[1]},
                            {"d": 1.0, "lat": ALPS[0], "lon": ALPS[1]}])
    lat, lon, place = await weather.location_for_user(session, u)
    assert (lat, lon) == ALPS
    assert place["source"] == "activity"


@pytest.mark.asyncio
async def test_an_indoor_session_does_not_hide_the_hike_before_it(session):
    """A gym evening has no coordinates; the mountain hike from the morning still counts."""
    u = await _user(session)
    await _activity(session, u.id, YDAY, aid=1,
                    start_lat=ALPS[0], start_lon=ALPS[1])
    await _activity(session, u.id, dt.date.today().isoformat(), aid=2,
                    type="strength_training")
    lat, lon, _place = await weather.location_for_user(session, u)
    assert (lat, lon) == ALPS


@pytest.mark.asyncio
async def test_hidden_activities_never_move_the_forecast(session):
    """ST-17: a hidden row is excluded from every aggregate — including this one."""
    u = await _user(session)
    await _activity(session, u.id, YDAY,
                    start_lat=ALPS[0], start_lon=ALPS[1], is_hidden=True)
    lat, lon, place = await weather.location_for_user(session, u)
    assert (lat, lon) == GDANSK[:2]
    assert place["source"] == "profile"


@pytest.mark.asyncio
async def test_a_trip_that_ended_a_week_ago_is_not_read_at_all(session):
    """The recency rule is applied in SQL as well as in the pure picker — a stale row is
    never even a candidate, so no amount of it can drag the forecast abroad."""
    u = await _user(session)
    await _activity(session, u.id, LAST_WEEK, start_lat=ALPS[0], start_lon=ALPS[1])
    assert await repository.last_activity_location(
        session, u.id, since_date=YDAY) is None
    lat, lon, place = await weather.location_for_user(session, u)
    assert (lat, lon) == GDANSK[:2]
    assert place["source"] == "profile"


@pytest.mark.asyncio
async def test_auto_location_off_restores_the_profile_only_behaviour(session, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "WEATHER_AUTO_LOCATION", False)
    u = await _user(session)
    await _activity(session, u.id, YDAY, start_lat=ALPS[0], start_lon=ALPS[1])
    lat, lon, place = await weather.location_for_user(session, u)
    assert (lat, lon) == GDANSK[:2]
    assert place["source"] == "profile"


# ---------- the forecast wrapper ----------

@pytest.mark.asyncio
async def test_forecast_for_user_fetches_the_travel_location_and_labels_it(
        session, monkeypatch):
    u = await _user(session)
    await _activity(session, u.id, YDAY, start_lat=ALPS[0], start_lon=ALPS[1])
    called = {}

    def fake_fetch(lat, lon):
        called["coords"] = (lat, lon)
        return {"date": dt.date.today().isoformat(), "t_min_c": 8, "t_max_c": 19,
                "summary": "ясно", "tz": "Europe/Vienna", "elev_m": 1620}

    monkeypatch.setattr(weather, "fetch_forecast", fake_fetch)
    wx = await weather.forecast_for_user(session, u)
    assert called["coords"] == ALPS
    # tz/elevation describe the PLACE and are moved into that block — one home per fact,
    # so the prompt (and the dedup-cache key) never carries them twice.
    assert wx["place"] == {"source": "activity", "since": YDAY, "away_km":
                           wx["place"]["away_km"], "home": "Gdańsk, Польща",
                           "tz": "Europe/Vienna", "elev_m": 1620}
    assert "tz" not in wx and "elev_m" not in wx


@pytest.mark.asyncio
async def test_forecast_for_user_without_any_location_stays_none(session, monkeypatch):
    u = User(email="nowhere@example.com", password_hash="x")
    session.add(u)
    await session.commit()
    monkeypatch.setattr(weather, "fetch_forecast",
                        lambda *a: pytest.fail("no location → no fetch"))
    assert await weather.forecast_for_user(session, u) is None


# ---------- persistence ----------

@pytest.mark.asyncio
async def test_upsert_keeps_coordinates_a_later_row_does_not_carry(session):
    """The single-activity detail path and the offline import build rows without
    coordinates; re-syncing through one of those must not put the weather back on the
    profile city."""
    u = await _user(session)
    await repository.upsert_activity(session, u.id, 42, {
        "date": YDAY, "type": "running",
        "start_lat": ALPS[0], "start_lon": ALPS[1]})
    await session.commit()
    await repository.upsert_activity(session, u.id, 42, {
        "date": YDAY, "type": "running", "dist_km": 12.0})
    await session.commit()
    act = await repository.get_activity_by_garmin_id(session, u.id, 42)
    assert (act.start_lat, act.start_lon) == ALPS
    assert act.dist_km == 12.0


def test_sync_coarsens_the_start_point_it_stores():
    """Three decimals ≈ 110 m: a neighbourhood, not a doorstep (app/routes.py's rule)."""
    from app.garmin import service

    assert service._start_coords(
        {"startLatitude": 47.2586921, "startLongitude": 11.4003792}
    ) == {"start_lat": 47.259, "start_lon": 11.4}
    assert service._start_coords({}) == {}
    assert service._start_coords({"startLatitude": None, "startLongitude": 11.4}) == {}
