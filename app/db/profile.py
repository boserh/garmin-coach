"""EP-18 · encrypted storage for the athlete profile (``athlete_profiles``).

The facts are Fernet-encrypted at rest, with the same key as the stored credentials. That's
not ceremony: this is the most sensitive free text in the database — injuries, work stress,
habits, travel — and treating it as "just a cache" would be the wrong call the first time a
disk or a backup leaves the Pi. (An OPS-02 DB copy is safe to store anywhere precisely
because everything sensitive in it is Fernet-encrypted; the profile must not be the one
exception that breaks that property.)

Reads degrade to an empty profile when ``APP_SECRET_KEY`` is unset or the blob won't
decrypt — a keyless install still gets its morning report, just with an amnesiac coach.
Writes do NOT degrade: silently storing the coach's memory in plaintext would be a worse
outcome than a loud failure on a path that only ever runs deliberately.
"""
import json
import logging
from typing import List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt, encrypt
from app.db.models import AthleteProfile

logger = logging.getLogger("api")


def _load(blob: Optional[str], default):
    if not blob:
        return default
    try:
        return json.loads(decrypt(blob))
    except Exception:  # noqa: BLE001 — a missing key or a rotated one must not break a report
        logger.warning("PROFILE: could not decrypt stored profile — treating as empty")
        return default


async def get_row(session: AsyncSession, user_id: int) -> Optional[AthleteProfile]:
    return (await session.execute(
        select(AthleteProfile).where(AthleteProfile.user_id == user_id)
    )).scalar_one_or_none()


async def get_profile(session: AsyncSession, user_id: int) -> Tuple[List[dict], List[str]]:
    """``(facts, stoplist)`` for this user — ``([], [])`` when there is no profile yet.
    User-scoped by construction: there is no call shape that can read another user's row."""
    row = await get_row(session, user_id)
    if row is None:
        return [], []
    return _load(row.facts_enc, []), _load(row.stoplist_enc, [])


async def save_profile(session: AsyncSession, user_id: int, facts: List[dict],
                       stoplist: Optional[List[str]] = None) -> None:
    """Encrypt and persist (does not commit). Raises if ``APP_SECRET_KEY`` is unset — see
    the module docstring for why this one doesn't degrade quietly."""
    facts_enc = encrypt(json.dumps(facts, ensure_ascii=False))
    stop_enc = encrypt(json.dumps(stoplist or [], ensure_ascii=False))
    row = await get_row(session, user_id)
    if row is None:
        session.add(AthleteProfile(
            user_id=user_id, facts_enc=facts_enc, stoplist_enc=stop_enc))
    else:
        row.facts_enc = facts_enc
        row.stoplist_enc = stop_enc


async def build_context(session: AsyncSession, user_id: Optional[int]) -> Optional[dict]:
    """The prompt block for this user, or ``None`` — the single helper every LLM path calls,
    so "what the coach remembers" is defined in exactly one place and can't drift between
    the daily report, /ask and plan adaptation."""
    from app import profile as profile_rules

    if user_id is None:
        return None
    facts, _ = await get_profile(session, user_id)
    return profile_rules.to_context(facts)
