"""OPS-01: read-only decoding of a stored garth token blob (expiry estimates)."""
import base64
import datetime as dt
import json

import pytest

from app.garmin.token_info import decode_token_info


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.fake-signature"


def _blob(iat=1750291200, expires_at=1750294800, refresh_expires_at=1753000000) -> str:
    oauth1 = {"oauth_token": "t", "oauth_token_secret": "s", "domain": "garmin.com"}
    oauth2 = {
        "scope": "CONNECT_READ", "jti": "x", "token_type": "Bearer",
        "access_token": _jwt({"iat": iat, "exp": expires_at}),
        "refresh_token": "r", "expires_in": 3600, "expires_at": expires_at,
        "refresh_token_expires_in": 86400,
        "refresh_token_expires_at": refresh_expires_at,
    }
    return base64.b64encode(json.dumps([oauth1, oauth2]).encode()).decode()


def test_decode_token_info():
    # iat 1750291200 = 2025-06-19T00:00:00Z (the shape of user 1's real token)
    info = decode_token_info(_blob())
    assert info["domain"] == "garmin.com"
    assert info["oauth1_issued"] == dt.datetime(2025, 6, 19, tzinfo=dt.timezone.utc)
    assert info["oauth1_expiry_est"] == dt.datetime(2026, 6, 19, tzinfo=dt.timezone.utc)
    assert info["oauth2_expires_at"] == dt.datetime.fromtimestamp(1750294800, tz=dt.timezone.utc)
    assert info["oauth2_refresh_expires_at"] == dt.datetime.fromtimestamp(
        1753000000, tz=dt.timezone.utc
    )


def test_decode_token_info_bad_jwt_still_returns_oauth2_facts():
    blob = _blob()
    raw = json.loads(base64.b64decode(blob))
    raw[1]["access_token"] = "not-a-jwt"
    blob = base64.b64encode(json.dumps(raw).encode()).decode()
    info = decode_token_info(blob)
    assert info["oauth1_issued"] is None
    assert info["oauth1_expiry_est"] is None
    assert info["oauth2_expires_at"] is not None


def test_decode_token_info_rejects_garbage():
    with pytest.raises(ValueError):
        decode_token_info("definitely not base64 json!!!")
