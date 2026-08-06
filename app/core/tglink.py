"""Telegram account linking — a signed, expiring token instead of a copied chat id.

Connecting the bot used to be the worst step of setup: the user had to find
``@userinfobot``, message it, copy a numeric id out of the reply and paste it into a
form field. Three apps, one number typed by hand, and nothing tells you if you got it
wrong — the bot just keeps answering "тебе не зареєстровано".

Instead the web hands out a ``https://t.me/<bot>?start=<token>`` deep link. Telegram
delivers the token back to us as ``/start <token>``, so the incoming chat id and the web
account arrive in the same update and the bot links them itself.

The token is an ``itsdangerous`` signed blob carrying the user id — no table, no
migration, no cleanup job, and no state to get out of sync between the web and the bot
processes. What they *do* have to share is ``APP_SECRET_KEY`` (they already do: same
``.env``); without it the signature can't be verified across processes, so
:func:`available` reports False and the UI falls back to the manual chat-id field.
"""
from __future__ import annotations

from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import settings

# Distinct salt so a link token can never be swapped in for a session cookie (or the
# other way round) even though both are signed with APP_SECRET_KEY.
SALT = "telegram-link"

# Long enough to walk away mid-setup and come back, short enough that a link pasted into
# a group chat or leaked in a screenshot stops working. Re-opening /onboarding mints a
# fresh one, so expiry costs the user one page reload.
TOKEN_TTL_S = 24 * 3600


def available() -> bool:
    """Whether the one-click link can be offered at all: we need a shared signing key
    (bot and web must agree on it) and the bot's public @username to build the URL."""
    return bool(settings.APP_SECRET_KEY and settings.TELEGRAM_BOT_USERNAME)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.APP_SECRET_KEY, salt=SALT)


def make_token(user_id: int) -> str:
    return _serializer().dumps(user_id)


def parse_token(token: str, *, max_age: int = TOKEN_TTL_S) -> Optional[int]:
    """The user id inside a link token, or None if it's forged, corrupt or expired.

    Callers can't distinguish those cases on purpose — every failure gets the same
    "open the connect page again" reply, which is the only useful next step regardless.
    """
    if not token or not settings.APP_SECRET_KEY:
        return None
    try:
        user_id = _serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    return user_id if isinstance(user_id, int) else None


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
