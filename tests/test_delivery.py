"""CODE-05: the shared report pipeline (app.analysis.delivery.build_report)."""
from types import SimpleNamespace
from unittest.mock import patch

import anyio

from app.analysis import delivery


def test_build_report_returns_text_and_sync_flags():
    """build_report runs the analysis on a pre-built payload and echoes its sync flags,
    forwarding question/kind/weather/api_key through to run_analysis unchanged."""
    payload = SimpleNamespace(synced_today=True, last_data_date="2026-07-09")
    user = SimpleNamespace(id=7)
    captured = {}

    async def fake_run_analysis(session, pl, *, user_id, question, kind, api_key, weather):
        captured.update(user_id=user_id, question=question, kind=kind,
                        api_key=api_key, weather=weather, payload=pl)
        return "аналіз"

    async def go():
        with patch.object(delivery, "run_analysis", fake_run_analysis):
            return await delivery.build_report(
                None, user, payload, question="q", kind="report",
                api_key="k", weather={"summary": "clear"},
            )

    result = anyio.run(go)
    assert result.text == "аналіз"
    assert result.synced_today is True
    assert result.last_data_date == "2026-07-09"
    assert captured == {
        "user_id": 7, "question": "q", "kind": "report",
        "api_key": "k", "weather": {"summary": "clear"}, "payload": payload,
    }
