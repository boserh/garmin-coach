"""NF-08: personal read-only MCP server over the stored history.

Thin MCP wrapper around the same read-only, user-scoped tools EP-09's ``/ask`` agent uses
(:func:`app.analysis.reports._run_ask_tool`) — "talk to your own data" from Claude
Desktop/Code/web without the bot/web UI. Zero Garmin calls, zero LLM cost on our side
(the MCP client's own subscription pays for inference).

Two transports, and the difference between them is *who the request speaks for*:

``--transport stdio`` (default)
    The client launches this as a local child process, so there is no request to
    authenticate: the process binds to one user (``--email``) for its whole lifetime.
    This is the personal-tool shape NF-08 was written for.

``--transport http``
    A public endpoint (Claude's web connector cannot reach a local process). Identity
    therefore comes per-request from an OAuth 2.1 access token, whose subject is the user
    id — see :mod:`app.mcp_oauth`. The server is multi-user in this mode; each call is
    scoped to the token's own account and nothing else.

Every tool is read-only in both modes and funnels through the single dispatch point in
``_run_ask_tool`` — the same validation/caps as ``/ask`` (row caps, whitelisted daily
fields). Adding a write tool here would defeat NF-08's whole point (its own ticket names
scope creep as the main risk) — keep it read-only. When something genuinely has to write,
it gets its own service on its own origin with its own OAuth scope, the way the
monitoring channel did: see :mod:`app.mcp_notify`.

Run (opt-in dependency — ``./venv/bin/python -m pip install -e ".[mcp]"``)::

    ./venv/bin/python -m app.mcp_server --email me@example.com
    ./venv/bin/python -m app.mcp_server --transport http --port 8788   # + MCP_PUBLIC_URL

For stdio, point a client at that command (Claude Desktop's ``claude_desktop_config.json``
or ``claude mcp add``). For http, put it behind HTTPS and add ``$MCP_PUBLIC_URL`` as a
custom connector; the OAuth dance (including client registration) happens by itself.
"""
import argparse
import asyncio
import logging
from typing import List, Optional

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer

from app.core.config import settings
from app.core.logging import setup as setup_logging
from app.db import users
from app.db.base import async_session_maker, init_db

logger = logging.getLogger("mcp")

# Bound once at startup in stdio mode, and never set in http mode — where identity has to
# come from the access token instead. Keeping it None there is what makes the fallback in
# _current_user_id() unreachable rather than merely unlikely.
_user_id: Optional[int] = None


def _current_user_id() -> int:
    """Whose data this call may read.

    Over http the SDK's RequireAuthMiddleware has already rejected anything without a
    valid token by the time a tool body runs, so the token is present and its subject is
    authoritative. Over stdio there is no token and the process-bound user stands in.
    """
    token = get_access_token()
    if token is not None and token.subject:
        return int(token.subject)
    if _user_id is not None:
        return _user_id
    raise RuntimeError("MCP call with no authenticated user and no bound --email user")


async def _call(name: str, **args) -> dict:
    """Open a fresh session per call (no state shared across tool invocations) and
    dispatch through the same read-only resolver `/ask` uses."""
    from app.analysis.reports import _run_ask_tool

    async with async_session_maker() as session:
        return await _run_ask_tool(session, _current_user_id(), name, args)


async def query_activities(
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    type: Optional[str] = None, min_dist_km: Optional[float] = None,
) -> dict:
    """List this user's activities in a date range (ISO yyyy-mm-dd, both ends
    inclusive; omit either for an open range), optionally filtered by type (substring
    match, e.g. 'running') or a minimum distance in km. Returns compact rows: id,
    date, type, dist_km, dur_min, avg_hr, max_hr, avg_pace_minkm. Capped at 200 rows,
    newest first. Use get_activity_detail with the returned id to drill in."""
    return await _call(
        "query_activities", date_from=date_from, date_to=date_to,
        type=type, min_dist_km=min_dist_km,
    )


async def query_daily(
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    fields: Optional[List[str]] = None,
) -> dict:
    """Daily recovery/sleep metrics in a date range (both ends inclusive; omit either
    for an open range), oldest first. `fields` picks which metrics to return (default:
    all whitelisted ones). A day with no stored data yet is simply absent."""
    return await _call("query_daily", date_from=date_from, date_to=date_to, fields=fields)


async def aggregate_weekly(metric: str, weeks: int = 12) -> dict:
    """One metric bucketed per ISO week (oldest first) over the last `weeks` weeks
    (default 12, max 26). `metric` is a running-volume aggregate (run_km/run_count/
    run_longest_km) or any daily-metrics field name, averaged per week."""
    return await _call("aggregate_weekly", metric=metric, weeks=weeks)


async def get_activity_detail(id: int) -> dict:
    """Full detail on one activity by its DB id (from query_activities): for runs,
    pace/HR broken into ~6 segments (not the raw point series); strength exercises;
    the runner's subjective RPE/pain check-in if any; plan-vs-actual comparison if it
    was matched to a planned session."""
    return await _call("get_activity_detail", id=id)


async def get_training_plan(
    date_from: Optional[str] = None, date_to: Optional[str] = None,
) -> dict:
    """This user's ACTIVE training plan: goal, target date, days/week, intensity, the
    coach's approach summary, and its dated sessions (date, week, type, dist_km,
    description, status: planned/done/partial/missed/skipped) in a date range (both
    ends inclusive; omit either for an open range, omit both for the whole plan).

    A session's `detail` key (when present) looks its structured content up in the
    top-level `session_details` map: {"steps": [...]} for a run, {"name"?, "blocks":
    [{"sets"?, "rest_s"?, "exercises": [{"name", "reps"?, "weight_kg"?}]}]} for a
    strength day. Sessions repeating the same content share one entry. A session
    without `detail` genuinely has none stored — say so rather than guessing the
    exercises from past activities.

    Returns {"plan": null} if there's no active plan."""
    return await _call("get_training_plan", date_from=date_from, date_to=date_to)


_TOOLS = (
    query_activities,
    query_daily,
    aggregate_weekly,
    get_activity_detail,
    get_training_plan,
)


def build_server(*, public_url: Optional[str] = None) -> MCPServer:
    """The MCP server, with OAuth wired up when ``public_url`` is given.

    Auth is configured per transport rather than always-on: over stdio there is no HTTP
    request to carry a token, and the SDK refuses auth settings without one.
    """
    from app.mcp_http import auth_kwargs, register_consent
    from app.mcp_oauth import SCOPE

    kwargs = auth_kwargs(public_url, SCOPE) if public_url else {}
    server = MCPServer("bihun", **kwargs)
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
    parser = argparse.ArgumentParser(description="Personal read-only MCP server (NF-08).")
    parser.add_argument(
        "--transport", choices=("stdio", "http"), default="stdio",
        help="stdio: a local child process bound to one --email user (default). "
             "http: a public endpoint, per-request OAuth identity.",
    )
    parser.add_argument("--email", help="stdio only: which user's data this server exposes")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="http only: bind address. Keep the default and put a reverse proxy or "
             "tunnel in front — binding to 0.0.0.0 publishes it to the whole network.",
    )
    parser.add_argument("--port", type=int, default=8788, help="http only: bind port")
    args = parser.parse_args(argv)

    if args.transport == "stdio":
        if not args.email:
            raise SystemExit("--email is required for --transport stdio")
        global _user_id
        _user_id = asyncio.run(_resolve_user_id(args.email))
        logger.info(f"MCP server bound to user_id={_user_id} ({args.email})")
        build_server().run()  # stdio transport; blocks until the client disconnects
        return

    # http: identity per request, so no --email binding — and no way to fall back to one.
    from app.mcp_http import http_app, require_public_url

    require_public_url(settings.MCP_PUBLIC_URL, var="MCP_PUBLIC_URL")
    if args.email:
        # Silently ignoring it would leave the operator believing the endpoint is
        # restricted to that one account, which is the opposite of how http mode works.
        raise SystemExit("--email is meaningless with --transport http: every request "
                         "carries its own OAuth identity.")
    import uvicorn

    asyncio.run(init_db())
    logger.info(f"MCP server (http) on {args.host}:{args.port}, issuer {settings.MCP_PUBLIC_URL}")
    public = settings.MCP_PUBLIC_URL
    uvicorn.run(http_app(build_server(public_url=public), public),
                host=args.host, port=args.port)


if __name__ == "__main__":
    main()
