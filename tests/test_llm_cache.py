"""PERF-02: the DB-backed Claude dedup cache (llm_cache table) — key/TTL semantics,
cross-process visibility (two engines on one SQLite file), and the run_* hit path."""
import time

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import llm_cache
from app.db.base import Base
from app.db.models import LlmCache, ReportLog


async def test_put_get_roundtrip(session):
    await llm_cache.put(session, "k1", "звіт", ttl_s=60)
    assert await llm_cache.get(session, "k1") == "звіт"
    assert await llm_cache.get(session, "missing") is None


async def test_expired_entry_is_a_miss(session):
    await llm_cache.put(session, "k1", "старий", ttl_s=-1)
    assert await llm_cache.get(session, "k1") is None


async def test_put_upserts_same_key(session):
    await llm_cache.put(session, "k1", "перший", ttl_s=60)
    await llm_cache.put(session, "k1", "другий", ttl_s=60)
    assert await llm_cache.get(session, "k1") == "другий"
    n = (await session.execute(select(func.count()).select_from(LlmCache))).scalar_one()
    assert n == 1


async def test_put_purges_expired_rows(session):
    await llm_cache.put(session, "dead", "x", ttl_s=-1)
    await llm_cache.put(session, "alive", "y", ttl_s=60)
    keys = (await session.execute(select(LlmCache.key))).scalars().all()
    assert keys == ["alive"]


async def test_cross_process_hit(tmp_path):
    """The bug PERF-02 fixes: an entry written by one process (engine) must be a
    hit in the other. Two independent engines over the same SQLite file."""
    url = f"sqlite+aiosqlite:///{tmp_path}/cache.db"

    engine_a = create_async_engine(url)
    async with engine_a.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine_a)() as s:
        await llm_cache.put(s, "shared", "звіт від бота", ttl_s=60)
    await engine_a.dispose()

    engine_b = create_async_engine(url)  # "the web process"
    async with async_sessionmaker(engine_b)() as s:
        assert await llm_cache.get(s, "shared") == "звіт від бота"
    await engine_b.dispose()


async def test_get_survives_db_failure(session):
    """A cache failure must never break the analysis call — a failed read is a miss."""
    await session.close()  # simulate a broken session
    assert await llm_cache.get(session, "k") is None


# ---------- run_analysis / run_ask hit path ----------

_PAYLOAD = {"daily": [], "recent_activities": [], "planned_runs": [],
            "synced_today": True, "has_data": True}


@pytest.fixture
def fake_analyze(monkeypatch):
    """Stub the paid Claude call and count invocations."""
    from app.analysis import service

    calls = []

    def fake(payload, question="", deep=False, kind=None, previous_report=None,
             api_key=None, weather=None, plan_today=None, fitness=None, records=None):
        calls.append(question)
        return "свіжий звіт", service.CallStats(kind=kind or "report", model="m",
                                                input_tokens=10, output_tokens=5)

    monkeypatch.setattr(service, "analyze_with_stats", fake)
    return calls


async def test_run_analysis_second_call_is_cache_hit(session, fake_analyze):
    from app.analysis import service

    t1 = await service.run_analysis(session, _PAYLOAD, question="q")
    t2 = await service.run_analysis(session, _PAYLOAD, question="q")
    assert t1 == t2 == "свіжий звіт"
    assert len(fake_analyze) == 1  # second call served from llm_cache

    logs = (await session.execute(
        select(ReportLog.cached).order_by(ReportLog.id))).scalars().all()
    assert logs == [False, True]  # the hit is still cost-logged, flagged cached


async def test_run_analysis_different_question_misses(session, fake_analyze):
    from app.analysis import service

    await service.run_analysis(session, _PAYLOAD, question="q1")
    await service.run_analysis(session, _PAYLOAD, question="q2")
    assert len(fake_analyze) == 2


async def test_run_ask_second_call_is_cache_hit(session, monkeypatch):
    from app.analysis import service
    from app.garmin import repository

    await repository.log_report(session, user_id=None, kind="report", model="m",
                                ok=True, report_text="денний звіт")
    # Pin the /ask thread: the first answer would otherwise enter recent_asks and
    # (correctly) change the second call's key — here we test the hit path itself.
    async def no_asks(session, user_id, minutes):
        return []

    monkeypatch.setattr(repository, "get_recent_asks", no_asks)
    calls = []

    def fake_ask(reports, question, api_key=None, recent_asks=None):
        calls.append(question)
        return "відповідь", service.CallStats(kind="ask", model="m")

    monkeypatch.setattr(service, "ask_with_stats", fake_ask)
    a1 = await service.run_ask(session, "чи бігти?")
    a2 = await service.run_ask(session, "чи бігти?")
    assert a1 == a2 == "відповідь"
    assert len(calls) == 1


async def test_expiry_makes_run_analysis_refetch(session, fake_analyze, monkeypatch):
    from app.analysis import service

    monkeypatch.setattr(service, "CACHE_TTL_S", -1)  # everything written already stale
    await service.run_analysis(session, _PAYLOAD, question="q")
    await service.run_analysis(session, _PAYLOAD, question="q")
    assert len(fake_analyze) == 2


async def test_llm_cache_get_ignores_row_expired_between_puts(session):
    """A row can sit expired until the next put purges it — get must not serve it."""
    session.add(LlmCache(key="k", value="v", expires_at=time.time() - 5))
    await session.commit()
    assert await llm_cache.get(session, "k") is None
