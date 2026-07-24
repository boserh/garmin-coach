"""Web smoke tests: the /plan setup form, view, strength preview, season/cycling, adapt."""
from unittest.mock import AsyncMock, patch

import anyio

from app.db import users
from app.db.base import async_session_maker
from tests.web_helpers import _user_id


def test_plan_requires_login(client):
    assert client.get("/plan", follow_redirects=False).status_code == 303


def test_plan_setup_then_view(auth_client):
    from app.garmin import repository
    from app.garmin.schemas import PlanWorkout
    from app.routers import plan as plan_router

    # no active plan → the setup form
    assert "Скласти програму" in auth_client.get("/plan").text

    async def fake_gen(session, **kw):
        return await repository.create_plan(
            session, kw["user_id"], goal=kw["goal"], goal_label=kw["goal_label"],
            target_date=kw["target_date"], start_date=kw["start_date"],
            days_per_week=kw["days_per_week"], intensity=kw["intensity"],
            intake=kw["intake"], summary="тестовий підхід",
            workouts=[PlanWorkout(date="2026-07-01", week=1, type="easy",
                                  dist_km=4.0, description="легкий біг")],
        )

    # POST returns immediately (background generation); GET then shows the waiting page.
    with patch.object(plan_router, "_spawn_plan_generation") as spawn:
        r = auth_client.post(
            "/plan",
            data={"goal": "first_5k", "run_days": ["tue", "thu", "sun"],
                  "long_run_day": "sun", "intensity": "moderate"},
            follow_redirects=False,
        )
    assert r.status_code == 303 and r.headers["location"] == "/plan"
    assert spawn.call_count == 1
    user_id, params = spawn.call_args.args
    assert "Складаємо" in auth_client.get("/plan").text   # pending → waiting page

    # Run the background job deterministically (Claude mocked), then the plan shows.
    with patch.object(plan_router, "run_plan_generation", fake_gen):
        anyio.run(plan_router._generate_plan_bg, user_id, params)
    view = auth_client.get("/plan").text
    assert "Перші 5 км" in view and "тестовий підхід" in view and "легкий біг" in view


def test_strength_preview_route_renders_fragment(auth_client):

    from app.routers import plan as plan_router

    sp = {"name": "Ноги", "warmup_s": 300, "blocks": [
        {"reps": 3, "rest_s": 90, "exercises": [
            {"category": "SQUAT", "exercise": "GOBLET_SQUAT", "reps": 12, "weight_kg": 20}]}]}
    with patch.object(plan_router, "run_strength_preview", AsyncMock(return_value=sp)):
        r = auth_client.post("/plan/strength/preview",
                             data={"description": "силова на ноги", "plan_model": "opus"})
    assert r.status_code == 200
    body = r.text
    assert "strength-preview" in body and "data-hash=" in body
    assert "Ноги" in body   # session name rendered


def test_strength_preview_route_empty_description_400(auth_client):
    r = auth_client.post("/plan/strength/preview", data={"description": "  "})
    assert r.status_code == 400


def test_plan_rejects_fewer_than_two_run_days(auth_client):
    r = auth_client.post(
        "/plan",
        data={"goal": "first_5k", "run_days": ["tue"], "long_run_day": "tue",
              "intensity": "easy"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/plan?error=days"


def test_plan_archive_and_readonly_view(auth_client):
    from app.db.base import async_session_maker
    from app.garmin import repository
    from app.garmin.schemas import PlanWorkout

    uid = _user_id("t@example.com")

    async def seed():
        async with async_session_maker() as s:
            await repository.create_plan(
                s, uid, goal="faster_5k", goal_label="Швидше 5 км", target_date=None,
                start_date="2026-06-01", days_per_week=3, intensity="easy", intake={},
                summary="старий план", workouts=[PlanWorkout(
                    date="2026-06-02", week=1, type="easy", dist_km=3.0, description="легко-арх")])
            # a second create archives the first
            await repository.create_plan(
                s, uid, goal="first_10k", goal_label="Перші 10 км", target_date=None,
                start_date="2026-06-20", days_per_week=3, intensity="moderate", intake={},
                summary="новий план", workouts=[])
            archived = await repository.list_plans(s, uid, status="archived")
            return next(p.id for p in archived if p.summary == "старий план")

    archived_id = anyio.run(seed)

    assert "Швидше 5 км" in auth_client.get("/plan/archive").text  # listed in archive

    view = auth_client.get(f"/plan/{archived_id}").text
    assert "старий план" in view and "легко-арх" in view
    assert "архівна програма" in view
    assert "Архівувати / почати нову" not in view  # readonly — no archive button


def test_plan_view_links_completed_workout_to_activity(auth_client):
    """A done/partial workout matched to an activity (matching.match_activities) should
    link to its /me/activities detail page instead of just showing the distance as text."""
    from app.db.base import async_session_maker
    from app.db.models import ActivityRecord
    from app.garmin import repository
    from app.garmin.schemas import PlanWorkout

    uid = _user_id("t@example.com")

    async def seed():
        async with async_session_maker() as s:
            await repository.create_plan(
                s, uid, goal="first_5k", goal_label="Перші 5 км", target_date=None,
                start_date="2026-06-01", days_per_week=3, intensity="easy", intake={},
                summary="", workouts=[PlanWorkout(
                    date="2026-06-02", week=1, type="easy", dist_km=5.0, description="легкий")])
            plan = await repository.get_active_plan(s, uid)
            workouts = await repository.list_workouts(s, plan.id)
            w = workouts[0]
            act = ActivityRecord(user_id=uid, activity_id=999001, date="2026-06-02",
                                  type="running", dist_km=5.1, dur_min=30.0)
            s.add(act)
            await s.flush()
            w.completed_activity_id = act.id
            w.status = "done"
            w.match_info = {"dist_delta_km": 0.1, "actual_dist_km": 5.1}
            await s.commit()
            return act.id

    act_id = anyio.run(seed)

    view = auth_client.get("/plan").text
    assert f'href="/me/activities/{act_id}"' in view
    assert "км факт" in view


def test_plan_view_links_completed_strength_to_activity(auth_client):
    """A matched STRENGTH session has no distance (match_info.actual_dist_km is None), but
    must still link to its activity — previously the link was gated on distance, so strength
    had no way through from /plan (runs did)."""
    from app.db.base import async_session_maker
    from app.db.models import ActivityRecord
    from app.garmin import repository
    from app.garmin.schemas import PlanWorkout

    uid = _user_id("t@example.com")

    async def seed():
        async with async_session_maker() as s:
            await repository.create_plan(
                s, uid, goal="first_5k", goal_label="Перші 5 км", target_date=None,
                start_date="2026-06-01", days_per_week=3, intensity="easy", intake={},
                summary="", workouts=[PlanWorkout(
                    date="2026-06-03", week=1, type="strength", description="Силова")])
            plan = await repository.get_active_plan(s, uid)
            w = (await repository.list_workouts(s, plan.id))[0]
            act = ActivityRecord(user_id=uid, activity_id=999002, date="2026-06-03",
                                 type="strength_training", dur_min=45.0)
            s.add(act)
            await s.flush()
            w.completed_activity_id = act.id
            w.status = "done"
            w.match_info = {"activity_date": "2026-06-03", "actual_dist_km": None}
            await s.commit()
            return act.id

    act_id = anyio.run(seed)

    view = auth_client.get("/plan").text
    assert f'href="/me/activities/{act_id}"' in view   # clickable link present
    assert "розбір →" in view                          # no distance → plain label


def test_plan_page_shows_weather_chip_and_conflict(auth_client, monkeypatch):
    """ST-13: a stored location + a forecast covering the plan's next-7-days window renders
    a compact weather chip next to the matching session, and flags a heavy session on an
    extreme-weather day (same rule EP-13's daily job uses) as a conflict."""
    import datetime as dt

    from app.garmin import repository
    from app.garmin.schemas import PlanWorkout
    from app.routers import plan as plan_router

    uid = _user_id("t@example.com")
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()

    async def seed():
        async with async_session_maker() as s:
            await repository.create_plan(
                s, uid, goal="faster_5k", goal_label="Швидше 5 км", target_date=None,
                start_date=dt.date.today().isoformat(), days_per_week=3, intensity="moderate",
                intake={}, summary="", workouts=[PlanWorkout(
                    date=tomorrow, week=1, type="tempo", dist_km=6.0, description="темпова")])
            u = await users.get_by_email(s, "t@example.com")
            u.latitude, u.longitude = 50.06, 19.94
            await s.commit()

    anyio.run(seed)

    forecast = [{
        "date": tomorrow, "t_min_c": 24, "t_max_c": 35, "feels_max_c": 34,
        "precip_mm": 0, "precip_prob_pct": 5, "wind_max_kmh": 10, "code": 0, "summary": "спекотно",
    }]
    with patch.object(plan_router.weather, "fetch_forecast_week", return_value=forecast):
        view = auth_client.get("/plan").text

    assert "🌡️34°" in view
    assert "wx-conflict" in view   # tempo session on a 34° day → flagged, same as the job


def test_plan_page_shows_race_pack_block_when_close(auth_client):
    """EP-05: a target race within race.PLAN_BLOCK_DAYS + a previously logged race-pack
    report renders the standing block; a report_logs row from a DIFFERENT kind never does."""
    import datetime as dt

    from app.db.models import ReportLog
    from app.garmin import repository
    from app.garmin.schemas import PlanWorkout

    uid = _user_id("t@example.com")
    target = (dt.date.today() + dt.timedelta(days=6)).isoformat()

    async def seed():
        async with async_session_maker() as s:
            await repository.create_plan(
                s, uid, goal="first_10k", goal_label="Перші 10 км", target_date=target,
                start_date="2026-06-01", days_per_week=3, intensity="moderate", intake={},
                summary="", workouts=[PlanWorkout(
                    date=target, week=1, type="race", dist_km=10.0, description="старт")])
            s.add(ReportLog(user_id=uid, kind="race", model="claude-opus-4-8", ok=True,
                            report_text="⏱ цільовий темп 5:00/км"))
            await s.commit()

    anyio.run(seed)
    view = auth_client.get("/plan").text
    assert "Race pack" in view
    assert "5:00/км" in view


def test_plan_page_no_race_pack_without_target(auth_client):
    """An open-ended (general) plan has no race distance/date → no race-pack block, even
    if a race report happens to exist in the log."""
    from app.db.models import ReportLog
    from app.garmin import repository
    from app.garmin.schemas import PlanWorkout

    uid = _user_id("t@example.com")

    async def seed():
        async with async_session_maker() as s:
            await repository.create_plan(
                s, uid, goal="general", goal_label="Персональний тренер", target_date=None,
                start_date="2026-06-01", days_per_week=3, intensity="moderate", intake={},
                summary="", workouts=[PlanWorkout(
                    date="2026-06-02", week=1, type="easy", dist_km=5.0, description="легко")])
            s.add(ReportLog(user_id=uid, kind="race", model="claude-opus-4-8", ok=True,
                            report_text="стороннiй race pack"))
            await s.commit()

    anyio.run(seed)
    view = auth_client.get("/plan").text
    assert "Race pack" not in view


def test_plan_page_renders_without_chips_when_no_location(auth_client):
    """No stored location → the page renders fine, just without any weather chips."""
    from app.garmin import repository
    from app.garmin.schemas import PlanWorkout

    uid = _user_id("t@example.com")

    async def seed():
        async with async_session_maker() as s:
            await repository.create_plan(
                s, uid, goal="first_5k", goal_label="Перші 5 км", target_date=None,
                start_date="2026-06-01", days_per_week=3, intensity="easy", intake={},
                summary="", workouts=[PlanWorkout(
                    date="2026-06-02", week=1, type="easy", dist_km=4.0, description="легко")])

    anyio.run(seed)
    view = auth_client.get("/plan").text
    assert "🌡️" not in view


def test_plan_page_renders_without_chips_when_forecast_fails(auth_client):
    """A stored location but a failed Open-Meteo fetch (None) → best-effort, no chips,
    page still 200 (same live-fallback pattern as the strength accordion)."""
    from app.garmin import repository
    from app.garmin.schemas import PlanWorkout
    from app.routers import plan as plan_router

    uid = _user_id("t@example.com")

    async def seed():
        async with async_session_maker() as s:
            await repository.create_plan(
                s, uid, goal="first_5k", goal_label="Перші 5 км", target_date=None,
                start_date="2026-06-01", days_per_week=3, intensity="easy", intake={},
                summary="", workouts=[PlanWorkout(
                    date="2026-06-02", week=1, type="easy", dist_km=4.0, description="легко")])
            u = await users.get_by_email(s, "t@example.com")
            u.latitude, u.longitude = 50.06, 19.94
            await s.commit()

    anyio.run(seed)
    with patch.object(plan_router.weather, "fetch_forecast_week", return_value=None):
        r = auth_client.get("/plan")
    assert r.status_code == 200
    assert "🌡️" not in r.text


def test_plan_readonly_view_has_no_weather_chips(auth_client):
    """ST-13 AC: only the ACTIVE plan gets chips — an archived/read-only view never does,
    even with a stored location and a forecast covering that (past) window."""
    from app.garmin import repository
    from app.garmin.schemas import PlanWorkout
    from app.routers import plan as plan_router

    uid = _user_id("t@example.com")

    async def seed():
        async with async_session_maker() as s:
            await repository.create_plan(
                s, uid, goal="faster_5k", goal_label="Швидше 5 км", target_date=None,
                start_date="2026-06-01", days_per_week=3, intensity="easy", intake={},
                summary="старий", workouts=[PlanWorkout(
                    date="2026-06-02", week=1, type="easy", dist_km=4.0, description="легко")])
            await repository.create_plan(   # archives the first
                s, uid, goal="first_10k", goal_label="Перші 10 км", target_date=None,
                start_date="2026-06-20", days_per_week=3, intensity="moderate", intake={},
                summary="новий", workouts=[])
            u = await users.get_by_email(s, "t@example.com")
            u.latitude, u.longitude = 50.06, 19.94
            await s.commit()
            archived = await repository.list_plans(s, uid, status="archived")
            return next(p.id for p in archived if p.summary == "старий")

    archived_id = anyio.run(seed)
    with patch.object(plan_router.weather, "fetch_forecast_week") as fetch:
        view = auth_client.get(f"/plan/{archived_id}").text
    fetch.assert_not_called()
    assert "🌡️" not in view


def test_plan_adjust_level_stored_from_form(auth_client):
    """ST-07: the form's adjust_level lands in intake; when omitted it defaults from
    the goal — a target_date means race prep (conservative), none means flexible."""
    from app.db.base import async_session_maker
    from app.garmin import repository
    from app.routers import plan as plan_router

    uid = _user_id("t@example.com")

    def clear_gen_state():   # the duplicate-submit guard would swallow the next POST
        async def run():
            async with async_session_maker() as s:
                await repository.set_state(s, uid, plan_router.PLAN_GEN_KEY, "")
        anyio.run(run)

    base = {"goal": "first_5k", "run_days": ["tue", "thu"], "long_run_day": "thu",
            "intensity": "moderate"}
    cases = [
        (dict(base, adjust_level="off"), "off"),
        (dict(base, target_date="2026-10-01"), "conservative"),
        (base, "flexible"),
    ]
    for data, expected in cases:
        with patch.object(plan_router, "_spawn_plan_generation") as spawn:
            r = auth_client.post("/plan", data=data, follow_redirects=False)
        assert r.status_code == 303
        _uid, params = spawn.call_args.args
        assert params["intake"]["adjust_level"] == expected
        clear_gen_state()


def test_plan_adjust_level_editable_on_page(auth_client):
    """ST-07: the plan page shows the level and changes it without regeneration."""
    from app.db.base import async_session_maker
    from app.garmin import repository

    uid = _user_id("t@example.com")

    async def seed():
        async with async_session_maker() as s:
            await repository.create_plan(
                s, uid, goal="first_5k", goal_label="Перші 5 км", target_date=None,
                start_date="2026-07-01", days_per_week=2, intensity="moderate",
                intake={}, summary="s", workouts=[])

    anyio.run(seed)
    assert "гнучка" in auth_client.get("/plan").text   # no target_date → flexible

    r = auth_client.post("/plan/adjust-level", data={"adjust_level": "off"},
                         follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/plan"
    assert "вимкнена" in auth_client.get("/plan").text

    # garbage is ignored — the stored level stays
    auth_client.post("/plan/adjust-level", data={"adjust_level": "yolo"})
    assert "вимкнена" in auth_client.get("/plan").text


def test_plan_season_stored_from_form(auth_client):
    """NF-12: the setup form's season fields land in intake["season"]; omitting the sport
    leaves the plan's intake exactly as before (opt-in, zero-change default)."""
    from app.db.base import async_session_maker
    from app.garmin import repository
    from app.routers import plan as plan_router

    uid = _user_id("t@example.com")

    def clear_gen_state():
        async def run():
            async with async_session_maker() as s:
                await repository.set_state(s, uid, plan_router.PLAN_GEN_KEY, "")
        anyio.run(run)

    base = {"goal": "first_5k", "run_days": ["tue", "thu"], "long_run_day": "thu",
            "intensity": "moderate"}
    with patch.object(plan_router, "_spawn_plan_generation") as spawn:
        r = auth_client.post(
            "/plan", data=dict(base, season_sport="kite", season_sessions="4",
                               season_avg_min="120"),
            follow_redirects=False,
        )
    assert r.status_code == 303
    _uid, params = spawn.call_args.args
    assert params["intake"]["season"] == {
        "sport": "kite", "sessions_per_week": 4, "avg_min": 120}
    clear_gen_state()

    with patch.object(plan_router, "_spawn_plan_generation") as spawn:
        auth_client.post("/plan", data=base, follow_redirects=False)
    _uid, params = spawn.call_args.args
    assert "season" not in params["intake"]
    clear_gen_state()


def test_plan_cycling_stored_from_form(auth_client):
    """EP-10 phase 3: the setup form's cycling fields land in intake["cycling"];
    leaving the checkbox unticked (or no days picked) leaves intake unchanged."""
    from app.db.base import async_session_maker
    from app.garmin import repository
    from app.routers import plan as plan_router

    uid = _user_id("t@example.com")

    def clear_gen_state():
        async def run():
            async with async_session_maker() as s:
                await repository.set_state(s, uid, plan_router.PLAN_GEN_KEY, "")
        anyio.run(run)

    base = {"goal": "first_5k", "run_days": ["tue", "thu"], "long_run_day": "thu",
            "intensity": "moderate"}
    with patch.object(plan_router, "_spawn_plan_generation") as spawn:
        r = auth_client.post(
            "/plan", data=dict(base, cycling_enabled="on",
                               cycling_days=["sat", "tue"], cycling_avg_min="45"),
            follow_redirects=False,
        )
    assert r.status_code == 303
    _uid, params = spawn.call_args.args
    assert params["intake"]["cycling"] == {"days": ["tue", "sat"], "avg_min": 45}
    clear_gen_state()

    # checkbox unticked (form still posts the days, browser just wouldn't) → no intake key
    with patch.object(plan_router, "_spawn_plan_generation") as spawn:
        auth_client.post(
            "/plan", data=dict(base, cycling_days=["sat"]), follow_redirects=False)
    _uid, params = spawn.call_args.args
    assert "cycling" not in params["intake"]
    clear_gen_state()


def test_plan_cycling_shown_on_page(auth_client):
    """A plan generated with a cycling intake shows the 🚴 badge on /plan."""
    from app.db.base import async_session_maker
    from app.garmin import repository

    uid = _user_id("t@example.com")

    async def seed():
        async with async_session_maker() as s:
            await repository.create_plan(
                s, uid, goal="first_5k", goal_label="Перші 5 км", target_date=None,
                start_date="2026-07-01", days_per_week=2, intensity="moderate",
                intake={"cycling": {"days": ["tue", "sat"], "avg_min": 60}},
                summary="s", workouts=[])

    anyio.run(seed)
    view = auth_client.get("/plan").text
    assert "Вело-сесії" in view and "Вт" in view and "Сб" in view


def test_plan_season_editable_on_page(auth_client):
    """NF-12: /plan/season sets/clears the active plan's seasonal accent without
    regenerating it, mirroring /plan/adjust-level."""
    from app.db.base import async_session_maker
    from app.garmin import repository

    uid = _user_id("t@example.com")

    async def seed():
        async with async_session_maker() as s:
            await repository.create_plan(
                s, uid, goal="first_5k", goal_label="Перші 5 км", target_date=None,
                start_date="2026-07-01", days_per_week=2, intensity="moderate",
                intake={}, summary="s", workouts=[])

    anyio.run(seed)
    view = auth_client.get("/plan").text
    assert 'value="kite"' in view
    assert 'value="tennis" selected' not in view

    r = auth_client.post(
        "/plan/season", data={"season_sport": "tennis", "season_sessions": "2",
                              "season_avg_min": "60"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/plan"
    view = auth_client.get("/plan").text
    assert 'value="tennis" selected' in view

    # clearing (empty sport) removes the accent again
    auth_client.post("/plan/season", data={"season_sport": ""})
    view = auth_client.get("/plan").text
    assert 'value="tennis" selected' not in view


def test_plan_get_surfaces_background_error_once(auth_client):
    _archive_all_plans()        # no active plan → the setup form renders
    _set_plan_state("err:boom")
    assert "Не вдалось згенерувати" in auth_client.get("/plan").text
    # the error is consumed on first view — a reload no longer shows it
    assert "Не вдалось згенерувати" not in auth_client.get("/plan").text


def test_plan_post_invalid_days_does_not_spawn(auth_client):
    _set_plan_state("")  # clear any leftover pending from a prior test
    from app.routers import plan as plan_router

    with patch.object(plan_router, "_spawn_plan_generation") as spawn:
        r = auth_client.post(
            "/plan",
            data={"goal": "first_5k", "run_days": ["tue"], "intensity": "easy"},
            follow_redirects=False,
        )
    assert r.status_code == 303 and r.headers["location"] == "/plan?error=days"
    assert spawn.call_count == 0




def _set_plan_state(value):
    from app.garmin import repository

    async def go():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, "t@example.com")
            await repository.set_state(s, u.id, "plan_gen", value)

    anyio.run(go)


def _archive_all_plans():
    from sqlalchemy import update

    from app.db.models import TrainingPlan

    async def go():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, "t@example.com")
            await s.execute(update(TrainingPlan).where(
                TrainingPlan.user_id == u.id).values(status="archived"))
            await s.commit()

    anyio.run(go)
