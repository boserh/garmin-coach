"""Web layer smoke tests via FastAPI TestClient (Garmin login mocked)."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core import security
from app.garmin import service
from app.main import create_app


@pytest.fixture
def client():
    with patch.object(service, "login", return_value=None):
        with TestClient(create_app()) as c:
            yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_status(client):
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["garmin_login"] == "ok"
    assert "cost_usd_total" in body


def test_history_requires_token(client, monkeypatch):
    monkeypatch.setattr(security.settings, "WEB_TOKEN", "secret")
    assert client.get("/history").status_code == 401
    assert client.get("/history", headers={"X-Token": "secret"}).status_code == 200


def test_ui_browse(client):
    assert client.get("/ui").status_code == 200
    r = client.get("/ui/daily_metrics")
    assert r.status_code == 200
    assert "daily_metrics" in r.text
    assert client.get("/ui/nope").status_code == 404


def test_ui_token_via_query(client, monkeypatch):
    monkeypatch.setattr(security.settings, "WEB_TOKEN", "secret")
    assert client.get("/ui").status_code == 401
    assert client.get("/ui?token=secret").status_code == 200


def test_ui_daily_metrics_has_trend_chart(client):
    import datetime as dt

    import anyio

    from app.db.base import async_session_maker
    from app.garmin import repository
    from app.garmin.schemas import DailySummary

    async def seed():
        async with async_session_maker() as s:
            today = dt.date.today()
            for i, hrv in enumerate([58, 61, 55, 63]):
                d = (today - dt.timedelta(days=i)).isoformat()
                await repository.upsert_daily(
                    s, DailySummary(date=d, hrv_avg=hrv, sleep_h=7.0 + i * 0.2,
                                    sleep_score=80, has_data=True)
                )
            await s.commit()

    anyio.run(seed)
    body = client.get("/ui/daily_metrics").text
    assert 'class="charts"' in body
    assert "HRV avg" in body
    assert "<polyline" in body
    # other tables get no chart
    assert 'class="charts"' not in client.get("/ui/report_logs").text
