"""UI-04: the post-run check-in works from the browser, and writes what the bot writes.

``ActivityRecord.subjective`` feeds NF-04's injury radar, EP-12's trend, NF-30 and plan
adaptation — and the web could only read it. A missed check-in isn't "one fewer number
in a table", it's those detectors going quiet, so the point of these tests is less "the
form posts" and more "the two entry points cannot drift apart".
"""
import datetime as dt

import anyio
import pytest

from tests.web_helpers import _seed_user, _user_id


def _add_activity(uid, date=None, activity_id=90001, subjective=None):
    from app.db.base import async_session_maker
    from app.db.models import ActivityRecord

    async def go():
        async with async_session_maker() as s:
            row = ActivityRecord(
                user_id=uid, activity_id=activity_id,
                date=(date or dt.date.today().isoformat()),
                type="running", dist_km=10.0, dur_min=52.0, avg_hr=145,
                subjective=subjective)
            s.add(row)
            await s.commit()
            return row.id

    return anyio.run(go)


def _subjective(uid, row_id):
    from app.db.base import async_session_maker
    from app.garmin import repository

    async def go():
        async with async_session_maker() as s:
            act = await repository.get_activity(s, uid, row_id)
            return dict(act.subjective or {})

    return anyio.run(go)


_next_id = iter(range(90100, 90999))


@pytest.fixture
def act(auth_client):
    # A fresh activity per test: the DB outlives a single test, and (user_id,
    # activity_id) is unique.
    uid = _user_id("t@example.com")
    return uid, _add_activity(uid, activity_id=next(_next_id))


def test_one_tap_writes_rpe(auth_client, act):
    uid, row_id = act
    r = auth_client.post(f"/me/activities/{row_id}/checkin", data={"rpe": "7"},
                         follow_redirects=False)
    assert r.status_code == 303
    assert _subjective(uid, row_id) == {"rpe": 7}


def test_the_bot_and_the_web_store_the_same_shape(auth_client, act):
    """One write path (``repository.set_subjective``) and one vocabulary
    (``app.subjective.PAIN_PARTS``) — otherwise a knee logged here stops matching a knee
    logged in Telegram and ``recurring_pain`` never fires."""
    from app.db.base import async_session_maker
    from app.garmin import repository

    uid, web_id = act
    bot_id = _add_activity(uid, activity_id=next(_next_id))

    auth_client.post(f"/me/activities/{web_id}/checkin", data={"rpe": "8"})
    auth_client.post(f"/me/activities/{web_id}/checkin", data={"pain": "knee"})

    async def via_bot():
        async with async_session_maker() as s:
            await repository.set_subjective(s, uid, bot_id, rpe=8)
            await repository.set_subjective(s, uid, bot_id, note="коліно")
            await s.commit()

    anyio.run(via_bot)
    assert _subjective(uid, web_id) == _subjective(uid, bot_id)


def test_the_web_check_in_reaches_the_injury_radar(auth_client, act):
    """The whole point: what the browser writes is what NF-04 reads, with no extra step."""
    from app import injury, subjective

    uid, row_id = act
    auth_client.post(f"/me/activities/{row_id}/checkin", data={"rpe": "9"})
    auth_client.post(f"/me/activities/{row_id}/checkin", data={"pain": "shin"})

    stored = _subjective(uid, row_id)
    assert stored == {"rpe": 9, "pain": True, "note": "гомілка"}

    # Fed straight into the detectors in the shape the repository hands them, with no
    # translation step in between. Both need a repeat before they speak (one sore run is
    # not a pattern), so the same check-in twice is what a real second run would give.
    runs = [{"date": dt.date.today().isoformat(), "pace": 5.2, "type": "easy",
             "rpe": stored["rpe"], "pain": stored["pain"], "note": stored["note"]}] * 2
    assert injury._pain_signal(runs) is not None
    assert subjective.recurring_pain(runs)["part"] == "гомілка"


def test_a_repeat_check_in_updates_and_does_not_duplicate(auth_client, act):
    uid, row_id = act
    auth_client.post(f"/me/activities/{row_id}/checkin", data={"rpe": "4"})
    auth_client.post(f"/me/activities/{row_id}/checkin", data={"rpe": "9"})
    assert _subjective(uid, row_id) == {"rpe": 9}


def test_no_pain_clears_an_earlier_niggle(auth_client, act):
    uid, row_id = act
    auth_client.post(f"/me/activities/{row_id}/checkin", data={"pain": "calf"})
    auth_client.post(f"/me/activities/{row_id}/checkin", data={"pain": "none"})
    stored = _subjective(uid, row_id)
    assert stored["pain"] is False and "note" not in stored


@pytest.mark.parametrize("payload", [{"rpe": "0"}, {"rpe": "11"}, {"rpe": "сім"},
                                     {"pain": "elbow-of-doom"}])
def test_a_bad_value_is_refused_not_stored(auth_client, act, payload):
    uid, row_id = act
    r = auth_client.post(f"/me/activities/{row_id}/checkin", data=payload,
                         follow_redirects=False)
    assert r.headers["location"].endswith("checkin=bad")
    assert _subjective(uid, row_id) == {}


def test_another_users_activity_is_not_writable(auth_client):
    other = "someone-else@example.com"
    _seed_user(email=other, password="pw", is_admin=False)
    theirs = _add_activity(_user_id(other), activity_id=90777)
    r = auth_client.post(f"/me/activities/{theirs}/checkin", data={"rpe": "5"})
    assert r.status_code == 404


def test_the_demo_account_does_not_write(client):
    from sqlalchemy import select

    from app.db.base import async_session_maker
    from app.db.models import User

    _seed_user(email="demo-ci@example.com", password="pw", is_admin=False)
    uid = _user_id("demo-ci@example.com")

    async def mark_demo():
        async with async_session_maker() as s:
            u = (await s.execute(select(User).where(User.id == uid))).scalar_one()
            u.is_demo = True
            await s.commit()

    anyio.run(mark_demo)
    row_id = _add_activity(uid, activity_id=90999)
    client.post("/login", data={"email": "demo-ci@example.com", "password": "pw"})
    r = client.post(f"/me/activities/{row_id}/checkin", data={"rpe": "6"},
                    follow_redirects=False)
    assert r.headers["location"].endswith("checkin=demo")
    assert _subjective(uid, row_id) == {}


def test_the_pills_are_plain_form_buttons(auth_client, act):
    """No JS required: the widget has to be a <form> of <button>s, or the one input the
    analytics depend on stops working the moment a script fails to load."""
    _, row_id = act
    html = auth_client.get(f"/me/activities/{row_id}").text
    assert f'action="/me/activities/{row_id}/checkin"' in html
    assert html.count('type="submit" name="rpe"') == 10
    for _, label in __import__("app.subjective", fromlist=["x"]).PAIN_PARTS:
        assert label in html


def test_the_dashboard_asks_only_about_a_fresh_unrated_session(auth_client):
    from app.routers.dashboard import _checkin_prompt

    today = dt.date.today()
    fresh = {"id": 1, "date": today.isoformat(), "has_checkin": False}
    rated = {"id": 2, "date": today.isoformat(), "has_checkin": True}
    old = {"id": 3, "date": (today - dt.timedelta(days=5)).isoformat(),
           "has_checkin": False}

    assert _checkin_prompt([fresh], today)["id"] == 1
    assert _checkin_prompt([rated, old], today) is None      # nagging about last week: no
    assert _checkin_prompt([rated], today) is None
    # The newest un-rated one wins, not simply the newest.
    assert _checkin_prompt([rated, fresh], today)["id"] == 1


def test_the_dashboard_prompt_renders_and_disappears_once_answered(auth_client):
    uid = _user_id("t@example.com")
    row_id = _add_activity(uid, activity_id=next(_next_id))
    # Keyed on THIS activity: other tests leave un-rated rows behind, and the prompt is
    # about a specific session, not about "some run somewhere".
    target = f'action="/me/activities/{row_id}/checkin"'
    html = auth_client.get("/dashboard").text
    assert "Як пройшло? RPE →" in html and target in html
    auth_client.post(f"/me/activities/{row_id}/checkin", data={"rpe": "6"})
    assert target not in auth_client.get("/dashboard").text


def test_lifestyle_tags_are_writable_from_the_web(auth_client):
    from app.db import lifestyle as lifestyle_db
    from app.db.base import async_session_maker

    uid = _user_id("t@example.com")
    today = dt.date.today().isoformat()
    auth_client.post("/me/lifestyle",
                     data={"date": today, "back": "/dashboard",
                           "tags": ["alcohol", "late_meal"]})

    async def read():
        async with async_session_maker() as s:
            row = await lifestyle_db.get_day(s, uid, today)
            return list(row.tags) if row else None

    assert anyio.run(read) == ["alcohol", "late_meal"]


def test_an_empty_evening_is_stored_as_data_not_as_an_absent_row(auth_client):
    """NF-28's control group: "nothing happened tonight" is a fact the correlation
    engine needs, and is not the same as "never filled in"."""
    from app.db import lifestyle as lifestyle_db
    from app.db.base import async_session_maker

    uid = _user_id("t@example.com")
    day = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    auth_client.post("/me/lifestyle", data={"date": day, "back": "/dashboard"})

    async def read():
        async with async_session_maker() as s:
            row = await lifestyle_db.get_day(s, uid, day)
            return None if row is None else list(row.tags or [])

    assert anyio.run(read) == []


def test_lifestyle_ignores_an_invented_tag(auth_client):
    from app.db import lifestyle as lifestyle_db
    from app.db.base import async_session_maker

    uid = _user_id("t@example.com")
    day = (dt.date.today() - dt.timedelta(days=4)).isoformat()
    auth_client.post("/me/lifestyle",
                     data={"date": day, "tags": ["alcohol", "moon_phase"]})

    async def read():
        async with async_session_maker() as s:
            return list((await lifestyle_db.get_day(s, uid, day)).tags)

    assert anyio.run(read) == ["alcohol"]


def test_the_return_url_cannot_leave_the_app(auth_client):
    """`back` is a form field, i.e. attacker-controllable — an open redirect off a POST
    is a phishing primitive, so anything not app-local falls back to the dashboard."""
    r = auth_client.post("/me/lifestyle",
                         data={"back": "https://evil.example/pwn", "tags": []},
                         follow_redirects=False)
    assert r.headers["location"].startswith("/dashboard")
    r = auth_client.post("/me/lifestyle", data={"back": "//evil.example", "tags": []},
                         follow_redirects=False)
    assert r.headers["location"].startswith("/dashboard")
