"""Photo/PDF → structured checkup (upload OCR): `_coerce_checkup_ocr` (JSON parsing,
tolerant of ``` fences and missing fields) and `run_checkup_ocr` (vision call → new
`HealthCheckup` row, logged even on failure, no dedup cache)."""
import json

import pytest

from app.analysis import reports
from app.analysis.client import CallStats
from app.analysis.service import AnalystError
from app.db import checkups as checkups_db

U1 = 1


def test_coerce_checkup_ocr_parses_full_payload():
    text = json.dumps({
        "title": "Загальний аналіз крові",
        "date": "2026-07-15",
        "category": "кров",
        "results": [
            {"name": "Феритин", "value": "45", "unit": "нг/мл", "ref_range": "30-400"},
            {"name": "", "value": "ignored"},  # no name -> dropped
        ],
        "notes": "висновок лікаря",
    })
    parsed = reports._coerce_checkup_ocr(text)
    assert parsed["title"] == "Загальний аналіз крові"
    assert parsed["date"] == "2026-07-15"
    assert parsed["category"] == "кров"
    assert parsed["results"] == [
        {"name": "Феритин", "value": "45", "unit": "нг/мл", "ref_range": "30-400"}
    ]
    assert parsed["notes"] == "висновок лікаря"


def test_coerce_checkup_ocr_tolerates_fences_and_missing_fields():
    text = "```json\n" + json.dumps({"title": "Огляд"}) + "\n```"
    parsed = reports._coerce_checkup_ocr(text)
    assert parsed["title"] == "Огляд"
    assert parsed["date"] is None
    assert parsed["category"] is None
    assert parsed["results"] is None
    assert parsed["notes"] is None


def test_coerce_checkup_ocr_raises_on_garbage():
    with pytest.raises(Exception):
        reports._coerce_checkup_ocr("not json at all")


async def test_run_checkup_ocr_creates_checkup(session, monkeypatch):
    def fake_with_stats(data_b64, media_type, api_key=None):
        return (
            json.dumps({
                "title": "Загальний аналіз крові",
                "date": "2026-07-10",
                "category": "кров",
                "results": [{"name": "Феритин", "value": "45", "unit": "нг/мл",
                             "ref_range": "30-400"}],
                "notes": None,
            }),
            CallStats(kind="checkup_ocr", model="m"),
        )

    monkeypatch.setattr(reports, "checkup_ocr_with_stats", fake_with_stats)

    row = await reports.run_checkup_ocr(
        session, user_id=U1, file_bytes=b"fake-bytes", media_type="image/jpeg",
        fallback_date="2026-07-20", api_key="k",
    )
    assert row.title == "Загальний аналіз крові"
    assert row.date == "2026-07-10"  # parsed date wins over fallback
    assert row.results == [{"name": "Феритин", "value": "45", "unit": "нг/мл",
                            "ref_range": "30-400"}]

    rows = await checkups_db.list_checkups(session, U1)
    assert any(r.id == row.id for r in rows)


async def test_run_checkup_ocr_uses_fallback_date_when_missing(session, monkeypatch):
    def fake_with_stats(data_b64, media_type, api_key=None):
        return json.dumps({"results": []}), CallStats(kind="checkup_ocr", model="m")

    monkeypatch.setattr(reports, "checkup_ocr_with_stats", fake_with_stats)

    row = await reports.run_checkup_ocr(
        session, user_id=U1, file_bytes=b"x", media_type="application/pdf",
        fallback_date="2026-07-20", api_key="k",
    )
    assert row.date == "2026-07-20"
    assert row.title == "Аналіз (розпізнано)"


async def test_run_checkup_ocr_raises_on_claude_failure(session, monkeypatch):
    def failing_with_stats(data_b64, media_type, api_key=None):
        raise AnalystError("боом")

    monkeypatch.setattr(reports, "checkup_ocr_with_stats", failing_with_stats)

    with pytest.raises(AnalystError):
        await reports.run_checkup_ocr(
            session, user_id=U1, file_bytes=b"x", media_type="image/png",
            fallback_date="2026-07-20", api_key="k",
        )


async def test_run_checkup_ocr_raises_on_unparseable_reply(session, monkeypatch):
    def fake_with_stats(data_b64, media_type, api_key=None):
        return "not json", CallStats(kind="checkup_ocr", model="m")

    monkeypatch.setattr(reports, "checkup_ocr_with_stats", fake_with_stats)

    with pytest.raises(AnalystError):
        await reports.run_checkup_ocr(
            session, user_id=U1, file_bytes=b"x", media_type="image/png",
            fallback_date="2026-07-20", api_key="k",
        )
