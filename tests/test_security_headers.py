"""Transport hardening: Secure session cookie + the response headers Cloudflare
doesn't add for us. The conftest turns SESSION_HTTPS_ONLY off for the whole suite
(TestClient speaks plain HTTP), so the prod behaviour is asserted by rebuilding the
app with the setting forced back on rather than by trusting the default.
"""
import pytest

from app.core.config import settings


@pytest.fixture
def https_app(monkeypatch):
    """A TestClient over https:// with the production cookie setting in force."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setattr(settings, "SESSION_HTTPS_ONLY", True)
    with TestClient(create_app(), base_url="https://testserver") as c:
        yield c


def test_session_cookie_is_secure_in_prod(https_app):
    r = https_app.post("/demo-login", follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_hsts_sent_when_https_enforced(https_app):
    assert "max-age=" in https_app.get("/login").headers["strict-transport-security"]


def test_no_hsts_from_a_plain_http_process(client):
    # Must stay in its own test: the middleware reads the setting per request, so a
    # https_app fixture in the same test would leave SESSION_HTTPS_ONLY patched on.
    assert "strict-transport-security" not in client.get("/login").headers


def test_clickjacking_and_sniffing_headers_on_login(client):
    h = client.get("/login").headers
    assert h["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
