"""``propose_move`` — a tool of the coach MCP server (:mod:`app.mcp_server`): move ONE
already planned session to another date, proposed to the athlete's own Telegram chat for
a ✅/❌ tap, never applied directly.

It used to be a server of its own (own process, origin, OAuth scope and
``MCP_PLAN_PUBLIC_URL``). That cost a tunnel hostname, a systemd unit and a second
connector for one tool, so it now rides on the connector the athlete already has. What
keeps it safe is unchanged and lives in the tool itself, not in a scope:

* Nothing is applied here. The proposal goes through the EXACT SAME confirm/reject flow
  EP-02's adaptive-plan proposals use (``bot.jobs._send_adapt_proposal`` + the existing
  ``adapt_callback``), so the plan changes only on the athlete's tap, in the one place
  ``apply_plan_ops`` already is.
* Deliberately NOT a general plan-edit tool: no free text, no add/modify/skip, no LLM call
  on our side (the calling Claude session already decided what to move). Only a
  `date` + `to_date` pair, validated mechanically against the stored plan — "modify"/
  "skip"/"add" need judgement about volume and context (see ``SYSTEM_PLAN_EDIT``) that a
  pair of dates cannot express safely.
* Rate limited per account (``MCP_PLAN_RATE_LIMIT`` per ``MCP_PLAN_RATE_WINDOW_S``), and
  it only ever writes to the caller's own chat.
"""
from __future__ import annotations

import datetime as dt
import logging
from types import SimpleNamespace

from mcp.server.auth.middleware.auth_context import get_access_token
from telegram import Bot

from app.core.config import settings
from app.core.ratelimit import RateLimiter
from app.db import users
from app.db.base import async_session_maker

logger = logging.getLogger("mcp.plan")

_limiter = RateLimiter(settings.MCP_PLAN_RATE_LIMIT, settings.MCP_PLAN_RATE_WINDOW_S)


def _current_user_id() -> int:
    """The token's user over http; over stdio, the coach process's bound --email user."""
    token = get_access_token()
    if token is not None and token.subject:
        return int(token.subject)
    from app.mcp_server import _current_user_id as coach_user_id

    return coach_user_id()


async def propose_move(date: str, to_date: str) -> dict:
    """Propose moving ONE already-planned session from `date` to `to_date` (both ISO
    yyyy-mm-dd). Sends a ✅/❌ confirmation to the athlete's own Telegram chat — nothing
    is changed on the plan until they tap it. Use `date`/`to_date` exactly as they appear
    in `get_training_plan` (the read-only coach connector, if also connected) or as the
    athlete stated them; both must be real calendar dates, `to_date` today or later.

    Fails with a clear message (nothing sent) when: there is no active plan, no session
    is planned on `date` (already done/skipped/missed sessions can't be moved — only
    "planned" ones), `to_date` is unparsable or in the past, or the athlete has no
    Telegram chat linked yet. Returns {"proposed": true, "date", "to_date"} on success —
    this does NOT mean the move was applied, only that the proposal was sent."""
    from app.garmin import repository

    user_id = _current_user_id()
    if not settings.TELEGRAM_BOT_TOKEN:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is not set on the server — nowhere to send the proposal."
        )
    if not _limiter.allow(str(user_id)):
        raise ValueError(
            f"Rate limit reached ({settings.MCP_PLAN_RATE_LIMIT} proposals per "
            f"{settings.MCP_PLAN_RATE_WINDOW_S}s). Nothing was sent."
        )
    if date == to_date:
        raise ValueError("to_date is the same as date — nothing to move.")
    try:
        target = dt.date.fromisoformat(to_date)
    except ValueError:
        raise ValueError("to_date must be an ISO date, yyyy-mm-dd.") from None
    if target < dt.date.today():
        raise ValueError("to_date is in the past — cannot move a session there.")

    from app.garmin.schemas import PlanOp
    from bot.jobs import _send_adapt_proposal

    async with async_session_maker() as session:
        user = await users.get_by_id(session, user_id)
        if user is None or not user.is_active or not user.is_approved:
            raise ValueError("Account is not active.")
        if not user.telegram_chat_id:
            raise ValueError(
                "No Telegram chat linked to this account yet — nowhere to send the "
                "proposal. Link one in /settings first."
            )
        plan = await repository.get_active_plan(session, user_id)
        if plan is None:
            raise ValueError("No active training plan for this account.")
        workout = await repository.workout_on_date(session, plan.id, date)
        if workout is None or workout.status != "planned":
            raise ValueError(
                f"No PLANNED session on {date} (it must exist and still be upcoming — "
                "already done/skipped/missed sessions can't be moved)."
            )
        edit = SimpleNamespace(
            operations=[PlanOp(action="move", date=date, to_date=to_date)],
            risky=False, alt_operations=None, alt_summary=None,
            summary=f"Перенести {workout.type or 'тренування'} {date} → {to_date}.",
        )
        async with Bot(token=settings.TELEGRAM_BOT_TOKEN) as bot:
            await _send_adapt_proposal(
                SimpleNamespace(bot=bot), session, user, plan.id, edit)
    logger.info(f"PLAN MCP: user={user_id} proposed move {date} -> {to_date}")
    return {"proposed": True, "date": date, "to_date": to_date}


TOOLS = (propose_move,)
