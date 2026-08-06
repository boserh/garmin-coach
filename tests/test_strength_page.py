"""UI-06: /strength charts what NF-27 already computes, and computes nothing itself.

``app.strengthstats`` produced session tonnage, Epley e1RM, weekly stats, trends and
stalls from the day it shipped, and none of it had a web surface — running had charts,
records and period comparisons while strength was a flat list of exercise names.

What's worth guarding here isn't the markup, it's the honesty of the numbers: an
estimate labelled as an estimate, a single measurement not drawn as a trend, hidden
activities excluded, and nothing recomputed outside the module.
"""
import datetime as dt
import re
from pathlib import Path

import anyio
import pytest

from tests.web_helpers import _seed_user, _user_id

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "app" / "templates" / "strength.html"
ROUTER = ROOT / "app" / "routers" / "strength.py"

EMAIL = "strength-page@example.com"


@pytest.fixture
def no_llm_no_garmin(monkeypatch):
    import app.analysis.client as client_mod
    import app.garmin.providers as providers

    def boom(*a, **kw):
        raise AssertionError("the strength page must not call out to Claude/Garmin")

    monkeypatch.setattr(client_mod, "_get_client", boom, raising=False)
    monkeypatch.setattr(providers, "get_provider", boom, raising=False)


def _sets(name, reps, weights):
    return {"sets": {name: {"count": len(reps), "reps": reps, "weight_kg": weights}}}


# The DB outlives a single test and (user_id, activity_id) is unique, so ids never repeat.
_next_id = iter(range(800000, 809999))


def _seed_strength(uid, sessions):
    """``sessions`` is ``[(days_ago, exercises, is_hidden)]``. Idempotent per user: a
    second call for an account that already has strength rows is a no-op."""
    from app.db.base import async_session_maker
    from app.db.models import ActivityRecord
    from app.garmin import repository

    today = dt.date.today()

    async def go():
        async with async_session_maker() as s:
            if await repository.strength_sessions(s, uid, weeks=520):
                return
            for days_ago, exercises, hidden in sessions:
                s.add(ActivityRecord(
                    user_id=uid, activity_id=next(_next_id),
                    date=(today - dt.timedelta(days=days_ago)).isoformat(),
                    type="strength_training", dur_min=55.0,
                    exercises=exercises, is_hidden=hidden))
            await s.commit()

    anyio.run(go)


@pytest.fixture
def lifter(client):
    _seed_user(email=EMAIL, password="pw", is_admin=False)
    client.post("/login", data={"email": EMAIL, "password": "pw"})
    uid = _user_id(EMAIL)
    # Twelve weeks of a rising bench, one week apart, plus one hidden session with an
    # absurd weight that must never reach the numbers.
    sessions = [
        (7 * w, _sets("Жим лежачи", [5, 5, 5], [60 + w * 2.5] * 3), False)
        for w in range(12)
    ]
    sessions.append((3, _sets("Жим лежачи", [5], [500.0]), True))
    _seed_strength(uid, sessions)
    return client, uid


def test_the_page_renders_the_lift_and_its_trend(lifter, no_llm_no_garmin):
    client, _uid = lifter
    html = client.get("/strength").text
    assert "Прогресія силових" in html
    assert "Жим лежачи" in html
    assert "Тижневий тонаж" in html


def test_every_number_comes_from_the_module(lifter, no_llm_no_garmin):
    """The page must agree with strengthstats exactly — if it drifts, one of them is
    wrong and the user can't tell which."""
    from app import strengthstats
    from app.db.base import async_session_maker
    from app.garmin import repository
    from app.routers.strength import WEEKS

    client, uid = lifter

    async def compute():
        async with async_session_maker() as s:
            rows = await repository.strength_sessions(s, uid, weeks=WEEKS)
            return strengthstats.weekly_stats(rows)

    weeks = anyio.run(compute)
    html = client.get("/strength").text
    assert f"{weeks[-1]['tonnage_kg']:.0f}" in html
    assert str(weeks[-1]["reps"]) in html
    trend = strengthstats.e1rm_trend(weeks, "Жим лежачи")
    assert trend is not None
    assert f"{trend['change_pct']:+.1f}%" in html


def test_a_hidden_session_is_not_in_the_statistics(lifter, no_llm_no_garmin):
    """Same filter as the records: a hidden activity is hidden everywhere, or a bogus
    500 kg bench becomes a permanent "personal best" on this page."""
    client, _uid = lifter
    html = client.get("/strength").text
    assert "500" not in html


def test_one_record_shows_a_value_not_a_two_point_trend(client, no_llm_no_garmin):
    email = "one-lift@example.com"
    _seed_user(email=email, password="pw", is_admin=False)
    client.post("/login", data={"email": email, "password": "pw"})
    _seed_strength(_user_id(email), [(2, _sets("Присід", [5, 5], [100.0, 100.0]), False)])

    html = client.get("/strength").text
    assert "Присід" in html
    assert "Один запис" in html
    # …and no curve was drawn from a single point.
    assert "<polyline" not in html


def test_an_account_without_strength_gets_an_honest_empty_state(client, no_llm_no_garmin):
    email = "no-strength@example.com"
    _seed_user(email=email, password="pw", is_admin=False)
    client.post("/login", data={"email": email, "password": "pw"})
    r = client.get("/strength")
    assert r.status_code == 200
    assert "не знайдено" in r.text
    assert "Тижневий тонаж" not in r.text


def test_the_estimate_is_labelled_as_an_estimate(lifter, no_llm_no_garmin):
    """Showing a computed 1RM as a fact is misleading — the caveat and its actual
    thresholds come from the module."""
    from app import strengthstats

    client, _uid = lifter
    html = client.get("/strength").text
    assert "Еплі" in html
    assert str(strengthstats.E1RM_MAX_REPS) in html
    assert str(strengthstats.TOP_SETS) in html
    assert str(int(strengthstats.WARMUP_FRACTION * 100)) in html


def test_neither_the_router_nor_the_template_does_arithmetic():
    """No multiplier, no divisor, no rep-scheme constant outside app/strengthstats.py."""
    router = ROUTER.read_text(encoding="utf-8")
    # Strip docstrings/comments — prose may well mention a formula.
    code = re.sub(r'"""(?:.|\n)*?"""', "", router)
    code = re.sub(r"#.*", "", code)
    assert "/ 30" not in code and "* (1 +" not in code, "Epley re-implemented in the router"

    template = TEMPLATE.read_text(encoding="utf-8")
    # Jinja may format numbers ('%.1f') but must not compute them.
    for op in (" * ", " / ", " ** "):
        exprs = re.findall(r"\{\{[^}]*" + re.escape(op) + r"[^}]*\}\}", template)
        assert not exprs, f"arithmetic in strength.html: {exprs}"


def test_the_picker_is_a_plain_link_and_an_unknown_lift_falls_back(lifter, no_llm_no_garmin):
    client, _uid = lifter
    html = client.get("/strength").text
    assert "?exercise=" in html
    # A hand-typed query param must not blow the page up or render an empty chart.
    r = client.get("/strength?exercise=Присідання%20на%20Марсі")
    assert r.status_code == 200
    assert "Жим лежачи" in r.text


def test_no_report_row_is_written(lifter, no_llm_no_garmin):
    from sqlalchemy import func, select

    from app.db.base import async_session_maker
    from app.db.models import ReportLog

    client, uid = lifter

    async def count():
        async with async_session_maker() as s:
            return (await s.execute(
                select(func.count()).select_from(ReportLog).where(ReportLog.user_id == uid)
            )).scalar_one()

    before = anyio.run(count)
    client.get("/strength")
    assert anyio.run(count) == before


def test_the_activity_page_shows_this_session_against_the_last_one(lifter, no_llm_no_garmin):
    """The delta is the point: "did the bench go up since last time" is what a per-session
    strength block exists to answer."""
    from app.db.base import async_session_maker
    from app.garmin import repository

    client, uid = lifter

    async def newest():
        async with async_session_maker() as s:
            rows = await repository.list_activities(s, uid, n=1)
            return rows[0]["id"]

    html = client.get(f"/me/activities/{anyio.run(newest)}").text
    assert "Ця сесія" in html
    assert "кг тонажу" in html
    assert re.search(r"[+\-]\d+\.\d", html), "no e1RM delta against the previous session"


def test_it_needs_a_login(client):
    client.get("/logout")
    r = client.get("/strength", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert r.headers["location"].endswith("/login")
