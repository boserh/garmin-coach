"""Per-user runtime context: bind a user's Garmin provider for the duration of a
request/command and persist a freshly minted garth session token.

Usage::

    async with user_runtime(session, user) as creds:
        payload, _ = await service.build_payload_cached(session, days=7)
        text = await run_analysis(session, payload, api_key=creds.anthropic_key)

Inside the block, ``service``/``client`` resolve the user's provider through the
provider ContextVar; ``creds.anthropic_key`` is passed to the analysis layer.
"""
import logging
from contextlib import asynccontextmanager

from app.core.crypto import encrypt
from app.core.impersonate import IMPERSONATING, ImpersonationUnavailable
from app.db.models import User
from app.garmin import providers
from app.garmin.credentials import load_credentials

logger = logging.getLogger("garmin")


class DemoModeUnavailable(Exception):
    """Raised instead of touching Garmin for the demo account (``User.is_demo``, see
    ``app.core.demo``) — its routes are expected to guard earlier and never reach here;
    this is the last-resort net, not the normal path."""


@asynccontextmanager
async def user_runtime(session, user: User):
    if user.is_demo:
        raise DemoModeUnavailable(user.id)
    if IMPERSONATING.get():
        # An admin is looking at this account through a borrowed session (see
        # app.core.impersonate). Support gets to read what's stored; it does not get to
        # spend the user's Garmin rate limit or mint a session token in their name.
        raise ImpersonationUnavailable(user.id)
    if user.garmin_creds_invalid:
        # A previous login already failed with a bad-credentials error — don't touch
        # Garmin again (repeatedly retrying a known-bad password risks a Cloudflare
        # block) until the user re-saves working creds in /settings, which clears
        # this flag. Same exception type as a fresh failure below, so every caller
        # handles both the same way.
        raise providers.GarminAuthFailed(user.id)
    creds = load_credentials(user)
    provider = providers.build_user_provider(creds)
    token = providers.set_current_provider(provider)
    try:
        # A login that hits Garmin's MFA gate raises MFARequired (app.garmin.mfa) —
        # deliberately not caught here, so routers/bot handlers can react to it.
        yield creds
    except providers.GarminAuthFailed:
        user.garmin_creds_invalid = True
        await session.commit()
        raise
    finally:
        providers.reset_current_provider(token)
        # A fresh login produced a new session token — store it (encrypted) so the
        # next run resumes instead of logging in again.
        if provider.new_token and provider.new_token != creds.garth_token:
            user.garth_token_enc = encrypt(provider.new_token)
            await session.commit()
            logger.info(f"GARTH token saved for user {user.id}")
