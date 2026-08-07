"""Telegram account linking — a signed, expiring token instead of a copied chat id.

Connecting the bot used to be the worst step of setup: the user had to find
``@userinfobot``, message it, copy a numeric id out of the reply and paste it into a
form field. Three apps, one number typed by hand, and nothing tells you if you got it
wrong — the bot just keeps answering "тебе не зареєстровано".

Instead the web hands out a ``https://t.me/<bot>?start=<token>`` deep link. Telegram
delivers the token back to us as ``/start <token>``, so the incoming chat id and the web
account arrive in the same update and the bot links them itself.

The token is a signed blob carrying the user id — no table, no migration, no cleanup
job, and no state to get out of sync between the web and the bot processes. What they
*do* have to share is ``APP_SECRET_KEY`` (they already do: same ``.env``); without it the
signature can't be verified across processes, so :func:`available` reports False and the
UI falls back to the manual chat-id field.

It is signed here by hand rather than with ``itsdangerous`` because Telegram constrains
a ``?start=`` payload to 64 characters of ``A-Z a-z 0-9 _ -``, and
``URLSafeTimedSerializer`` joins its parts with dots — url-safe in general, but outside
the set Telegram documents. Changing the serializer's separator doesn't help either: it
un-signs by splitting from the right, and its own base64url alphabet already contains
both remaining candidates. So: fixed-width fields, one HMAC, no separator at all.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from typing import Optional

from app.core.config import settings

# Distinct salt so a link token can never be swapped in for a session cookie (or the
# other way round) even though both are signed with APP_SECRET_KEY.
SALT = "telegram-link"

# Long enough to walk away mid-setup and come back, short enough that a link pasted into
# a group chat or leaked in a screenshot stops working. Re-opening /onboarding mints a
# fresh one, so expiry costs the user one page reload.
TOKEN_TTL_S = 24 * 3600

# Telegram's own limit on a ?start= payload: at most 64 characters, and only from
# A-Z a-z 0-9 _ - . That is the whole reason this module signs by hand instead of
# handing the job to itsdangerous, whose URLSafeTimedSerializer joins its three parts
# with DOTS ("MQ.anZMaQ._Epkc…") — url-safe in general, outside Telegram's set here.
START_PARAM_MAX = 64

# 8 bytes of body + a 128-bit truncated HMAC = 24 bytes = exactly 32 base64url
# characters, no padding and no separator to get the parsing wrong.
_SIG_BYTES = 16
_BODY = ">II"          # user id, issued-at — both unsigned 32-bit, big-endian


def available() -> bool:
    """Whether the one-click link can be offered at all: we need a shared signing key
    (bot and web must agree on it) and the bot's public @username to build the URL."""
    return bool(settings.APP_SECRET_KEY and settings.TELEGRAM_BOT_USERNAME)


def _sign(body: bytes) -> bytes:
    """HMAC-SHA256 over the salt + body, truncated. The salt is inside the MAC, so a
    token minted for another purpose with the same key cannot verify here."""
    return hmac.new(
        settings.APP_SECRET_KEY.encode(), SALT.encode() + body, hashlib.sha256
    ).digest()[:_SIG_BYTES]


def make_token(user_id: int) -> str:
    body = struct.pack(_BODY, user_id, int(time.time()))
    return base64.urlsafe_b64encode(body + _sign(body)).decode().rstrip("=")


def parse_token(token: str, *, max_age: int = TOKEN_TTL_S) -> Optional[int]:
    """The user id inside a link token, or None if it's forged, corrupt or expired.

    Callers can't distinguish those cases on purpose — every failure gets the same
    "open the connect page again" reply, which is the only useful next step regardless.
    """
    if not token or not settings.APP_SECRET_KEY:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except Exception:
        return None
    size = struct.calcsize(_BODY)
    if len(raw) != size + _SIG_BYTES:
        return None
    body, sig = raw[:size], raw[size:]
    # compare_digest, not ==: a timing-variable comparison of a MAC is the textbook way
    # to let someone forge one byte at a time.
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    user_id, issued_at = struct.unpack(_BODY, body)
    if max_age is not None and time.time() - issued_at > max_age:
        return None
    return user_id


def deep_link(user_id: int) -> Optional[str]:
    """The ``t.me`` URL that links this account on tap, or None when unavailable."""
    if not available():
        return None
    return f"https://t.me/{settings.TELEGRAM_BOT_USERNAME}?start={make_token(user_id)}"


def bot_link() -> Optional[str]:
    """Plain link to the bot, no token — for "which bot is it?" when linking is done."""
    if not settings.TELEGRAM_BOT_USERNAME:
        return None
    return f"https://t.me/{settings.TELEGRAM_BOT_USERNAME}"
