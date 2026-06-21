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
