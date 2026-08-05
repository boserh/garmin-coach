"""Auto "looks like you're ill" trigger (NF-18) — pure Python, zero LLM, zero Garmin.

NF-03's block rebuild has exactly one entry point: the user typing ``/sick``. A user who
is actually ill doesn't type it — the plan just silently fills up with ``missed`` sessions
and Sunday's adaptation later patches the *symptoms* with point move/modify ops.

This module holds the objective half of the trigger: **how many planned sessions in a row
were missed**, walking backwards from the most recent past session. The bot hook
(``bot.jobs._sickness_check_for_user``) pairs it with an actionable EP-08 health report
before asking anything — a streak alone is just as likely to mean a business trip.

Deliberately NOT what NF-09 (auto-deload) looks at: NF-09 fires on a risk signal plus a
*heavy session ahead* and eases the future; this one fires on a *broken past* and repairs
it. A user who already missed half the week may well have nothing heavy ahead at all.

Status semantics (see ``app.db.models.WorkoutStatus`` and ``app.garmin.matching``):

* ``missed``           — the date passed with no matching activity → extends the streak;
* ``done``/``partial`` — the session happened → breaks the streak;
* ``skipped``          — an explicit manual "I'm not doing this" (ST-21) → also breaks it:
  the user is already managing the plan by hand, they don't need to be asked;
* ``planned``          — ignored entirely, never breaks a streak. A past row is still
  ``planned`` only for a session type the matcher doesn't track (rest, cross) or one it
  hasn't reached yet, and a rest day between two missed runs must not reset the count.
"""
import datetime as dt
from typing import Optional, Sequence

# How far back a streak may reach. Matches the ticket's "several missed sessions in the
# last week" — a longer window would start counting an old, already-recovered-from gap.
LOOKBACK_DAYS = 7

_COUNTS = "missed"
_BREAKS = ("done", "partial", "skipped")


def _as_date(value) -> Optional[dt.date]:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def missed_streak(
    workouts: Sequence[dict], *, today: dt.date, lookback_days: int = LOOKBACK_DAYS
) -> int:
    """Length of the trailing run of consecutive ``missed`` sessions.

    ``workouts`` are plan sessions as ``{"date": ISO, "status": str}`` in any order
    (extra keys ignored). Only dates in ``[today - lookback_days, today)`` count —
    today's own session is still open (the activity may sync later in the day), so it
    never participates. Unparseable rows are skipped like ``planned`` ones.

    Returns 0 when the most recent resolved session was completed (or manually skipped),
    i.e. the user is back on track even if there is an older gap behind them.
    """
    lo = today - dt.timedelta(days=lookback_days)
    rows = []
    for w in workouts:
        d = _as_date(w.get("date"))
        if d is None or not (lo <= d < today):
            continue
        status = (w.get("status") or "").lower()
        if status == _COUNTS or status in _BREAKS:
            rows.append((d, status))
    rows.sort(key=lambda r: r[0])

    streak = 0
    for _d, status in reversed(rows):
        if status != _COUNTS:
            break
        streak += 1
    return streak
