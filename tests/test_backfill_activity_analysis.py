"""The CLI that re-attaches activity analyses which were generated but never stored on
the row (the missing commit in _activity_watch_for_user). Reads report_logs, writes
activities.analysis — 0 Claude, 0 Garmin."""
from contextlib import asynccontextmanager

import pytest

from app import cli
from app.db.models import ActivityRecord, ReportLog, User


@pytest.fixture
def _cli_session(session, monkeypatch):
    """Route app.cli's async_session_maker/init_db to the test in-memory session."""
    @asynccontextmanager
    async def maker():
        yield session

    async def _noop_init_db():
        return None

    monkeypatch.setattr(cli, "async_session_maker", maker)
    monkeypatch.setattr(cli, "init_db", _noop_init_db)
    return session


async def _seed(session, *, analysis=None, logged="розбір пробіжки", ok=True):
    user = User(email="baa@x.com", password_hash="h")
    session.add(user)
    await session.commit()
    act = ActivityRecord(user_id=user.id, activity_id=555, date="2026-08-18",
                         type="running", dist_km=8.0, analysis=analysis)
    session.add(act)
    await session.flush()
    if logged is not None:
        session.add(ReportLog(
            user_id=user.id, kind="activity", model="m", ok=ok,
            question=f"activity #{act.id} (running)", report_text=logged))
    await session.commit()
    return user, act


async def test_dry_run_lists_but_writes_nothing(_cli_session, capsys):
    session = _cli_session
    _user, act = await _seed(session)

    assert await cli._backfill_activity_analysis("baa@x.com", apply=False) == 0

    out = capsys.readouterr().out
    assert f"#{act.id}" in out and "Would restore 1" in out and "--apply" in out
    await session.refresh(act)
    assert act.analysis is None


async def test_apply_restores_the_logged_text(_cli_session, capsys):
    session = _cli_session
    _user, act = await _seed(session)

    assert await cli._backfill_activity_analysis("baa@x.com", apply=True) == 0

    assert "Restored 1" in capsys.readouterr().out
    await session.refresh(act)
    assert act.analysis == "розбір пробіжки"


async def test_never_overwrites_an_existing_analysis(_cli_session, capsys):
    session = _cli_session
    _user, act = await _seed(session, analysis="написано раніше")

    assert await cli._backfill_activity_analysis("baa@x.com", apply=True) == 0

    assert "already on their activities" in capsys.readouterr().out
    await session.refresh(act)
    assert act.analysis == "написано раніше"


async def test_ignores_failed_calls_and_says_so_when_nothing_is_logged(_cli_session, capsys):
    session = _cli_session
    _user, act = await _seed(session, ok=False)

    assert await cli._backfill_activity_analysis("baa@x.com", apply=True) == 0

    assert "nothing to restore" in capsys.readouterr().out
    await session.refresh(act)
    assert act.analysis is None
