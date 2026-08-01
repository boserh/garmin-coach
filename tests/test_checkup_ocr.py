"""Photo/PDF → structured checkup(s) (upload OCR, batched): `_coerce_checkup_ocr_batch`
(JSON parsing, tolerant of ``` fences / a bare object / missing fields) and
`run_checkup_ocr_batch` (one Claude vision call for 1+ files → new `HealthCheckup`
row(s), logged even on failure, no dedup cache)."""
import json

import pytest

from app.analysis import reports
from app.analysis.client import CallStats
from app.analysis.service import AnalystError
from app.db import checkups as checkups_db

U1 = 1


def test_coerce_checkup_ocr_batch_parses_multiple_checkups():
    text = json.dumps({"checkups": [
        {
            "title": "Загальний аналіз крові", "date": "2026-07-15", "category": "кров",
            "results": [
                {"name": "Феритин", "value": "45", "unit": "нг/мл", "ref_range": "30-400"},
                {"name": "", "value": "ignored"},  # no name -> dropped
            ],
            "notes": "висновок лікаря",
        },
        {"title": "Огляд лікаря", "results": []},
    ]})
    parsed = reports._coerce_checkup_ocr_batch(text)
    assert len(parsed) == 2
    assert parsed[0]["title"] == "Загальний аналіз крові"
    assert parsed[0]["date"] == "2026-07-15"
    assert parsed[0]["category"] == "кров"
    assert parsed[0]["results"] == [
        {"name": "Феритин", "value": "45", "unit": "нг/мл", "ref_range": "30-400"}
    ]
    assert parsed[0]["notes"] == "висновок лікаря"
    assert parsed[1]["title"] == "Огляд лікаря"


def test_coerce_checkup_ocr_batch_accepts_bare_object_as_single_item():
    """A model that (against instructions) returns one bare object instead of
    {"checkups": [...]} is still accepted as a single-item batch."""
    text = "```json\n" + json.dumps({"title": "Огляд"}) + "\n```"
    parsed = reports._coerce_checkup_ocr_batch(text)
    assert len(parsed) == 1
    assert parsed[0]["title"] == "Огляд"
    assert parsed[0]["date"] is None
    assert parsed[0]["category"] is None
    assert parsed[0]["results"] is None
    assert parsed[0]["notes"] is None


def test_coerce_checkup_ocr_batch_raises_on_garbage():
    with pytest.raises(Exception):
        reports._coerce_checkup_ocr_batch("not json at all")


async def test_run_checkup_ocr_batch_creates_one_checkup_per_item(session, monkeypatch):
    def fake_with_stats(files, api_key=None, max_tokens=None):
        return (
            json.dumps({"checkups": [
                {"title": "Загальний аналіз крові", "date": "2026-07-10", "category": "кров",
                 "results": [{"name": "Феритин", "value": "45", "unit": "нг/мл",
                              "ref_range": "30-400"}], "notes": None},
                {"title": "Гормони", "date": None, "category": None, "results": [], "notes": None},
            ]}),
            CallStats(kind="checkup_ocr", model="m"),
        )

    monkeypatch.setattr(reports, "checkup_ocr_with_stats", fake_with_stats)

    rows = await reports.run_checkup_ocr_batch(
        session, user_id=U1,
        files=[(b"fake-bytes-1", "image/jpeg", "lab1.jpg"),
               (b"fake-bytes-2", "image/jpeg", "lab2.jpg")],
        fallback_date="2026-07-20", api_key="k",
    )
    assert len(rows) == 2
    assert rows[0].title == "Загальний аналіз крові"
    assert rows[0].date == "2026-07-10"  # parsed date wins over fallback
    assert rows[0].results == [{"name": "Феритин", "value": "45", "unit": "нг/мл",
                                "ref_range": "30-400"}]
    assert rows[1].title == "Гормони"
    assert rows[1].date == "2026-07-20"  # missing date falls back

    db_rows = await checkups_db.list_checkups(session, U1)
    ids = {r.id for r in db_rows}
    assert rows[0].id in ids and rows[1].id in ids

    # both uploaded files are attached to BOTH resulting checkups (Claude's response
    # doesn't say which input file informed which output object — see
    # CheckupAttachment's docstring for why per-file attribution isn't recoverable)
    for row in rows:
        attachments = await checkups_db.list_attachments(session, row.id)
        assert {a.filename for a in attachments} == {"lab1.jpg", "lab2.jpg"}
        assert {a.data for a in attachments} == {b"fake-bytes-1", b"fake-bytes-2"}


async def test_run_checkup_ocr_batch_uses_fallback_date_when_missing(session, monkeypatch):
    def fake_with_stats(files, api_key=None, max_tokens=None):
        return json.dumps({"checkups": [{"results": []}]}), CallStats(kind="checkup_ocr", model="m")

    monkeypatch.setattr(reports, "checkup_ocr_with_stats", fake_with_stats)

    rows = await reports.run_checkup_ocr_batch(
        session, user_id=U1, files=[(b"x", "application/pdf", "lab.pdf")],
        fallback_date="2026-07-20", api_key="k",
    )
    assert rows[0].date == "2026-07-20"
    assert rows[0].title == "Аналіз (розпізнано)"


async def test_run_checkup_ocr_batch_raises_on_claude_failure(session, monkeypatch):
    def failing_with_stats(files, api_key=None, max_tokens=None):
        raise AnalystError("боом")

    monkeypatch.setattr(reports, "checkup_ocr_with_stats", failing_with_stats)

    with pytest.raises(AnalystError):
        await reports.run_checkup_ocr_batch(
            session, user_id=U1, files=[(b"x", "image/png", "lab.png")],
            fallback_date="2026-07-20", api_key="k",
        )


async def test_run_checkup_ocr_batch_raises_on_unparseable_reply(session, monkeypatch):
    def fake_with_stats(files, api_key=None, max_tokens=None):
        return "not json", CallStats(kind="checkup_ocr", model="m")

    monkeypatch.setattr(reports, "checkup_ocr_with_stats", fake_with_stats)

    with pytest.raises(AnalystError):
        await reports.run_checkup_ocr_batch(
            session, user_id=U1, files=[(b"x", "image/png", "lab.png")],
            fallback_date="2026-07-20", api_key="k",
        )


async def test_run_checkup_ocr_batch_retries_with_larger_budget_on_truncated_reply(
    session, monkeypatch,
):
    """Regression for a real prod failure: a large panel hit the token budget, cutting
    the JSON off mid-array (stop_reason=max_tokens) — the retry with a bigger budget
    should recover it."""
    calls = []

    def fake_with_stats(files, api_key=None, max_tokens=None):
        calls.append(max_tokens)
        if len(calls) == 1:
            return '{"checkups": [{"title": "Кров", "results": [{"name": "Феритин", "valu', \
                CallStats(kind="checkup_ocr", model="m", output_tokens=1)
        return (
            json.dumps({"checkups": [
                {"title": "Кров", "date": None, "category": None,
                 "results": [{"name": "Феритин", "value": "45", "unit": "нг/мл",
                              "ref_range": "30-400"}], "notes": None},
            ]}),
            CallStats(kind="checkup_ocr", model="m", output_tokens=2),
        )

    monkeypatch.setattr(reports, "checkup_ocr_with_stats", fake_with_stats)

    rows = await reports.run_checkup_ocr_batch(
        session, user_id=U1, files=[(b"x", "image/jpeg", "lab.jpg")],
        fallback_date="2026-07-20", api_key="k",
    )
    assert calls == [None, reports._ocr_retry_max_tokens(1)]
    assert rows[0].title == "Кров"
    assert rows[0].results == [{"name": "Феритин", "value": "45", "unit": "нг/мл",
                               "ref_range": "30-400"}]


async def test_run_checkup_ocr_batch_raises_after_retry_also_fails(session, monkeypatch):
    def fake_with_stats(files, api_key=None, max_tokens=None):
        return "still not json", CallStats(kind="checkup_ocr", model="m")

    monkeypatch.setattr(reports, "checkup_ocr_with_stats", fake_with_stats)

    with pytest.raises(AnalystError):
        await reports.run_checkup_ocr_batch(
            session, user_id=U1, files=[(b"x", "image/png", "lab.png")],
            fallback_date="2026-07-20", api_key="k",
        )


def test_ocr_max_tokens_scales_with_batch_size_and_caps():
    assert reports._ocr_max_tokens(1) == reports.CHECKUP_OCR_MAX_TOKENS_PER_FILE
    assert reports._ocr_max_tokens(2) == reports.CHECKUP_OCR_MAX_TOKENS_PER_FILE * 2
    assert reports._ocr_max_tokens(100) == reports.CHECKUP_OCR_MAX_TOKENS_CAP


def test_ocr_retry_max_tokens_scales_with_batch_size_and_caps():
    assert reports._ocr_retry_max_tokens(1) == reports.CHECKUP_OCR_RETRY_MAX_TOKENS_PER_FILE
    assert reports._ocr_retry_max_tokens(100) == reports.CHECKUP_OCR_RETRY_MAX_TOKENS_CAP
