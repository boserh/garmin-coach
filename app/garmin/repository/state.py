"""BotState key/value + the DB-backed pending-plan-edit state (EP-11). Split out of
the flat ``repository.py`` (B1)."""
import json
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotState,
)

# ---------- BOT STATE ----------

async def get_state(session: AsyncSession, user_id: int, key: str) -> Optional[str]:
    m = await session.get(BotState, (user_id, key))
    return m.value if m else None


async def set_state(session: AsyncSession, user_id: int, key: str, value: str) -> None:
    m = await session.get(BotState, (user_id, key))
    if m:
        m.value = value
    else:
        session.add(BotState(user_id=user_id, key=key, value=value))
    await session.commit()


# EP-11: the free-text "/plan <text>" edit and "/sick" proposals used to stash their
# confirm-button ops in ``context.user_data["pending_plan"]`` — in-memory, per-process,
# and Telegram-only, so a web chat turn could never see or confirm them (and a bot
# restart silently dropped an unanswered proposal). This mirrors the DB-backed pattern
# EP-02's adaptation proposals already use (``PENDING_ADAPT_KEY`` in bot/jobs.py) via the
# same ``bot_state`` key/value store, just under its own key so an in-flight free-text
# edit never collides with an outstanding adapt/weather/deload proposal. Single-use: a
# pop clears it, so a stale button (already answered, or superseded by a newer proposal)
# reads back nothing instead of re-applying an old edit.
PENDING_PLAN_EDIT_KEY = "pending_plan_edit"


async def set_pending_plan_edit(
    session: AsyncSession, user_id: int, ops: list, alt: Optional[list] = None,
    *, summary: Optional[str] = None, alt_summary: Optional[str] = None,
    risky: bool = False,
) -> None:
    """``summary``/``alt_summary``/``risky`` are display-only extras (EP-11's web chat
    re-renders the proposal text across page loads, unlike a Telegram message which
    already has the text baked in) — the bot's confirm flow ignores them, reading only
    ``ops``/``alt``, so old and new writers stay compatible either direction."""
    await set_state(
        session, user_id, PENDING_PLAN_EDIT_KEY,
        json.dumps({"ops": ops, "alt": alt or [], "summary": summary,
                    "alt_summary": alt_summary, "risky": bool(risky)}, ensure_ascii=False),
    )


async def get_pending_plan_edit(session: AsyncSession, user_id: int) -> Optional[dict]:
    """Peek at this user's pending free-text plan edit without clearing it (for
    re-rendering the confirm banner on every page load, e.g. the web chat's GET)."""
    raw = await get_state(session, user_id, PENDING_PLAN_EDIT_KEY)
    return json.loads(raw) if raw else None


async def pop_pending_plan_edit(session: AsyncSession, user_id: int) -> Optional[dict]:
    """Read this user's pending free-text plan edit and clear it (single-use)."""
    raw = await get_state(session, user_id, PENDING_PLAN_EDIT_KEY)
    if not raw:
        return None
    await set_state(session, user_id, PENDING_PLAN_EDIT_KEY, "")
    return json.loads(raw)
