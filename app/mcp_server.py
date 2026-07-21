"""NF-08: personal read-only MCP server over the stored history.

Thin stdio MCP wrapper around the same read-only, user-scoped tools EP-09's ``/ask``
agent uses (:func:`app.analysis.reports._run_ask_tool`) — "talk to your own data" from
Claude Desktop/Code without the bot/web UI. Zero Garmin calls, zero LLM cost on our
side (the MCP client's own subscription pays for inference). Single-user: the process
binds to one user (``--email``) for its whole lifetime — a personal tool, not a
multi-tenant endpoint, so there's no per-request auth to design.

Run (opt-in dependency — ``./venv/bin/python -m pip install -e ".[mcp]"``)::

    ./venv/bin/python -m app.mcp_server --email me@example.com

Then point a client at this command (Claude Desktop's ``claude_desktop_config.json``,
or ``claude mcp add``). Every tool is read-only and funnels through the single
dispatch point in ``_run_ask_tool`` — the same validation/caps as ``/ask`` (row caps,
whitelisted daily fields). Adding a write tool here would defeat NF-08's whole point
(its own ticket names scope creep as the main risk) — keep it read-only.
"""
import argparse
import asyncio
import logging
from typing import List, Optional

from mcp.server.fastmcp import FastMCP

from app.core.logging import setup as setup_logging
from app.db import users
from app.db.base import async_session_maker, init_db

logger = logging.getLogger("mcp")

mcp = FastMCP("garmin-coach")

_user_id: Optional[int] = None


def _require_user_id() -> int:
    if _user_id is None:
        raise RuntimeError("MCP server not initialised with a user — call main() first")
    return _user_id


async def _call(name: str, **args) -> dict:
    """Open a fresh session per call (no state shared across tool invocations) and
    dispatch through the same read-only resolver `/ask` uses."""
    from app.analysis.reports import _run_ask_tool

    async with async_session_maker() as session:
        return await _run_ask_tool(session, _require_user_id(), name, args)


@mcp.tool()
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


@mcp.tool()
async def query_daily(
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    fields: Optional[List[str]] = None,
) -> dict:
    """Daily recovery/sleep metrics in a date range (both ends inclusive; omit either
    for an open range), oldest first. `fields` picks which metrics to return (default:
    all whitelisted ones). A day with no stored data yet is simply absent."""
    return await _call("query_daily", date_from=date_from, date_to=date_to, fields=fields)


@mcp.tool()
async def aggregate_weekly(metric: str, weeks: int = 12) -> dict:
    """One metric bucketed per ISO week (oldest first) over the last `weeks` weeks
    (default 12, max 26). `metric` is a running-volume aggregate (run_km/run_count/
    run_longest_km) or any daily-metrics field name, averaged per week."""
    return await _call("aggregate_weekly", metric=metric, weeks=weeks)


@mcp.tool()
async def get_activity_detail(id: int) -> dict:
    """Full detail on one activity by its DB id (from query_activities): for runs,
    pace/HR broken into ~6 segments (not the raw point series); strength exercises;
    the runner's subjective RPE/pain check-in if any; plan-vs-actual comparison if it
    was matched to a planned session."""
    return await _call("get_activity_detail", id=id)


@mcp.tool()
async def get_training_plan(
    date_from: Optional[str] = None, date_to: Optional[str] = None,
) -> dict:
    """This user's ACTIVE training plan: goal, target date, days/week, intensity, the
    coach's approach summary, and its dated sessions (date, week, type, dist_km,
    description, status: planned/done/partial/missed/skipped) in a date range (both
    ends inclusive; omit either for an open range, omit both for the whole plan).
    Returns {"plan": null} if there's no active plan."""
    return await _call("get_training_plan", date_from=date_from, date_to=date_to)


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
    parser.add_argument("--email", required=True, help="which user's data this server exposes")
    args = parser.parse_args(argv)

    global _user_id
    _user_id = asyncio.run(_resolve_user_id(args.email))
    logger.info(f"MCP server bound to user_id={_user_id} ({args.email})")
    mcp.run()   # stdio transport; blocks until the client disconnects


if __name__ == "__main__":
    main()
