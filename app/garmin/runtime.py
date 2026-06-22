"""Per-user runtime context: bind a user's Garmin provider for the duration of a
request/command and persist a freshly minted garth session token.

Usage::

    async with user_runtime(session, user) as creds:
        payload = await service.build_payload_cached(session, days=7)
        text = await run_analysis(session, payload, api_key=creds.anthropic_key)

Inside the block, ``service``/``client`` resolve the user's provider through the
provider ContextVar; ``creds.anthropic_key`` is passed to the analysis layer.
"""
import logging
from contextlib import asynccontextmanager

from app.core.crypto import encrypt
from app.db.models import User
from app.garmin import providers
from app.garmin.credentials import load_credentials

logger = logging.getLogger("garmin")


@asynccontextmanager
async def user_runtime(session, user: User):
    creds = load_credentials(user)
    provider = providers.build_user_provider(creds)
    token = providers.set_current_provider(provider)
    try:
        yield creds
    finally:
        providers.reset_current_provider(token)
        # A fresh login produced a new session token — store it (encrypted) so the
        # next run resumes instead of logging in again.
        if provider.new_token and provider.new_token != creds.garth_token:
            user.garth_token_enc = encrypt(provider.new_token)
            await session.commit()
            logger.info(f"GARTH token saved for user {user.id}")
