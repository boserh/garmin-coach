"""What earns a planned session the ``long`` label — pure functions, no DB, no I/O.

``type`` is not a caption on the plan card, it is a behaviour flag. ``bot.jobs``
short-circuits the whole weekly/weather review unless the window holds one of
``ADAPT_HEAVY_TYPES`` (tempo/intervals/long) and drives weather-conflict detection off the
same set; ``SYSTEM_PLAN_ADAPT`` forbids conservative plans from cancelling a ``long`` at
all; ``_MANUAL_MATCH_TYPES`` uses it to pair a plan row with an actual run. So a session
mislabelled ``long`` makes every one of those treat a routine easy run as a key session.

That mislabelling is not hypothetical: a plan running 4 km easy on Tuesday picked up an
added 4.0 km "long" on the Wednesday, one day later, while its real long run — 6.0 km —
still stood on the Sunday. The label said "the endurance session of this week" about the
shortest run in it.

The rule below is deliberately RELATIVE. There is no absolute distance that makes a run
long: at 6-14 km a week no session qualifies by any textbook, yet one of them still carries
the week's endurance stimulus. Being the week's longest is not enough either — 4.5 km among
4.0s is not a different session, it is the same session plus a lap. So a ``long`` has to be
both:

* the longest run of its ISO week, and
* at least ``MIN_RATIO`` times the week's TYPICAL easy run (their median).

Anything else is stored as an ordinary easy run. When the week offers nothing to compare
against — no easy run at all — the label stands: this demotes only what it can disprove.
"""

from __future__ import annotations

import datetime as dt
from statistics import median
from typing import Iterable, List, Optional, Set

# How much longer than the week's typical easy run a session must be to count as the long
# one. 1.25 keeps 6.0 km against 4.0s (a real step up) and rejects 4.5 against 4.0s.
MIN_RATIO = 1.25

LONG = "long"

# What a demoted long run becomes. Not "recovery": the session itself was never the
# problem, only the claim that it was the week's endurance run.
DEMOTED_TYPE = "easy"

# The baseline is the week's EASY running, not every run in it — a 6 km tempo says nothing
# about how long an easy day is.
_EASY_TYPES = frozenset({"easy", "recovery"})

# Everything that puts kilometres on the legs, for "is this the week's longest run".
# Strength/rest/cross/cycling carry no comparable distance.
_RUN_TYPES = frozenset({"easy", "recovery", "long", "tempo", "intervals", "race"})

# Distances are one-decimal floats; compare with a slack far below that so 5.0 against a
# 5.0 threshold reads as "meets it", not "just under".
_EPS = 1e-6


def iso_week(date: str) -> Optional[str]:
    """``"2026-09-02"`` → ``"2026-W36"``, or ``None`` for an unparseable date. The plan's
    own ``week`` column is a block-relative counter that an ``add`` operation leaves empty,
    so grouping by the calendar is the only thing that always works."""
    try:
        return dt.date.fromisoformat(date).strftime("%G-W%V")
    except (TypeError, ValueError):
        return None


def _km(value) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def unearned_long_dates(runs: Iterable) -> Set[str]:
    """Dates of the ``type="long"`` sessions in ONE ISO week that do not earn the label.

    ``runs`` is any iterable of objects carrying ``date`` / ``type`` / ``dist_km`` (a
    ``PlannedWorkout`` row, or anything shaped like one). Sessions without a usable
    distance are ignored rather than demoted — a long run measured purely in time is not
    something this module can judge.
    """
    entries = []
    for r in runs:
        km = _km(getattr(r, "dist_km", None))
        entries.append((getattr(r, "date", None), (getattr(r, "type", None) or "").lower(), km))

    longs = [(d, km) for d, t, km in entries if t == LONG and km and d]
    if not longs:
        return set()

    distances: List[float] = [km for _, t, km in entries if t in _RUN_TYPES and km]
    easy: List[float] = [km for _, t, km in entries if t in _EASY_TYPES and km]
    if not easy or not distances:
        return set()          # no baseline in this week — leave the label alone

    longest = max(distances)
    floor = median(easy) * MIN_RATIO
    return {d for d, km in longs if km < longest - _EPS or km < floor - _EPS}
