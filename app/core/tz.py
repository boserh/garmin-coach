"""Per-user timezone helpers (ST-14) — the one place that reads ``User.timezone``.

The same three-line "``ZoneInfo(user.timezone)`` with a fallback" helper had grown a copy
in ``bot.jobs`` and another in ``app.routers.chat``; both now delegate here so a user in
another timezone gets the same "today" everywhere (jobs, chat timestamps and — since the
day-confusion fix — the date context handed to Claude).

The fallback is deliberate: a corrupt/missing IANA string must never break a job or a
report, so it degrades to the process default (Europe/Warsaw) instead of raising.
"""
import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ_NAME = "Europe/Warsaw"
DEFAULT_TZ = ZoneInfo(DEFAULT_TZ_NAME)


def user_tz(user) -> ZoneInfo:
    """This user's own IANA timezone, falling back to the process default."""
    try:
        return ZoneInfo(getattr(user, "timezone", None) or DEFAULT_TZ_NAME)
    except (ZoneInfoNotFoundError, ValueError):
        return DEFAULT_TZ


def user_now(user) -> dt.datetime:
    """Current local time for this user (tz-aware)."""
    return dt.datetime.now(user_tz(user))


def user_today(user) -> dt.date:
    """Today's calendar date in this user's timezone — what "сьогодні" means for them,
    which is not necessarily what ``dt.date.today()`` says in the process timezone."""
    return user_now(user).date()
