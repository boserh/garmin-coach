"""NF-29 — «А що, якщо…»: the scenario simulator.

The ticket's headline requirement is the negative one — **no Claude call anywhere on the
path**, including a bot request it cannot parse — followed by "nothing is written" and "the
thresholds are the live ones, not a copy". The arithmetic tests sit on the pure module."""
import datetime as dt
from types import SimpleNamespace

import anyio
import pytest

from app import loadforecast, whatif
from app.whatif import Inputs, Scenario
from tests.web_helpers import _seed_user, _user_id

MON = dt.date(2026, 10, 26)        # a Monday — makes the week arithmetic readable


def _plan_inputs(today=MON, *, weeks=6, past=(300.0, 300.0, 300.0, 300.0), goal=None):
    """Four runs a week (two easy, a tempo, a long) for ``weeks`` weeks from today's Monday."""
    planned = []
    monday = today - dt.timedelta(days=today.weekday())
    for w in range(weeks):
        base = monday + dt.timedelta(weeks=w)
        for off, typ, km in [(1, "easy", 6.0), (3, "tempo", 8.0), (4, "easy", 6.0),
                             (6, "long", 14.0)]:
            planned.append({"date": (base + dt.timedelta(days=off)).isoformat(),
                            "type": typ, "dist_km": km, "steps": None})
    return Inputs(today=today, past_weekly_loads=list(past), history_days=120,
                  anchor_pace=6.0, planned=planned, has_plan=True, goal=goal)


def _no_plan_inputs(today=MON):
    return Inputs(today=today, past_weekly_loads=[280.0, 300.0, 320.0, 300.0],
                  history_days=120, anchor_pace=6.0, has_plan=False,
                  typical={"sessions": 4, "session_load": 75.0, "session_km": 8.0})


# ---------- the pure module ----------

def test_baseline_scenario_changes_nothing():
    r = whatif.simulate(_plan_inputs(), Scenario())
    for row in r["weeks"]:
        assert row["base"] == row["scen"]
    assert r["flags"] == [] and r["apply"] is None


def test_inputs_are_not_mutated():
    inputs = _plan_inputs()
    before = [dict(p) for p in inputs.planned]
    whatif.simulate(inputs, Scenario(sessions_delta=2, volume_pct=30, skip_days=10))
    assert inputs.planned == before


def test_plus_one_session_adds_an_easy_session_each_week():
    r = whatif.simulate(_plan_inputs(), Scenario(sessions_delta=1))
    for row in r["weeks"]:
        assert row["scen"]["sessions"] == row["base"]["sessions"] + 1
        assert row["scen"]["load"] > row["base"]["load"]
        # an easy session of the plan's own easy length (median 6 km)
        assert row["scen"]["km"] == pytest.approx(row["base"]["km"] + 6.0)


def test_minus_one_session_drops_the_lightest_not_the_long_run():
    r = whatif.simulate(_plan_inputs(), Scenario(sessions_delta=-1))
    w = r["weeks"][1]
    assert w["scen"]["sessions"] == w["base"]["sessions"] - 1
    assert w["base"]["km"] - w["scen"]["km"] == pytest.approx(6.0)   # an easy 6 km went


def test_volume_scales_load_and_km():
    r = whatif.simulate(_plan_inputs(), Scenario(volume_pct=20))
    w = r["weeks"][1]
    assert w["scen"]["km"] == pytest.approx(w["base"]["km"] * 1.2, abs=0.2)


def test_skip_a_week_drops_chronic_and_spikes_the_return():
    """The AC: a 7-day absence shows the chronic load falling and an ACWR jump on return."""
    r = whatif.simulate(_plan_inputs(), Scenario(skip_days=7))
    weeks = r["weeks"]
    assert weeks[0]["scen"]["load"] == 0                      # the week away
    assert weeks[1]["scen"]["load"] == weeks[1]["base"]["load"]  # back to the plan
    assert weeks[1]["scen"]["chronic"] < weeks[1]["base"]["chronic"]
    assert weeks[1]["scen"]["acwr"] > weeks[1]["base"]["acwr"]


def test_levels_come_from_loadforecast_with_the_live_thresholds(monkeypatch):
    """"No second copy of the constants": every level is loadforecast.acwr_level's verdict
    for the thresholds the caller passes (the router passes settings.FORECAST_ACWR_*)."""
    calls = []
    real = loadforecast.acwr_level

    def spy(acwr, **kw):
        calls.append(kw)
        return real(acwr, **kw)

    monkeypatch.setattr(loadforecast, "acwr_level", spy)
    r = whatif.simulate(_plan_inputs(), Scenario(skip_days=14), warn_acwr=1.1, high_acwr=1.2)
    assert calls and all(kw == {"warn_acwr": 1.1, "high_acwr": 1.2} for kw in calls)
    for row in r["weeks"]:
        s = row["scen"]
        if s["acwr"] is not None:
            assert s["level"] == real(s["acwr"], warn_acwr=1.1, high_acwr=1.2)
    assert not any(hasattr(whatif, n) for n in ("ACWR_WARN", "ACWR_HIGH"))


def test_a_return_spike_is_flagged():
    r = whatif.simulate(_plan_inputs(), Scenario(skip_days=14), warn_acwr=1.2, high_acwr=1.5)
    assert any("ACWR" in f for f in r["flags"])
    assert any("Хронічне навантаження нижче" in f for f in r["flags"])


def test_short_history_is_calibrating_not_a_number():
    inputs = _plan_inputs()
    inputs.history_days = 5
    r = whatif.simulate(inputs, Scenario(skip_days=7))
    assert r["calibrating"] is True
    assert all(row["scen"]["acwr"] is None for row in r["weeks"])
    assert r["flags"] == [] or all("ACWR" not in f for f in r["flags"])


def test_without_a_plan_volume_works_and_goal_is_silent():
    r = whatif.simulate(_no_plan_inputs(), Scenario(sessions_delta=1, target_shift_weeks=3))
    assert r["has_plan"] is False and r["goal"] is None and r["apply"] is None
    w = r["weeks"][1]
    assert w["base"]["sessions"] == 4 and w["scen"]["sessions"] == 5
    assert w["scen"]["load"] == pytest.approx(w["base"]["load"] + 75.0)
    assert "Без активного плану" in whatif.summary(r)


def test_no_data_at_all():
    r = whatif.simulate(Inputs(today=MON), Scenario(sessions_delta=1))
    assert r["has_data"] is False
    assert "нема що рахувати" in whatif.summary(r)


def _goal_inputs():
    hist = []
    for i in range(6):   # six weeks of a 5k prediction improving 10 s/week
        d = MON - dt.timedelta(weeks=6 - i)
        hist.append({"date": d.isoformat(), "race_5k_s": 1500 - 10 * i})
    return {"history": hist, "metric_key": "race_5k_s", "label": "прогноз на 5 км",
            "higher_better": False, "target_date": (MON + dt.timedelta(weeks=4)).isoformat(),
            "target_s": None}


def test_shifting_the_race_projects_to_the_new_date():
    r = whatif.simulate(_plan_inputs(goal=_goal_inputs()), Scenario(target_shift_weeks=3))
    g = r["goal"]
    assert g["new_target_date"] == (MON + dt.timedelta(weeks=7)).isoformat()
    assert g["scen"]["projected"] < g["base"]["projected"]     # 3 more weeks of the trend
    assert ":" in g["base_txt"]                               # formatted as a time
    assert "Старт переноситься" in r["apply"]


def test_shift_too_far_says_trend_only():
    r = whatif.simulate(_plan_inputs(goal=_goal_inputs()), Scenario(target_shift_weeks=12))
    assert r["goal"]["scen"]["projected"] is None
    assert "задалеко" in whatif.summary(r)


def test_skip_offers_apply_with_dates():
    r = whatif.simulate(_plan_inputs(), Scenario(skip_days=7))
    assert "2026-10-26" in r["apply"] and "2026-11-01" in r["apply"]


def test_scenario_is_clamped():
    sc = Scenario(sessions_delta=99, volume_pct=-500, target_shift_weeks=40, skip_days=-3)
    assert sc.clamped() == Scenario(3, -50, 12, 0)
    assert whatif.scenario_from_query({"sessions": "x", "skip": "999"}) == Scenario(0, 0, 0, 28)


@pytest.mark.parametrize("text, expected", [
    ("+1 сесія", Scenario(sessions_delta=1)),
    ("-2 тренування", Scenario(sessions_delta=-2)),
    ("+1", Scenario(sessions_delta=1)),
    ("старт +3 тижні", Scenario(target_shift_weeks=3)),
    ("старт -2", Scenario(target_shift_weeks=-2)),
    ("пропуск 7д", Scenario(skip_days=7)),
    ("пропуск 10 днів", Scenario(skip_days=10)),
    ("пропуск 2 тижні", Scenario(skip_days=14)),
    ("обсяг +20%", Scenario(volume_pct=20)),
    ("+1 сесія, обсяг -10%", Scenario(sessions_delta=1, volume_pct=-10)),
    ("старт +3 тижні пропуск 5д", Scenario(target_shift_weeks=3, skip_days=5)),
])
def test_parse_understands_the_bounded_phrases(text, expected):
    assert whatif.parse(text) == expected


@pytest.mark.parametrize("text", [
    "", "привіт", "а якщо я біжу марафон?", "+1 сесія і ще щось", "старт",
    "обсяг 0%",
])
def test_parse_rejects_everything_else(text):
    assert whatif.parse(text) is None


# ---------- the web page ----------

EMAIL = "whatif@example.com"


@pytest.fixture
def no_llm_no_garmin(monkeypatch):
    import app.analysis.client as client_mod
    import app.garmin.providers as providers

    def boom(*a, **kw):
        raise AssertionError("/whatif must not call out to Claude/Garmin")

    monkeypatch.setattr(client_mod, "_get_client", boom, raising=False)
    monkeypatch.setattr(providers, "get_provider", boom, raising=False)


def _seed_plan(uid):
    from app.db.base import async_session_maker
    from app.db.models import ActivityRecord, DailyMetric
    from app.garmin import repository

    today = dt.date.today()

    async def go():
        async with async_session_maker() as s:
            if await repository.get_active_plan(s, uid):
                return
            for i in range(40):
                d = (today - dt.timedelta(days=i)).isoformat()
                s.add(DailyMetric(user_id=uid, date=d, hrv_avg=55,
                                  extra={"race_5k_s": 1500 + i}))
                if i % 2 == 0:
                    s.add(ActivityRecord(user_id=uid, activity_id=880000 + i, date=d,
                                         type="running", dist_km=8.0, dur_min=46.0,
                                         avg_hr=145))
            workouts = [SimpleNamespace(
                date=(today + dt.timedelta(days=i)).isoformat(), week=1 + i // 7,
                type="easy" if i % 3 else "tempo", dist_km=7.0, description="біг",
                steps=None) for i in range(0, 35, 2)]
            await repository.create_plan(
                s, uid, goal="faster_5k", goal_label="5 км", start_date=today.isoformat(),
                target_date=(today + dt.timedelta(weeks=6)).isoformat(), days_per_week=3,
                intensity="moderate", intake={}, summary="", workouts=workouts)

    anyio.run(go)


@pytest.fixture
def page_client(client):
    _seed_user(email=EMAIL, password="pw", is_admin=False)
    client.post("/login", data={"email": EMAIL, "password": "pw"})
    _seed_plan(_user_id(EMAIL))
    return client


def _snapshot():
    """Every row of every table, as plain tuples — a moved race date must change none."""
    from sqlalchemy import select

    from app.db.base import Base, async_session_maker

    async def go():
        out = {}
        async with async_session_maker() as s:
            for name, table in Base.metadata.tables.items():
                rows = (await s.execute(select(table))).all()
                out[name] = sorted(repr(tuple(r)) for r in rows)
        return out

    return anyio.run(go)


def test_page_renders_a_scenario_and_spends_nothing(page_client, no_llm_no_garmin):
    r = page_client.get("/whatif?sessions=1&volume=20&shift=3&skip=7")
    assert r.status_code == 200
    assert "А що, якщо" in r.text
    assert "Застосувати до плану" in r.text and "/chat?draft=" in r.text


def test_page_writes_nothing(page_client, no_llm_no_garmin):
    page_client.get("/whatif")          # warm-up: login/session rows settle first
    before = _snapshot()
    page_client.get("/whatif?shift=4&skip=10&sessions=-1")
    assert _snapshot() == before


def test_fresh_account_gets_an_honest_empty_state(client, no_llm_no_garmin):
    _seed_user(email="whatif-empty@example.com", password="pw", is_admin=False)
    client.post("/login", data={"email": "whatif-empty@example.com", "password": "pw"})
    r = client.get("/whatif?sessions=1")
    assert r.status_code == 200 and "нема з чого" in r.text


def test_chat_draft_prefills_the_composer(page_client):
    r = page_client.get("/chat?draft=" + "перенеси%20старт")
    assert "перенеси старт</textarea>" in r.text


# ---------- the bot ----------

class _Msg:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))


async def test_bot_unparsed_request_gets_help_not_an_llm(monkeypatch):
    """The AC's pitfall: anything not understood → the syntax hint; the analysis service is
    never touched."""
    import app.analysis.client as client_mod
    from bot import handlers

    monkeypatch.setattr(client_mod, "_get_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM")),
                        raising=False)
    msg = _Msg()
    update = SimpleNamespace(message=msg)
    ctx = SimpleNamespace(args=["а", "якщо", "марафон?"])
    await handlers.whatif_cmd.__wrapped__(update, ctx, None, SimpleNamespace(id=1))
    assert "Не впізнав" in msg.replies[0][0] and "/whatif" in msg.replies[0][0]


async def test_bot_whatif_answers_with_apply_button(monkeypatch):
    from bot import handlers

    async def fake_inputs(session, user_id, *, today=None):
        return _plan_inputs(today=MON)

    monkeypatch.setattr(handlers.repository, "whatif_inputs", fake_inputs)
    monkeypatch.setattr(handlers, "user_today", lambda u: MON)
    msg = _Msg()
    update = SimpleNamespace(message=msg)
    ctx = SimpleNamespace(args=["пропуск", "7д"])
    await handlers.whatif_cmd.__wrapped__(update, ctx, None, SimpleNamespace(id=1))
    text, kw = msg.replies[0]
    assert "пропуск 7 дн" in text and "ACWR" in text
    button = kw["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "wi:0:7"
