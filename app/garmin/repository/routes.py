"""NF-33 · user-scoped route clustering and pass history.

The pure geometry (fingerprints, similarity, the comparison numbers) is ``app.routes``; this
module only does the storage half: match a freshly stored run against the user's known
routes, create a cluster when it's a new one, and read back the history of passes.

Assignment is deliberately **idempotent** — a run that already carries a ``route_id`` is
never re-assigned, and matching takes the first similar cluster rather than the best one, so
re-running ``app.cli backfill-routes`` over the same activities cannot silently re-partition
history or create duplicate routes (an AC).
"""
import logging
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import routes as routes_mod
from app.db.models import ActivityRecord, Route

logger = logging.getLogger("api")


async def list_routes(session: AsyncSession, user_id: int) -> List[Route]:
    return list((await session.execute(
        select(Route).where(Route.user_id == user_id).order_by(Route.id)
    )).scalars().all())


async def get_route(session: AsyncSession, user_id: int, route_id: int) -> Optional[Route]:
    """User-scoped by construction — a route id from another account reads as missing."""
    return (await session.execute(
        select(Route).where(Route.user_id == user_id, Route.id == route_id)
    )).scalar_one_or_none()


async def rename_route(session: AsyncSession, user_id: int, route_id: int,
                       name: Optional[str]) -> bool:
    route = await get_route(session, user_id, route_id)
    if route is None:
        return False
    route.name = (name or "").strip()[:80] or None
    return True


async def assign_route(session: AsyncSession, user_id: int,
                       activity: ActivityRecord) -> Optional[int]:
    """Attach this activity to a route cluster, creating one if it starts a new route.
    Returns the route id, or ``None`` when the run has no usable GPS (treadmill, indoor,
    pre-NF-33 series) — in which case nothing is written at all.

    Does not commit; the caller's unit of work owns that.
    """
    if activity is None or activity.route_id is not None or not activity.series:
        return activity.route_id if activity is not None else None
    fp = routes_mod.fingerprint(activity.series)
    if not fp:
        return None
    known = await list_routes(session, user_id)
    route_id = routes_mod.match(fp, [(r.id, r.fingerprint) for r in known if r.fingerprint])
    if route_id is None:
        route = Route(user_id=user_id, fingerprint=fp)
        session.add(route)
        await session.flush()      # need the id to link the activity
        route_id = route.id
        logger.info(f"ROUTE new cluster user={user_id} route={route_id} "
                    f"dist={fp.get('dist_km')}km")
    activity.route_id = route_id
    return route_id


async def assign_routes_for_activities(session: AsyncSession, user_id: int,
                                       activity_ids: List[int]) -> int:
    """Assign routes to the just-persisted activities that carry a track and no cluster yet.
    Returns how many were linked. Runs on the sync path, so it is bounded and cheap: one
    query for the user's routes, then pure math per activity — no network, no LLM."""
    if not activity_ids:
        return 0
    rows = list((await session.execute(
        select(ActivityRecord).where(
            ActivityRecord.user_id == user_id,
            ActivityRecord.activity_id.in_([int(a) for a in activity_ids]),
            ActivityRecord.route_id.is_(None),
        )
    )).scalars().all())
    linked = 0
    for a in rows:
        if a.series and await assign_route(session, user_id, a) is not None:
            linked += 1
    return linked


async def route_passes(session: AsyncSession, user_id: int, route_id: int,
                       before_activity_id: Optional[int] = None) -> List[dict]:
    """Every stored pass of this route, oldest-first, as the shape ``app.routes`` compares:
    ``{date, gap_pace_min_km, avg_hr, dist_km}``.

    GAP pace is computed here from each pass's own series (``app.gap``) rather than stored:
    it is cheap, and it stays correct if the GAP model is ever refined. ``before_activity_id``
    excludes the run being analysed (and anything after it), so "previous pass" means
    previous in time, not "whatever is in the table now".
    """
    from app import gap

    stmt = select(ActivityRecord).where(
        ActivityRecord.user_id == user_id,
        ActivityRecord.route_id == route_id,
        ActivityRecord.is_hidden.is_(False),      # ST-17
    ).order_by(ActivityRecord.date, ActivityRecord.id)
    rows = list((await session.execute(stmt)).scalars().all())
    out = []
    for a in rows:
        if before_activity_id is not None and a.id >= before_activity_id:
            continue
        raw = (a.dur_min / a.dist_km) if (a.dur_min and a.dist_km) else None
        out.append({
            "date": a.date,
            "dist_km": a.dist_km,
            "avg_hr": a.avg_hr,
            "gap_pace_min_km": gap.effective_pace_min_km(a.series, raw),
        })
    return out


async def build_route_context(session: AsyncSession, user_id: int,
                              activity: ActivityRecord) -> Optional[dict]:
    """The ``route`` block for one activity's LLM context / detail page, or ``None``.

    Carries the anonymised ``route_id`` and pace/HR deltas ONLY — never coordinates, never a
    place name derived from them (the AC that the track never leaves the Pi). ``None`` for a
    first pass of a route, so a brand-new loop doesn't produce an empty comparison.
    """
    if activity is None or activity.route_id is None:
        return None
    from app import gap

    history = await route_passes(session, user_id, activity.route_id,
                                 before_activity_id=activity.id)
    raw = (activity.dur_min / activity.dist_km) \
        if (activity.dur_min and activity.dist_km) else None
    current = {
        "date": activity.date,
        "avg_hr": activity.avg_hr,
        "gap_pace_min_km": gap.effective_pace_min_km(activity.series, raw),
    }
    comparison = routes_mod.build_comparison(current, history)
    if not comparison:
        return None
    route = await get_route(session, user_id, activity.route_id)
    out = {"route_id": activity.route_id, **comparison}
    if route is not None and route.name:
        out["name"] = route.name
    return out
