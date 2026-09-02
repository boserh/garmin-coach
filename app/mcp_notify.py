"""The monitoring MCP server: one write-only tool that pushes a message to Telegram.

Why it is a *separate service* from :mod:`app.mcp_server` rather than one more tool
there. NF-08's server is read-only by design — its own ticket names scope creep as the
main risk, and the consent screen makes the read-only promise to the user's face.
Bolting a "send a message" tool onto it would break that promise and, worse, hand every
monitoring client the athlete's whole health history as context it has no business
seeing. So the split is along the only line that matters here: **what a token can do.**

    coach   : app.mcp_server  · MCP_PUBLIC_URL        · scope garmin:read  · reads, cannot send
    monitor : app.mcp_notify  · MCP_NOTIFY_PUBLIC_URL · scope notify:write · sends, cannot read

They are two processes on two origins, and the SDK refuses a token whose scope doesn't
match the endpoint it is presented at — so neither can be used to do the other's job,
even though both sit on the same OAuth storage. The shared transport plumbing lives in
:mod:`app.mcp_http`; the delivery half in :mod:`app.notify`.

The intended shape of use: a scheduled Claude task does the morning monitoring work of
its own (war-threshold watching — nothing in this repo knows or cares what the text is
about) and calls ``send_message`` once when it has something to say. This server does no
analysis, spends nothing on Claude, touches neither Garmin nor the athlete's data.

Two guards on top of the OAuth boundary, both because the destination chat is
deployment-global rather than per-account:

1. **Admin only.** The monitoring channel belongs to the install. On a multi-user
   deployment an ordinary approved account must not be able to write into the owner's
   channel, and a per-account destination is not what this is (see settings).
2. **Rate limited** (``MCP_NOTIFY_RATE_LIMIT`` per ``MCP_NOTIFY_RATE_WINDOW_S``). A
   client stuck in a loop at 06:00 would otherwise take the channel — and Telegram's own
   flood limits — down with it.

Run (needs the ``mcp`` extra, ``pip install -e ".[mcp]"``)::

    ./venv/bin/python -m app.mcp_notify                       # stdio, for local testing
    ./venv/bin/python -m app.mcp_notify --transport http --port 8789   # + MCP_NOTIFY_PUBLIC_URL

Over stdio there is no request to authenticate and no ``--email`` to bind: the tool
sends to the configured chat and to nowhere else, which is the same thing it does over
http. The admin check applies only where there *is* an identity to check (http) — a
local child process is already running as whoever launched it.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Optional

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer

from app.core.config import settings
from app.core.logging import setup as setup_logging
from app.core.ratelimit import RateLimiter
from app.db import users
from app.db.base import async_session_maker, init_db
from app.notify import NotifyError, send_monitor_message

logger = logging.getLogger("mcp.notify")

_limiter = RateLimiter(
    settings.MCP_NOTIFY_RATE_LIMIT, settings.MCP_NOTIFY_RATE_WINDOW_S
)


async def _authorize() -> str:
    """Who this call speaks for, refusing anyone who may not use the channel.

    Returns a short label for the log. Over http the SDK has already verified the token
    and its scope; what it cannot know is that this particular endpoint is admin-only.
    Over stdio there is no token — the process is a child of whoever launched it.
    """
    token = get_access_token()
    if token is None or not token.subject:
        return "stdio"

    user_id = int(token.subject)
    async with async_session_maker() as session:
        user = await users.get_by_id(session, user_id)
    # is_active/is_approved are re-checked by load_access_token on every request; what is
    # left to check here is the one rule that belongs to this endpoint alone.
    if user is None or not user.is_admin:
        logger.warning("NOTIFY: refused non-admin user_id=%s", user_id)
        raise PermissionError(
            "This monitoring channel is restricted to the deployment's admin account."
        )
    return f"user {user_id}"


async def send_message(
    text: str, parse_mode: Optional[str] = None, silent: bool = False
) -> dict:
    """Send a message to this deployment's Telegram monitoring channel.

    Use it to deliver a finished briefing or an alert — the text arrives verbatim, so
    write it for a human reading a phone notification, not as JSON. Long text is split
    across several Telegram messages automatically (hard limit 20000 characters).

    `parse_mode` is optional: "HTML" or "Markdown" if the text carries formatting; leave
    it out for plain text. Malformed markup is retried unformatted rather than dropped.
    `silent` delivers without a notification sound.

    Returns {"sent": true, "parts": N}. This tool sends only — it cannot read anything,
    and it is the only tool on this endpoint.
    """
    who = await _authorize()
    if not _limiter.allow(who):
        raise ValueError(
            "Rate limit reached for the monitoring channel "
            f"({settings.MCP_NOTIFY_RATE_LIMIT} messages per "
            f"{settings.MCP_NOTIFY_RATE_WINDOW_S}s). Nothing was sent."
        )
    try:
        parts = await send_monitor_message(text, parse_mode=parse_mode, silent=silent)
    except NotifyError as exc:
        # The MCP client is a language model: hand it the actionable sentence, not a
        # traceback it will paste into the channel it just failed to write to.
        raise ValueError(str(exc)) from exc
    logger.info("NOTIFY: %s sent a monitoring message (%d part(s))", who, parts)
    return {"sent": True, "parts": parts}


_TOOLS = (send_message,)


def build_server(*, public_url: Optional[str] = None) -> MCPServer:
    """The MCP server, with OAuth wired up when ``public_url`` is given.

    Auth is configured per transport rather than always-on: over stdio there is no HTTP
    request to carry a token, and the SDK refuses auth settings without one.
    """
    from app.mcp_http import auth_kwargs, register_consent
    from app.mcp_oauth import NOTIFY_SCOPE

    kwargs = auth_kwargs(public_url, NOTIFY_SCOPE) if public_url else {}
    server = MCPServer("bihun-monitor", **kwargs)
    for fn in _TOOLS:
        server.tool()(fn)
    if public_url:
        register_consent(server)
    return server


def main(argv=None) -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Monitoring notify MCP server — one write-only Telegram tool."
    )
    parser.add_argument(
        "--transport", choices=("stdio", "http"), default="stdio",
        help="stdio: a local child process (default). http: a public endpoint, "
             "per-request OAuth identity.",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="http only: bind address. Keep the default and put a reverse proxy or "
             "tunnel in front — binding to 0.0.0.0 publishes it to the whole network.",
    )
    parser.add_argument("--port", type=int, default=8789, help="http only: bind port")
    args = parser.parse_args(argv)

    # Refuse to start rather than accept connections that can only fail at send time —
    # a monitoring channel that answers "not configured" every morning is worse than one
    # that never came up.
    if not settings.TELEGRAM_MONITOR_BOT_TOKEN or settings.TELEGRAM_MONITOR_CHAT_ID is None:
        raise SystemExit(
            "TELEGRAM_MONITOR_BOT_TOKEN and TELEGRAM_MONITOR_CHAT_ID must both be set: "
            "this server has nothing to deliver to without them."
        )

    if args.transport == "stdio":
        build_server().run()  # blocks until the client disconnects
        return

    from app.mcp_http import http_app, require_public_url

    public = require_public_url(
        settings.MCP_NOTIFY_PUBLIC_URL, var="MCP_NOTIFY_PUBLIC_URL"
    )
    import uvicorn

    asyncio.run(init_db())
    logger.info(f"Notify MCP server (http) on {args.host}:{args.port}, issuer {public}")
    uvicorn.run(http_app(build_server(public_url=public), public),
                host=args.host, port=args.port)


if __name__ == "__main__":
    main()
