"""Read-only introspection of a stored garth session token (OPS-01).

A garth ``Client.dumps()`` blob is base64 of ``[oauth1_dict, oauth2_dict]``.
The OAuth1 token carries no timestamps, but we only ever persist the blob right
after a fresh email+password login (``runtime.user_runtime`` → ``new_token``),
so the OAuth2 access token's JWT ``iat`` equals the OAuth1 issue time. Garmin
OAuth1 tokens live ~1 year from issue — that estimate is each user's
"auth death date" and the deadline for the OPS-01 plan-B migration.

Pure decoding, no network and no writes.
"""
import base64
import datetime as dt
import json
from typing import Optional

OAUTH1_LIFETIME_DAYS = 365  # empirical: Garmin OAuth1 tokens live ~1 year


def _jwt_claims(jwt: str) -> dict:
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


def decode_token_info(token_b64: str) -> dict:
    """Expiry facts from a stored garth token blob.

    Returns ``oauth1_issued`` / ``oauth1_expiry_est`` (datetimes, from the OAuth2
    JWT ``iat`` — see module docstring), ``oauth2_expires_at`` /
    ``oauth2_refresh_expires_at``, and ``domain``. Raises ``ValueError`` on a
    blob that isn't a garth token.
    """
    try:
        oauth1, oauth2 = json.loads(base64.b64decode(token_b64))
    except Exception as exc:
        raise ValueError(f"not a garth token blob: {exc}") from exc

    claims = _jwt_claims(oauth2.get("access_token") or "")
    issued = _ts(claims.get("iat"))
    return {
        "domain": oauth1.get("domain"),
        "oauth1_issued": issued,
        "oauth1_expiry_est": issued + dt.timedelta(days=OAUTH1_LIFETIME_DAYS) if issued else None,
        "oauth2_expires_at": _ts(oauth2.get("expires_at")),
        "oauth2_refresh_expires_at": _ts(oauth2.get("refresh_token_expires_at")),
    }
