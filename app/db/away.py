"""NF-34 · user-scoped CRUD over ``away_periods`` + the one prompt-context helper.

Storage only; the rules (validation, parsing, overlap maths) are ``app.away``. The context
builder lives here for the same reason ``app.db.profile.build_context`` does: every LLM path
must read "is he away, and doing what?" through ONE helper, or the daily report and the
digest drift into knowing different things about the same week — which is the exact bug this
feature exists to fix.

Plain text, not encrypted like the coach profile: a note is one line the user typed about a
trip, of the same sensitivity as NF-28's lifestyle note, and it has to be readable by the
bot, the web form and the export without a key round-trip. (The profile is encrypted because
it accumulates inferred claims about a person over years — a different thing.)
"""
import datetime as dt
import logging
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import away as away_rules
from app.db.models import AwayPeriod

logger = logging.getLogger("api")

# How far back a context read looks. A period that ended months ago explains nothing about
# this week and would only cost tokens.
CONTEXT_LOOKBACK_DAYS = 90


def _as_dict(row: AwayPeriod) -> dict:
    return {"id": row.id, "start_date": row.start_date, "end_date": row.end_date,
            "kind": row.kind, "note": row.note}


async def list_periods(session: AsyncSession, user_id: int, *,
                       since: Optional[dt.date] = None) -> List[dict]:
    """This user's periods, oldest first. ``since`` drops everything that ENDED before that
    date (a period that started last month but is still running must survive the filter)."""
    q = select(AwayPeriod).where(AwayPeriod.user_id == user_id)
    if since is not None:
        q = q.where(AwayPeriod.end_date >= since.isoformat())
    rows = (await session.execute(q.order_by(AwayPeriod.start_date))).scalars().all()
    return [_as_dict(r) for r in rows]


async def get_by_id(session: AsyncSession, user_id: int, row_id: int) -> Optional[AwayPeriod]:
    """User-scoped by construction — there is no call shape that reaches another user's row."""
    return (await session.execute(
        select(AwayPeriod).where(AwayPeriod.user_id == user_id, AwayPeriod.id == row_id)
    )).scalar_one_or_none()


async def get_current(session: AsyncSession, user_id: int,
                      today: Optional[dt.date] = None) -> Optional[dict]:
    """The period covering ``today``, or ``None`` — the cheap check the background jobs use
    before deciding whether a "you missed three sessions" nudge makes any sense."""
    d = today or dt.date.today()
    return away_rules.current(await list_periods(session, user_id, since=d), d)


async def save(session: AsyncSession, user_id: int, data: dict,
               *, row_id: Optional[int] = None) -> AwayPeriod:
    """Insert (or update ``row_id``) from a ``app.away.normalize`` result. Does not commit —
    the caller owns the transaction, same as the rest of the repository.

    An identical period declared twice (the classic "confirmed the plan edit, then also typed
    ``/away``") updates the existing row instead of stacking duplicates: overlapping rows
    would make ``days_in_week`` meaningless."""
    row = await get_by_id(session, user_id, row_id) if row_id else None
    if row is None:
        row = next(
            (r for r in (await session.execute(
                select(AwayPeriod).where(
                    AwayPeriod.user_id == user_id,
                    AwayPeriod.start_date == data["start_date"],
                    AwayPeriod.end_date == data["end_date"])
            )).scalars().all()), None)
    if row is None:
        row = AwayPeriod(user_id=user_id, **data)
        session.add(row)
    else:
        row.start_date = data["start_date"]
        row.end_date = data["end_date"]
        row.kind = data["kind"]
        row.note = data["note"]
    return row


async def delete(session: AsyncSession, user_id: int, row_id: int) -> bool:
    row = await get_by_id(session, user_id, row_id)
    if row is None:
        return False
    await session.delete(row)
    return True


async def build_context(session: AsyncSession, user_id: Optional[int],
                        today: Optional[dt.date] = None, *,
                        week_start: Optional[dt.date] = None,
                        week_end: Optional[dt.date] = None) -> Optional[dict]:
    """The ``away`` prompt block for this user, or ``None``.

    The single helper every LLM path calls (daily report, digest, /ask, plan generation,
    adaptation, /sick), so "does the coach know he's away?" can't differ between surfaces.
    ``week_start``/``week_end`` make the digest's ``days_in_week`` available — how many days
    of the week being judged were away days."""
    if user_id is None:
        return None
    d = today or dt.date.today()
    since = min(d, week_start or d) - dt.timedelta(days=CONTEXT_LOOKBACK_DAYS)
    rows = await list_periods(session, user_id, since=since)
    return away_rules.to_context(rows, d, week_start=week_start, week_end=week_end)


async def apply_pending(session: AsyncSession, user_id: int,
                        pending: Optional[dict]) -> Optional[dict]:
    """Write the away period a confirmed plan proposal carried (NF-34), or ``None``.

    Lives here so the bot's ``plan_callback`` and the web ``/chat/confirm`` share one
    implementation — the two confirm paths must not diverge on whether a declared trip got
    recorded. Best-effort by design: a period that fails to store must never sink the plan
    edit the user actually asked for."""
    data = (pending or {}).get("away")
    if not data:
        return None
    try:
        await save(session, user_id, data)
        await session.commit()
    except Exception:  # noqa: BLE001 — the plan edit is the user's actual request
        logger.exception(f"AWAY save failed user={user_id}")
        return None
    logger.info(f"AWAY stored user={user_id} {data['start_date']}..{data['end_date']} "
                f"kind={data['kind']} (from plan edit)")
    return data


async def read_all(session: AsyncSession, user_id: int) -> List[dict]:
    """Every row this user owns — NF-13's data export (user-authored data, not derived)."""
    rows = (await session.execute(
        select(AwayPeriod).where(AwayPeriod.user_id == user_id)
        .order_by(AwayPeriod.start_date)
    )).scalars().all()
    return [{**_as_dict(r),
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows]
