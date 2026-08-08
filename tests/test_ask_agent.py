"""EP-09: the /ask tool-use agent loop — tool dispatch, round/token limits, and the
no-tool-calls-needed fast path. Uses fake sync stand-ins for reports._complete_tools (the
one function that actually talks to Anthropic) so no real API call is ever made."""
import types

import pytest

from app.analysis import reports
from app.analysis.client import AnalystError, CallStats


def _msg(content, stop_reason):
    return types.SimpleNamespace(content=content, stop_reason=stop_reason)


def _text_block(text):
    return types.SimpleNamespace(type="text", text=text)


def _tool_block(id, name, input):
    return types.SimpleNamespace(type="tool_use", id=id, name=name, input=input)


# ---------- _ask_tools schema ----------

def test_ask_tools_schema_has_expected_tools():
    names = {t["name"] for t in reports._ask_tools()}
    assert names == {
        "query_activities", "query_daily", "aggregate_weekly",
        "get_activity_detail", "get_training_plan",
    }
    for t in reports._ask_tools():
        assert t["input_schema"]["type"] == "object"


# ---------- _run_ask_tool dispatch ----------

async def test_run_ask_tool_unknown_name(session):
    got = await reports._run_ask_tool(session, 1, "not_a_tool", {})
    assert "error" in got


async def test_run_ask_tool_query_activities(session):
    from app.garmin import repository

    await repository.upsert_activity(session, 1, 1, {
        "date": "2026-06-01", "type": "running", "dist_km": 5.0, "dur_min": 30.0})
    await session.commit()
    got = await reports._run_ask_tool(session, 1, "query_activities", {"type": "running"})
    assert got["activities"][0]["id"] == 1
    assert got["activities"][0]["avg_pace_minkm"] == 6.0


async def test_run_ask_tool_query_daily(session):
    from app.garmin import repository
    from app.garmin.schemas import DailySummary

    await repository.upsert_daily(session, 1, DailySummary(
        date="2026-06-01", hrv_avg=50, has_data=True))
    await session.commit()
    got = await reports._run_ask_tool(session, 1, "query_daily", {"fields": ["hrv_avg"]})
    assert got["days"] == [{"date": "2026-06-01", "hrv_avg": 50}]


async def test_run_ask_tool_aggregate_weekly_requires_metric(session):
    got = await reports._run_ask_tool(session, 1, "aggregate_weekly", {})
    assert "error" in got


async def test_run_ask_tool_get_activity_detail_missing(session):
    got = await reports._run_ask_tool(session, 1, "get_activity_detail", {"id": 999})
    assert "error" in got


async def test_run_ask_tool_get_activity_detail_bad_id(session):
    got = await reports._run_ask_tool(session, 1, "get_activity_detail", {"id": "abc"})
    assert "error" in got


async def test_run_ask_tool_get_activity_detail_excludes_series(session):
    from app.garmin import repository

    await repository.upsert_activity(session, 1, 1, {
        "date": "2026-06-01", "type": "running", "dist_km": 5.0, "dur_min": 30.0,
        "series": [{"d": 0.1, "p": 6.0, "hr": 140}] * 20,
    })
    await session.commit()
    got = await reports._run_ask_tool(session, 1, "get_activity_detail", {"id": 1})
    assert "series" not in got
    assert "segments" in got  # collapsed, not the raw point cloud


async def test_run_ask_tool_get_training_plan(session):
    from app.db.models import PlannedWorkout, TrainingPlan

    plan = TrainingPlan(user_id=1, goal="first_5k", goal_label="Перші 5К",
                        status="active", target_date="2026-08-01")
    session.add(plan)
    await session.flush()
    session.add(PlannedWorkout(plan_id=plan.id, user_id=1, date="2026-06-10",
                               type="tempo", dist_km=8.0, status="planned"))
    await session.commit()

    got = await reports._run_ask_tool(session, 1, "get_training_plan", {})
    assert got["plan"]["goal_label"] == "Перші 5К"
    assert got["sessions"][0]["type"] == "tempo"


async def test_run_ask_tool_get_training_plan_no_plan(session):
    got = await reports._run_ask_tool(session, 1, "get_training_plan", {})
    assert got == {"plan": None}


async def _plan_with(session, *workouts):
    from app.db.models import PlannedWorkout, TrainingPlan

    plan = TrainingPlan(user_id=1, goal="first_5k", status="active")
    session.add(plan)
    await session.flush()
    for kw in workouts:
        session.add(PlannedWorkout(plan_id=plan.id, user_id=1, status="planned", **kw))
    await session.commit()


async def test_get_training_plan_strength_exercises_from_snapshot(session):
    """ST-09's hole, second half: a cloned-template strength day reads as nothing but
    description="Day 1" unless the tool hands over the stored exercises."""
    await _plan_with(session, {
        "date": "2026-06-10", "type": "strength", "description": "Day 1",
        "garmin_template_id": 931013083,
        "strength_snapshot": {"name": "Day 1", "blocks": [
            {"reps": 3, "rest_s": 90, "exercises": [
                {"category": "SQUAT", "exercise": "BARBELL_BACK_SQUAT", "reps": 8},
            ]},
        ]},
    })
    got = await reports._run_ask_tool(session, 1, "get_training_plan", {})
    ref = got["sessions"][0]["detail"]
    detail = got["session_details"][ref]
    assert detail["name"] == "Day 1"
    assert detail["blocks"][0]["sets"] == 3
    assert detail["blocks"][0]["rest_s"] == 90
    ex = detail["blocks"][0]["exercises"][0]
    assert ex["category"] == "SQUAT" and ex["reps"] == 8
    assert ex["name"] and ex["name"] != "SQUAT"   # readable label, not a raw Garmin code


async def test_get_training_plan_strength_from_scratch_plan_wins(session):
    """A from-scratch session (strength_plan) takes precedence over any snapshot, and its
    per-exercise weight rides along."""
    await _plan_with(session, {
        "date": "2026-06-10", "type": "strength", "description": "Силова",
        "strength_plan": {"name": "Ноги", "blocks": [
            {"reps": 4, "exercises": [
                {"category": "DEADLIFT", "exercise": "BARBELL_DEADLIFT",
                 "reps": 5, "weight_kg": 80.0},
            ]},
        ]},
        "strength_snapshot": {"name": "Day 2", "exercises": [{"category": "BENCH_PRESS"}]},
    })
    got = await reports._run_ask_tool(session, 1, "get_training_plan", {})
    detail = got["session_details"][got["sessions"][0]["detail"]]
    assert detail["name"] == "Ноги"
    assert detail["blocks"][0]["exercises"][0]["weight_kg"] == 80.0


async def test_get_training_plan_legacy_flat_snapshot(session):
    """Older snapshots stored a flat exercise list — still surfaced, as one block."""
    await _plan_with(session, {
        "date": "2026-06-10", "type": "strength", "description": "Day 2",
        "strength_snapshot": {"exercises": [{"category": "BENCH_PRESS", "reps": 10}]},
    })
    got = await reports._run_ask_tool(session, 1, "get_training_plan", {})
    detail = got["session_details"][got["sessions"][0]["detail"]]
    assert [e["category"] for e in detail["blocks"][0]["exercises"]] == ["BENCH_PRESS"]


async def test_get_training_plan_repeated_sessions_share_one_detail(session):
    """Day 1 recurs every week; inlining a copy per date is what would blow the token
    budget, so identical details are stored once and referenced."""
    snap = {"name": "Day 1", "blocks": [
        {"reps": 3, "exercises": [{"category": "SQUAT"}]}]}
    await _plan_with(
        session,
        {"date": "2026-06-10", "type": "strength", "strength_snapshot": snap},
        {"date": "2026-06-17", "type": "strength", "strength_snapshot": dict(snap)},
        {"date": "2026-06-20", "type": "tempo", "dist_km": 8.0,
         "steps": [{"kind": "warmup", "dist_m": 1000}]},
    )
    got = await reports._run_ask_tool(session, 1, "get_training_plan", {})
    refs = [s.get("detail") for s in got["sessions"]]
    assert refs[0] == refs[1] != refs[2]
    assert len(got["session_details"]) == 2
    assert got["session_details"][refs[2]]["steps"][0]["kind"] == "warmup"


async def test_get_training_plan_no_stored_detail_says_nothing(session):
    """An empty snapshot deserialises to Python None (the JSON-null gotcha) — the session
    is returned without a `detail` key rather than with an empty one."""
    await _plan_with(
        session,
        {"date": "2026-06-10", "type": "strength", "description": "Day 1",
         "strength_snapshot": None, "strength_plan": None},
        {"date": "2026-06-11", "type": "easy", "dist_km": 6.0},
    )
    got = await reports._run_ask_tool(session, 1, "get_training_plan", {})
    assert all("detail" not in s for s in got["sessions"])
    assert "session_details" not in got


# ---------- run_ask_agent loop ----------

async def test_run_ask_agent_answers_without_tools(session, monkeypatch):
    def fake(model, system, messages, tools, api_key, max_tokens):
        return _msg([_text_block("просто відповідь")], "end_turn"), \
               CallStats(kind="ask", model=model)

    monkeypatch.setattr(reports, "_complete_tools", fake)
    text, stats, rounds = await reports.run_ask_agent(session, 1, "чи бігти?", [], [], None)
    assert text == "просто відповідь"
    assert rounds == 1


async def test_run_ask_agent_calls_tool_then_answers(session, monkeypatch):
    from app.garmin import repository
    from app.garmin.schemas import DailySummary

    await repository.upsert_daily(session, 1, DailySummary(
        date="2026-06-01", hrv_avg=50, has_data=True))
    await session.commit()

    calls = {"n": 0}

    def fake(model, system, messages, tools, api_key, max_tokens):
        calls["n"] += 1
        if calls["n"] == 1:
            block = _tool_block("t1", "query_daily",
                                {"date_from": "2026-06-01", "date_to": "2026-06-01"})
            return _msg([block], "tool_use"), CallStats(kind="ask", model=model)
        return _msg([_text_block("HRV був 50")], "end_turn"), CallStats(kind="ask", model=model)

    monkeypatch.setattr(reports, "_complete_tools", fake)
    text, stats, rounds = await reports.run_ask_agent(
        session, 1, "який був HRV 1 червня?", [], [], None)
    assert text == "HRV був 50"
    assert rounds == 2
    assert calls["n"] == 2


async def test_run_ask_agent_hits_round_limit(session, monkeypatch):
    def fake(model, system, messages, tools, api_key, max_tokens):
        return _msg([_tool_block("t", "query_activities", {})], "tool_use"), \
               CallStats(kind="ask", model=model)

    monkeypatch.setattr(reports, "_complete_tools", fake)
    text, stats, rounds = await reports.run_ask_agent(session, 1, "?", [], [], None)
    assert text == reports.ASK_LIMIT_TEXT
    assert rounds == reports.MAX_ASK_ROUNDS


async def test_run_ask_agent_stops_on_token_budget(session, monkeypatch):
    def fake(model, system, messages, tools, api_key, max_tokens):
        stats = CallStats(kind="ask", model=model, input_tokens=40_000, output_tokens=0)
        return _msg([_tool_block("t", "query_activities", {})], "tool_use"), stats

    monkeypatch.setattr(reports, "_complete_tools", fake)
    text, stats, rounds = await reports.run_ask_agent(session, 1, "?", [], [], None)
    assert text == reports.ASK_LIMIT_TEXT
    assert rounds < reports.MAX_ASK_ROUNDS  # budget, not the round cap, stopped it
    assert stats.input_tokens >= reports.MAX_ASK_TOTAL_TOKENS


async def test_run_ask_agent_propagates_analyst_error(session, monkeypatch):
    def boom(model, system, messages, tools, api_key, max_tokens):
        raise AnalystError("💥")

    monkeypatch.setattr(reports, "_complete_tools", boom)
    with pytest.raises(AnalystError):
        await reports.run_ask_agent(session, 1, "?", [], [], None)


# ---------- run_ask no longer requires an existing report ----------

async def test_run_ask_works_with_no_reports_yet(session, monkeypatch):
    async def fake_agent(session, user_id, question, reports_, recent_asks, api_key):
        assert reports_ == []
        return "немає даних", CallStats(kind="ask", model="m"), 1

    monkeypatch.setattr(reports, "run_ask_agent", fake_agent)
    text = await reports.run_ask(session, "скільки я пробіг?", user_id=1)
    assert text == "немає даних"
