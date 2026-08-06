"""UI-08: the step-by-step verdict as a picture, and the old rows that predate it.

NF-14 knew about every working step of a structured session and everything it knew came
out as one string: ``🎯 7/8 у цілі``. Eight 400s with the last two blown is a different
session from an even shortfall on all eight — one says endurance ran out, the other says
the target pace was wrong — and the counter renders them identically.

The riskiest part is the stored-JSON change, so that's what most of this covers: the new
field is additive, and a row written before it must still render.
"""
import datetime as dt

import anyio
import pytest

from app.charts import shade_zones
from app.routers.me import _stepbar_block
from tests.web_helpers import _seed_user, _user_id

EMAIL = "stepvis@example.com"

# A stored match in the OLD shape — counters and misses, no per-step detail.
LEGACY_MATCH = {"steps_hit": 3, "steps_total": 4,
                "misses": [{"step": 4, "planned": [4.5, 4.7], "actual": 5.4}]}


def _add_activity(uid, step_match, activity_id, series=None):
    from app.db.base import async_session_maker
    from app.db.models import ActivityRecord

    async def go():
        async with async_session_maker() as s:
            row = ActivityRecord(
                user_id=uid, activity_id=activity_id,
                date=dt.date.today().isoformat(), type="running",
                dist_km=8.0, dur_min=42.0, avg_hr=155,
                series=series, step_match=step_match)
            s.add(row)
            await s.commit()
            return row.id

    return anyio.run(go)


@pytest.fixture
def user(client):
    _seed_user(email=EMAIL, password="pw", is_admin=False)
    client.post("/login", data={"email": EMAIL, "password": "pw"})
    return client, _user_id(EMAIL)


_next_id = iter(range(950000, 959999))


def test_an_old_stored_match_still_renders_and_does_not_crash(user):
    """No migration was written on purpose — a row from before UI-08 has no ``steps`` key
    and must keep showing exactly what it showed before: the badge."""
    client, uid = user
    row_id = _add_activity(uid, LEGACY_MATCH, next(_next_id))
    html = client.get(f"/me/activities/{row_id}").text
    assert "🎯 3/4 у цілі" in html
    assert 'class="stepbar"' not in html


def test_the_bar_agrees_with_the_counters(user):
    """Green rows == ``steps_hit``, red rows == the ``misses`` list. Those fields stay the
    source of truth; the picture may never contradict them."""
    from app import stepmatch

    client, uid = user
    steps = [{"kind": "repeat", "reps": 4,
              "steps": [{"kind": "run", "dist_m": 400, "pace_min_km": [4.5, 4.7]}]}]
    laps = [{"dist_m": 400.0, "pace_min_km": p} for p in (4.55, 4.6, 5.3, 5.5)]
    match = stepmatch.match(steps, laps)

    row_id = _add_activity(uid, match, next(_next_id))
    html = client.get(f"/me/activities/{row_id}").text
    assert html.count('class="sbrow hit"') == match["steps_hit"]
    assert html.count('class="sbrow miss"') == len(match["misses"])
    assert "інтервал 1/4" not in html          # the kind is "run" here
    assert "відрізок 4/4" in html


def test_a_step_never_run_says_so_instead_of_showing_a_time(user):
    """Stopped early: the module calls it an honest miss, and the UI must not invent an
    actual pace of 0:00 for a lap that doesn't exist."""
    from app import stepmatch

    client, uid = user
    steps = [{"kind": "repeat", "reps": 3,
              "steps": [{"kind": "run", "dist_m": 400, "pace_min_km": [4.5, 4.7]}]}]
    match = stepmatch.match(steps, [{"dist_m": 400.0, "pace_min_km": 4.6}])

    row_id = _add_activity(uid, match, next(_next_id))
    html = client.get(f"/me/activities/{row_id}").text
    assert html.count("не виконано") == 2
    assert "0.00" not in html


def test_a_free_run_gets_no_block_at_all(user):
    """``match`` returns None for an unstructured run — no bar, no empty card."""
    client, uid = user
    row_id = _add_activity(uid, None, next(_next_id))
    html = client.get(f"/me/activities/{row_id}").text
    assert 'class="stepbar"' not in html
    assert "🎯" not in html


def test_the_stepbar_block_is_none_without_per_step_detail():
    assert _stepbar_block(None) is None
    assert _stepbar_block(LEGACY_MATCH) is None
    assert _stepbar_block({"steps_hit": 0, "steps_total": 0, "steps": []}) is None
    assert _stepbar_block("not a dict") is None


def test_the_deviation_bars_are_scaled_within_one_session():
    """Bars are comparable inside a session: the widest miss sets the scale, a hit is
    flat, and a step that never happened has no bar to draw."""
    block = _stepbar_block({
        "steps_hit": 1, "steps_total": 4, "misses": [],
        "steps": [
            {"step": 1, "kind": "run", "planned": [4.5, 4.7], "actual": 4.6,
             "hit": True, "delta_s": 0},
            {"step": 2, "kind": "run", "planned": [4.5, 4.7], "actual": 4.9,
             "hit": False, "delta_s": 12},
            {"step": 3, "kind": "run", "planned": [4.5, 4.7], "actual": 5.1,
             "hit": False, "delta_s": 24},
            {"step": 4, "kind": "run", "planned": [4.5, 4.7], "actual": None,
             "hit": False, "delta_s": None},
        ],
    })
    assert [r["width_pct"] for r in block["rows"]] == [0, 50, 100, 0]
    assert [r["slower"] for r in block["rows"]] == [False, True, True, False]
    assert block["rows"][3]["missing"] is True


def test_the_pace_curve_shades_the_intervals(user):
    """The bands are placed by distance, because the curve's x axis is sampled by
    distance — a band placed by lap index would drift away from the line it marks."""
    from app import stepmatch

    client, uid = user
    series = [{"d": i * 100, "p": 4.6, "hr": 160} for i in range(41)]   # 4 km
    steps = [{"kind": "warmup", "dist_m": 1000},
             {"kind": "repeat", "reps": 2,
              "steps": [{"kind": "run", "dist_m": 400, "pace_min_km": [4.5, 4.7]},
                        {"kind": "recovery", "dist_m": 200}]}]
    laps = [{"dist_m": 1000.0, "pace_min_km": 6.0},
            {"dist_m": 400.0, "pace_min_km": 4.6},
            {"dist_m": 200.0, "pace_min_km": 7.0},
            {"dist_m": 400.0, "pace_min_km": 5.4},
            {"dist_m": 200.0, "pace_min_km": 7.0}]
    match = stepmatch.match(steps, laps)

    row_id = _add_activity(uid, match, next(_next_id), series=series)
    html = client.get(f"/me/activities/{row_id}").text
    # one band per scored step, coloured by the verdict
    assert html.count('fill="var(--easy)" opacity=".13"') == 1
    assert html.count('fill="var(--intervals)" opacity=".13"') == 1


def test_shade_zones_places_bands_by_distance_and_drops_impossible_ones():
    series = [{"d": i * 100} for i in range(41)]      # 0..4000 m
    zones = shade_zones(series, [
        {"from_m": 0, "to_m": 2000, "hit": True},     # first half
        {"from_m": 2000, "to_m": 4000, "hit": False},
        {"from_m": 9000, "to_m": 9500, "hit": True},  # past the end of the run
        {"from_m": 500, "to_m": 500, "hit": True},    # zero width
    ])
    assert [z["hit"] for z in zones] == [True, False]
    assert zones[0]["x"] < zones[1]["x"]
    assert zones[0]["w"] == pytest.approx(zones[1]["w"], abs=1.0)
    # Nothing to place a band against is an empty list, not a crash.
    assert shade_zones([], [{"from_m": 0, "to_m": 10, "hit": True}]) == []
    assert shade_zones(series, []) == []


def test_the_plan_shows_the_aggregate_hit_rate(user, monkeypatch):
    """``stepmatch.aggregate`` was written for the adaptation context and shown nowhere.
    One bad session and a plan that's systematically too fast look identical without it.

    The four matched structured sessions are stubbed at the repository boundary —
    building them through the DB would exercise the matcher, which has its own tests."""
    from app.db.base import async_session_maker
    from app.garmin import repository
    from app.garmin.schemas import PlanWorkout

    client, uid = user
    today = dt.date.today()

    async def make_plan():
        async with async_session_maker() as s:
            if await repository.get_active_plan(s, uid):
                return
            await repository.create_plan(
                s, uid, goal="general", goal_label="Загальна форма", target_date=None,
                start_date=today.isoformat(), days_per_week=3, intensity="easy",
                intake={}, summary="Блок.",
                workouts=[PlanWorkout(date=today.isoformat(), week=1, type="intervals",
                                      dist_km=8.0, description="8×400")])

    anyio.run(make_plan)

    async def fake(_session, _plan_id, **kw):
        return [{"date": "2026-08-01", "steps_hit": 7, "steps_total": 9}] * 4

    monkeypatch.setattr(repository, "recent_step_match", fake)
    html = client.get("/plan").text
    assert "структурні сесії" in html
    assert "78%" in html          # 28/36 hit
    assert "28/36" in html


def test_another_users_activity_is_not_readable(user):
    client, _uid = user
    _seed_user(email="other-stepvis@example.com", password="pw", is_admin=False)
    theirs = _add_activity(_user_id("other-stepvis@example.com"), LEGACY_MATCH,
                           next(_next_id))
    assert client.get(f"/me/activities/{theirs}").status_code == 404
