"""BotState key/value + the DB-backed pending-plan-edit state (EP-11). Split out of
the flat ``repository.py`` (B1)."""
import json
import time
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotState,
)

# ---------- BOT STATE ----------

# Shared between bot/jobs.py (sets it, once, when a Garmin login fails with bad
# creds) and app/routers/settings.py (clears it when the user saves a changed
# Garmin email/password) — a neutral home so neither imports the other.
GARMIN_AUTH_INVALID_NOTIFIED_KEY = "garmin_auth_invalid_notified"

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


# ST-23: how many follow-up turns about one proposal ride along as context. A dialogue
# about a single edit is short by nature; the cap keeps the stored blob (and the prompt
# it feeds) bounded no matter how long the user keeps poking at one proposal.
PENDING_THREAD_MAX = 6


async def set_pending_plan_edit(
    session: AsyncSession, user_id: int, ops: list, alt: Optional[list] = None,
    *, summary: Optional[str] = None, alt_summary: Optional[str] = None,
    risky: bool = False, instruction: Optional[str] = None,
    thread: Optional[list] = None, message: Optional[dict] = None,
) -> None:
    """``summary``/``alt_summary``/``risky`` are display-only extras (EP-11's web chat
    re-renders the proposal text across page loads, unlike a Telegram message which
    already has the text baked in) — the bot's confirm flow ignores them, reading only
    ``ops``/``alt``, so old and new writers stay compatible either direction.

    ST-23 adds the dialogue extras, all equally optional: ``instruction`` (the request
    this proposal came from), ``thread`` (the follow-up Q/A so far, trimmed to
    ``PENDING_THREAD_MAX``) and ``message`` (``{chat_id, message_id}`` of the Telegram
    message carrying the confirm buttons, so a superseding proposal can retire the old
    one's keyboard instead of leaving two live button sets in the chat)."""
    await set_state(
        session, user_id, PENDING_PLAN_EDIT_KEY,
        json.dumps({"ops": ops, "alt": alt or [], "summary": summary,
                    "alt_summary": alt_summary, "risky": bool(risky),
                    "instruction": instruction,
                    "thread": (thread or [])[-PENDING_THREAD_MAX:],
                    "message": message}, ensure_ascii=False),
    )


def append_thread(pending: Optional[dict], question: str, answer: Optional[str]) -> list:
    """The follow-up thread of ``pending`` with this turn appended (ST-23), trimmed to
    ``PENDING_THREAD_MAX``. Pure — the caller passes the result back to
    ``set_pending_plan_edit(thread=...)``."""
    thread = list((pending or {}).get("thread") or [])
    thread.append({"q": (question or "")[:300], "a": (answer or "")[:500]})
    return thread[-PENDING_THREAD_MAX:]


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


# ---------- OPS-09: last Garmin calendar sync summary ----------
# One row per plan (``plan_sync_last:<plan_id>``) so an archived plan keeps its own
# last-known sync result independent of whatever plan is active now — the read-only
# archive view (``GET /plan/{id}``) can show it too, not just the live ``/plan`` page.

def _plan_sync_key(plan_id: int) -> str:
    return f"plan_sync_last:{plan_id}"


async def set_plan_sync_summary(
    session: AsyncSession, user_id: int, plan_id: int, pushed: int, removed: int,
    errors: list,
) -> None:
    await set_state(
        session, user_id, _plan_sync_key(plan_id),
        json.dumps({"ts": time.time(), "pushed": pushed, "removed": removed,
                    "errors": errors}, ensure_ascii=False),
    )


async def get_plan_sync_summary(
    session: AsyncSession, user_id: int, plan_id: int
) -> Optional[dict]:
    raw = await get_state(session, user_id, _plan_sync_key(plan_id))
    return json.loads(raw) if raw else None
