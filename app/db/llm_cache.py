"""Cross-process Claude dedup cache over the ``llm_cache`` table (PERF-02).

Replaces the per-process ``claude_cache.json``: both the bot and the web app now
see the same entries, so a report generated in one process is a `CLAUDE CACHE HIT`
in the other. Keys and TTL semantics are unchanged — the sha256 keys still come
from ``analysis.service._cache_key`` and siblings; expired rows are purged lazily
on write. Both helpers are best-effort: a cache failure must never break the
analysis call it fronts (a failed read is a miss, a failed write is a warning).
"""
import logging
import time

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LlmCache

logger = logging.getLogger("claude")


async def get(session: AsyncSession, key: str):
    """The cached text for ``key``, or None (missing, expired, or read failure)."""
    try:
        row = await session.get(LlmCache, key)
    except Exception:
        logger.warning("LLM cache read failed", exc_info=True)
        return None
    if row is None or row.expires_at <= time.time():
        return None
    return row.value


async def put(session: AsyncSession, key: str, value: str, ttl_s: float) -> None:
    """Upsert ``key`` with a fresh TTL and lazily purge expired rows. Puts are rare
    (one per paid Claude call), so the DELETE piggybacking here is cheap."""
    now = time.time()
    try:
        await session.merge(LlmCache(key=key, value=value, expires_at=now + ttl_s))
        await session.execute(delete(LlmCache).where(LlmCache.expires_at <= now))
        await session.commit()
    except Exception:
        logger.warning("LLM cache write failed", exc_info=True)
        try:
            await session.rollback()  # keep the session usable for the ReportLog write
        except Exception:
            pass
