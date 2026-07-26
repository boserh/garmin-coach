"""Read-only introspection of a stored Garmin session token (OPS-01, OPS-10).

Two blob formats exist, and a deployment can hold both at once (a user who hasn't
re-logged in since the migration still carries the old one):

* **native** (``garminconnect.client.Client.dumps()``, the default since OPS-10) —
  plain JSON ``{di_token, di_refresh_token, di_client_id}``. The DI *access* token is
  short-lived and refreshed in place; the deadline that actually matters is the
  *refresh* token's, so its JWT ``iat``/``exp`` are what we report as the session
  window. Garmin doesn't have to issue a JWT there — when it isn't one, we honestly
  report "unknown" (``None``) instead of inventing a date.
* **garth** (pre-OPS-10) — base64 of ``[oauth1_dict, oauth2_dict]``. The OAuth1 token
  carries no timestamps, but we only ever persisted the blob right after a fresh
  login, so the OAuth2 access token's JWT ``iat`` equals the OAuth1 issue time;
  Garmin OAuth1 tokens live ~1 year from issue.

``session_issued``/``session_expiry_est`` are the engine-neutral pair every caller
reads (ST-11's warning, ``app.cli token-expiry``): "when did this session start" and
"when does re-login become mandatory".

Pure decoding, no network and no writes.
"""
import base64
import datetime as dt
import json
from typing import Optional

OAUTH1_LIFETIME_DAYS = 365  # empirical: Garmin OAuth1 tokens live ~1 year (garth blobs)


def _jwt_claims(jwt) -> dict:
    """Decode a JWT payload without verifying the signature (we only read it)."""
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore stripped base64 padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _ts(epoch) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromtimestamp(int(epoch), tz=dt.timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _decode_gconn(data: dict) -> dict:
    refresh = _jwt_claims(data.get("di_refresh_token") or "")
    access = _jwt_claims(data.get("di_token") or "")
    issued = _ts(refresh.get("iat"))
    return {
        "kind": "gconn",
        "domain": None,
        # An opaque (non-JWT) refresh token leaves both as None on purpose: the
        # session then has no knowable deadline, and a made-up one would drive a
        # wrong "re-login now" warning.
        "session_issued": issued,
        "session_expiry_est": _ts(refresh.get("exp")),
        "access_expires_at": _ts(access.get("exp")),
        "refresh_expires_at": _ts(refresh.get("exp")),
    }


def _decode_garth(blob: str) -> dict:
    oauth1, oauth2 = json.loads(base64.b64decode(blob))
    claims = _jwt_claims(oauth2.get("access_token") or "")
    issued = _ts(claims.get("iat"))
    return {
        "kind": "garth",
        "domain": oauth1.get("domain"),
        "session_issued": issued,
        "session_expiry_est": (
            issued + dt.timedelta(days=OAUTH1_LIFETIME_DAYS) if issued else None
        ),
        "access_expires_at": _ts(oauth2.get("expires_at")),
        "refresh_expires_at": _ts(oauth2.get("refresh_token_expires_at")),
    }


def decode_token_info(token_blob: str) -> dict:
    """Expiry facts from a stored session blob, whichever engine wrote it.

    Returns ``kind`` (``gconn``/``garth``), ``session_issued`` /
    ``session_expiry_est`` (the re-login deadline, ``None`` when unknowable),
    ``access_expires_at`` / ``refresh_expires_at`` and ``domain``. Raises
    ``ValueError`` on a blob that is neither format.
    """
    try:
        data = json.loads(token_blob)
    except Exception:
        data = None
    if isinstance(data, dict) and any(
        data.get(k) for k in ("di_token", "di_refresh_token", "di_client_id")
    ):
        return _decode_gconn(data)
    try:
        return _decode_garth(token_blob)
    except Exception as exc:
        raise ValueError(f"not a Garmin token blob: {exc}") from exc
