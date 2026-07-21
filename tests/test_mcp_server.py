"""NF-08: the personal MCP server is a thin read-only wrapper around the same
`_run_ask_tool` dispatch EP-09's `/ask` agent uses — these tests cover the wrapper
(user binding, session handling) since the dispatch logic itself is covered by
tests/test_ask_agent.py."""
import contextlib

import pytest

from app import mcp_server
from app.db import users
from app.garmin import repository


class _FakeMaker:
    """Stand-in for async_session_maker() that hands back the test's shared session
    instead of opening a new engine — the mcp_server module always calls it as
    ``async with async_session_maker() as session``."""

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _reset_user_id():
    mcp_server._user_id = None
    yield
    mcp_server._user_id = None


def test_require_user_id_raises_before_init():
    with pytest.raises(RuntimeError):
        mcp_server._require_user_id()


async def test_resolve_user_id_unknown_email_exits(session, monkeypatch):
    async def fake_init_db():
        return None

    class _FakeMakerModule:
        def __call__(self):
            return contextlib.nullcontext(session)

    monkeypatch.setattr(mcp_server, "init_db", fake_init_db)
    monkeypatch.setattr(mcp_server, "async_session_maker", _FakeMakerModule())
    with pytest.raises(SystemExit):
        await mcp_server._resolve_user_id("nobody@example.com")


async def test_resolve_user_id_returns_id(session, monkeypatch):
    async def fake_init_db():
        return None

    user = await users.create_user(session, email="mcp@example.com", password_hash="x",
                                    is_admin=False, is_approved=True)
    await session.commit()

    class _FakeMakerModule:
        def __call__(self):
            return contextlib.nullcontext(session)

    monkeypatch.setattr(mcp_server, "init_db", fake_init_db)
    monkeypatch.setattr(mcp_server, "async_session_maker", _FakeMakerModule())
    got = await mcp_server._resolve_user_id("mcp@example.com")
    assert got == user.id


async def test_query_activities_tool_scopes_to_bound_user(session, monkeypatch):
    monkeypatch.setattr(mcp_server, "async_session_maker", _FakeMaker(session))
    mcp_server._user_id = 7
    await repository.upsert_activity(session, 7, 101, {
        "date": "2026-06-01", "type": "running", "dist_km": 5.0, "dur_min": 30.0})
    await repository.upsert_activity(session, 8, 202, {
        "date": "2026-06-01", "type": "running", "dist_km": 9.0, "dur_min": 50.0})
    await session.commit()

    got = await mcp_server.query_activities(type="running")
    assert len(got["activities"]) == 1
    assert got["activities"][0]["dist_km"] == 5.0   # user 7's row, never user 8's 9.0 km


async def test_query_daily_tool_filters_fields(session, monkeypatch):
    from app.garmin.schemas import DailySummary

    monkeypatch.setattr(mcp_server, "async_session_maker", _FakeMaker(session))
    mcp_server._user_id = 7
    await repository.upsert_daily(session, 7, DailySummary(
        date="2026-06-01", hrv_avg=50, has_data=True))
    await session.commit()

    got = await mcp_server.query_daily(fields=["hrv_avg"])
    assert got["days"] == [{"date": "2026-06-01", "hrv_avg": 50}]


async def test_aggregate_weekly_tool_requires_metric_error_surfaces(session, monkeypatch):
    monkeypatch.setattr(mcp_server, "async_session_maker", _FakeMaker(session))
    mcp_server._user_id = 7
    got = await mcp_server.aggregate_weekly(metric="")
    assert "error" in got


async def test_get_training_plan_tool_no_plan(session, monkeypatch):
    monkeypatch.setattr(mcp_server, "async_session_maker", _FakeMaker(session))
    mcp_server._user_id = 7
    got = await mcp_server.get_training_plan()
    assert got == {"plan": None}


async def test_get_activity_detail_tool_unknown_id(session, monkeypatch):
    monkeypatch.setattr(mcp_server, "async_session_maker", _FakeMaker(session))
    mcp_server._user_id = 7
    got = await mcp_server.get_activity_detail(id=999999)
    assert "error" in got


async def test_call_raises_without_bound_user(session, monkeypatch):
    monkeypatch.setattr(mcp_server, "async_session_maker", _FakeMaker(session))
    with pytest.raises(RuntimeError):
        await mcp_server._call("query_activities")
