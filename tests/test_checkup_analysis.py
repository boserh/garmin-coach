"""On-demand Claude interpretation of a health checkup (the "Аналізи" tab's second
follow-up): `checkup_payload` (own results + trend history), `run_checkup_analysis`
(dedup-cached narration, stores `.analysis` on the row)."""
from app.analysis import reports
from app.analysis.client import CallStats
from app.db.models import HealthCheckup

U1 = 1


async def _checkup(session, **kw):
    row = HealthCheckup(
        user_id=U1, date=kw.pop("date", "2026-07-15"), title=kw.pop("title", "Кров"),
        **kw,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def test_checkup_payload_includes_history_only_with_results():
    checkup = HealthCheckup(id=2, date="2026-07-15", title="Кров", category="кров",
                            results=[{"name": "Феритин", "value": "45"}], notes="ок")
    history = [
        HealthCheckup(id=1, date="2026-06-01", title="Кров",
                     results=[{"name": "Феритин", "value": "60"}]),
        HealthCheckup(id=0, date="2026-05-01", title="Кров", results=None),  # dropped
    ]
    data = reports.checkup_payload(checkup, history)
    assert data["category"] == "кров"
    assert data["notes"] == "ок"
    assert len(data["history"]) == 1
    assert data["history"][0]["date"] == "2026-06-01"


def test_checkup_payload_omits_empty_fields():
    checkup = HealthCheckup(id=1, date="2026-07-15", title="Огляд")
    data = reports.checkup_payload(checkup)
    assert "category" not in data and "results" not in data and "notes" not in data
    assert "history" not in data


async def test_run_checkup_analysis_stores_text_and_caches(session, monkeypatch):
    checkup = await _checkup(
        session, results=[{"name": "Феритин", "value": "45", "unit": "нг/мл",
                          "ref_range": "30-400"}])
    calls = {"n": 0}

    def fake_with_stats(context, api_key=None):
        calls["n"] += 1
        return f"розбір #{calls['n']}", CallStats(kind="checkup", model="m")

    monkeypatch.setattr(reports, "checkup_with_stats", fake_with_stats)

    text1 = await reports.run_checkup_analysis(session, checkup, user_id=U1, api_key="k")
    assert text1 == "розбір #1" and calls["n"] == 1
    assert checkup.analysis == "розбір #1"

    # a second call with unchanged data is a dedup-cache hit — no new Claude call
    text2 = await reports.run_checkup_analysis(session, checkup, user_id=U1, api_key="k")
    assert text2 == "розбір #1" and calls["n"] == 1


async def test_run_checkup_analysis_feeds_similar_history(session, monkeypatch):
    older = await _checkup(session, date="2026-06-01", title="Кров",
                           results=[{"name": "Феритин", "value": "60"}])
    newer = await _checkup(session, date="2026-07-15", title="Кров",
                           results=[{"name": "Феритин", "value": "45"}])
    seen = {}

    def fake_with_stats(context, api_key=None):
        seen["context"] = context
        return "text", CallStats(kind="checkup", model="m")

    monkeypatch.setattr(reports, "checkup_with_stats", fake_with_stats)
    await reports.run_checkup_analysis(session, newer, user_id=U1, api_key="k")

    assert seen["context"]["history"][0]["date"] == older.date
