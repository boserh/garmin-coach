"""ST-19: regenerate a stored activity analysis.

``force=True`` must skip the dedup-cache *get* (so a paid re-run actually happens after
resynced data / a poor first write) yet still write the fresh text to both the cache and
``ActivityRecord.analysis`` — the next non-force call is a hit of the new text."""
import datetime as dt

from app.analysis import reports
from app.analysis.client import CallStats
from app.db.models import ActivityRecord

U1 = 1
TODAY = dt.date.today().isoformat()


async def _activity(session):
    act = ActivityRecord(user_id=U1, activity_id=222, date=TODAY, type="running",
                         dist_km=5.0, dur_min=30.0)
    session.add(act)
    await session.commit()
    await session.refresh(act)
    return act


async def test_force_bypasses_cache_and_rewrites(session, monkeypatch):
    act = await _activity(session)
    calls = {"n": 0}

    def fake_analyze(activity_data, api_key=None):
        calls["n"] += 1
        return f"аналіз #{calls['n']}", CallStats(kind="activity", model="m")

    monkeypatch.setattr(reports, "analyze_activity_with_stats", fake_analyze)

    # 1) first run — LLM called, result cached + stored.
    t1 = await reports.run_activity_analysis(session, act, user_id=U1, api_key="k")
    assert t1 == "аналіз #1" and calls["n"] == 1
    assert act.analysis == "аналіз #1"

    # 2) non-force again — a cache HIT, no new LLM call, same text.
    t2 = await reports.run_activity_analysis(session, act, user_id=U1, api_key="k")
    assert t2 == "аналіз #1" and calls["n"] == 1

    # 3) force — cache get skipped, LLM called again, fresh text stored.
    t3 = await reports.run_activity_analysis(
        session, act, user_id=U1, api_key="k", force=True)
    assert t3 == "аналіз #2" and calls["n"] == 2
    assert act.analysis == "аналіз #2"

    # 4) non-force once more — hits the freshly-written cache (no new call).
    t4 = await reports.run_activity_analysis(session, act, user_id=U1, api_key="k")
    assert t4 == "аналіз #2" and calls["n"] == 2


async def test_force_logs_uncached_report(session, monkeypatch):
    from sqlalchemy import select

    from app.db.models import ReportLog

    act = await _activity(session)
    monkeypatch.setattr(
        reports, "analyze_activity_with_stats",
        lambda data, api_key=None: ("розбір", CallStats(kind="activity", model="m")))

    await reports.run_activity_analysis(
        session, act, user_id=U1, api_key="k", force=True)

    rows = (await session.execute(
        select(ReportLog).where(ReportLog.user_id == U1, ReportLog.kind == "activity"))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].cached is False   # a forced re-run is a real, billable call
