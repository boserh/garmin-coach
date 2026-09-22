"""The plan-move MCP server: one narrow write tool — move ONE already planned session to
another date, proposed to the athlete's own Telegram chat for a ✅/❌ tap, never applied
directly.

Why this is its own service rather than one more tool on :mod:`app.mcp_server` or
:mod:`app.mcp_notify`, same reasoning as the notify/coach split in ``app.mcp_oauth``:

    coach : app.mcp_server · MCP_PUBLIC_URL      · scope garmin:read  · reads, cannot write
    notify: app.mcp_notify · MCP_NOTIFY_PUBLIC_URL· scope notify:write· sends, cannot read
    plan  : app.mcp_plan   · MCP_PLAN_PUBLIC_URL  · scope plan:propose· proposes ONE move

A coach-scoped token can never reach this endpoint even though both sit on the same OAuth
storage — the SDK refuses a token whose scope doesn't match what the endpoint requires.

Deliberately NOT a general plan-edit tool: no free text, no add/modify/skip, no LLM call
on our side (the calling Claude session already decided what to move — that is the whole
point of doing this over MCP instead of through ``/plan <text>``). The tool only accepts
a `date` + `to_date` pair, validates them mechanically against the stored plan, and hands
the result to the EXACT SAME confirm/reject flow EP-02's adaptive-plan proposals already
use (``bot.jobs._send_adapt_proposal`` + the existing ``adapt_callback``) — so nothing new
gets applied outside the one place ``apply_plan_ops`` already is. This is also why the
scope grants no "modify"/"skip"/"add": those need real judgement about volume, breaks and
context (see ``SYSTEM_PLAN_EDIT``) that a bare pair of dates cannot express safely.

Run (needs the ``mcp`` extra, ``pip install -e ".[mcp]"``)::

    ./venv/bin/python -m app.mcp_plan --email me@example.com                # stdio
    ./venv/bin/python -m app.mcp_plan --transport http --port 8790          # + MCP_PLAN_PUBLIC_URL

Over stdio (as with ``app.mcp_server``) there is no request to authenticate: ``--email``
binds the process to one user for its whole lifetime. Over http, identity is per-request
from the OAuth token's subject — the same account whose Telegram chat receives the
proposal, never anyone else's.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
from types import SimpleNamespace
from typing import Optional

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from telegram import Bot

from app.core.config import settings
from app.core.logging import setup as setup_logging
from app.core.ratelimit import RateLimiter
from app.db import users
from app.db.base import async_session_maker, init_db

logger = logging.getLogger("mcp.plan")

# Bound once at startup in stdio mode, and never set in http mode — mirrors
# app.mcp_server._user_id; see its docstring for why the fallback there is unreachable
# rather than merely unlikely over http.
_user_id: Optional[int] = None

_limiter = RateLimiter(settings.MCP_PLAN_RATE_LIMIT, settings.MCP_PLAN_RATE_WINDOW_S)


def _current_user_id() -> int:
    token = get_access_token()
    if token is not None and token.subject:
        return int(token.subject)
    if _user_id is not None:
        return _user_id
    raise RuntimeError("MCP call with no authenticated user and no bound --email user")


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


_TOOLS = (propose_move,)


def build_server(*, public_url: Optional[str] = None) -> MCPServer:
    """The MCP server, with OAuth wired up when ``public_url`` is given.

    Auth is configured per transport rather than always-on: over stdio there is no HTTP
    request to carry a token, and the SDK refuses auth settings without one.
    """
    from app.mcp_http import auth_kwargs, register_consent
    from app.mcp_oauth import PLAN_SCOPE

    kwargs = auth_kwargs(public_url, PLAN_SCOPE) if public_url else {}
    server = MCPServer("bihun-plan", **kwargs)
    for fn in _TOOLS:
        server.tool()(fn)
    if public_url:
        register_consent(server)
    return server


async def _resolve_user_id(email: str) -> int:
    await init_db()
    async with async_session_maker() as session:
        user = await users.get_by_email(session, email)
        if user is None:
            raise SystemExit(
                f"No user with email {email!r}. Create one first: "
                "./venv/bin/python -m app.cli create-user --email ..."
            )
        return user.id


def main(argv=None) -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Plan-move MCP server — one write tool, proposes a session move "
                     "for Telegram confirmation (NF-08 sibling, no LLM call)."
    )
    parser.add_argument(
        "--transport", choices=("stdio", "http"), default="stdio",
        help="stdio: a local child process bound to one --email user (default). "
             "http: a public endpoint, per-request OAuth identity.",
    )
    parser.add_argument("--email", help="stdio only: which user's plan this server proposes for")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="http only: bind address. Keep the default and put a reverse proxy or "
             "tunnel in front — binding to 0.0.0.0 publishes it to the whole network.",
    )
    parser.add_argument("--port", type=int, default=8790, help="http only: bind port")
    args = parser.parse_args(argv)

    if not settings.TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN must be set: this server has nowhere to send a proposal "
            "without it."
        )

    if args.transport == "stdio":
        if not args.email:
            raise SystemExit("--email is required for --transport stdio")
        global _user_id
        _user_id = asyncio.run(_resolve_user_id(args.email))
        logger.info(f"MCP plan server bound to user_id={_user_id} ({args.email})")
        build_server().run()  # stdio transport; blocks until the client disconnects
        return

    from app.mcp_http import http_app, require_public_url

    public = require_public_url(settings.MCP_PLAN_PUBLIC_URL, var="MCP_PLAN_PUBLIC_URL")
    import uvicorn

    asyncio.run(init_db())
    logger.info(f"MCP plan server (http) on {args.host}:{args.port}, issuer {public}")
    uvicorn.run(http_app(build_server(public_url=public), public),
                host=args.host, port=args.port)


if __name__ == "__main__":
    main()
