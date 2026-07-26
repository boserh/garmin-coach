"""OPS-01/OPS-10: read-only decoding of a stored session blob (expiry estimates).

Two formats must decode: the native ``garminconnect`` JSON one written since OPS-10,
and the legacy garth one a user still carries until their next login.
"""
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


def _gconn_blob(refresh_token="opaque-refresh", access_exp=1750294800) -> str:
    return json.dumps({
        "di_token": _jwt({"iat": 1750291200, "exp": access_exp}),
        "di_refresh_token": refresh_token,
        "di_client_id": "CONNECT_MOBILE",
    })


def test_decode_garth_token_info():
    # iat 1750291200 = 2025-06-19T00:00:00Z (the shape of user 1's real token)
    info = decode_token_info(_blob())
    assert info["kind"] == "garth"
    assert info["domain"] == "garmin.com"
    assert info["session_issued"] == dt.datetime(2025, 6, 19, tzinfo=dt.timezone.utc)
    assert info["session_expiry_est"] == dt.datetime(2026, 6, 19, tzinfo=dt.timezone.utc)
    assert info["access_expires_at"] == dt.datetime.fromtimestamp(1750294800, tz=dt.timezone.utc)
    assert info["refresh_expires_at"] == dt.datetime.fromtimestamp(
        1753000000, tz=dt.timezone.utc
    )


def test_decode_garth_token_bad_jwt_still_returns_oauth2_facts():
    blob = _blob()
    raw = json.loads(base64.b64decode(blob))
    raw[1]["access_token"] = "not-a-jwt"
    blob = base64.b64encode(json.dumps(raw).encode()).decode()
    info = decode_token_info(blob)
    assert info["session_issued"] is None
    assert info["session_expiry_est"] is None
    assert info["access_expires_at"] is not None


def test_decode_gconn_token_with_jwt_refresh():
    """A JWT refresh token gives the real deadline — its own iat/exp, not the DI access
    token's (that one is refreshed in place every hour)."""
    refresh = _jwt({"iat": 1750291200, "exp": 1781827200})  # 2025-06-19 → 2026-06-19
    info = decode_token_info(_gconn_blob(refresh_token=refresh))
    assert info["kind"] == "gconn"
    assert info["session_issued"] == dt.datetime(2025, 6, 19, tzinfo=dt.timezone.utc)
    assert info["session_expiry_est"] == dt.datetime.fromtimestamp(
        1781827200, tz=dt.timezone.utc
    )
    assert info["access_expires_at"] == dt.datetime.fromtimestamp(
        1750294800, tz=dt.timezone.utc
    )


def test_decode_gconn_token_with_opaque_refresh_is_honest_about_unknown():
    """No JWT to read → no deadline. Reporting None keeps ST-11 silent instead of
    warning off an invented date."""
    info = decode_token_info(_gconn_blob())
    assert info["kind"] == "gconn"
    assert info["session_issued"] is None
    assert info["session_expiry_est"] is None
    assert info["access_expires_at"] is not None   # the DI token is still readable


def test_decode_token_info_rejects_garbage():
    with pytest.raises(ValueError):
        decode_token_info("definitely not base64 json!!!")
    with pytest.raises(ValueError):
        decode_token_info(json.dumps({"something": "else"}))
