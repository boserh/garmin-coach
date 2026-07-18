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
    assert names == {"query_activities", "query_daily", "aggregate_weekly", "get_activity_detail"}
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
