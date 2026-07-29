"""Relative day labels for the LLM context — pure Python, zero-LLM.

Every dated record we hand Claude (a `daily[]` row, a `recent_activities[]` entry, a
planned session, the previous report) carries an ISO ``date``, and the prompt used to ask
the model to work out "сьогодні/вчора/позавчора" itself by comparing that date against
``today``. It got it wrong often enough to be a standing complaint — a run from two days
ago narrated as "вчора", tomorrow's session announced as "сьогодні" — which is exactly the
kind of arithmetic that should never have been the model's job: it's deterministic, and we
already know the answer.

So we compute the label in Python and put it **on the record** (``day``), next to the date
it was derived from. The prompt's rule becomes "use the ``day`` field as given" instead of
"subtract these two dates correctly, every time, for every record".

The weekday in the label is a second, independent anchor: "2 дн тому (нд)" survives a
mis-read delta in a way a bare "2 дн тому" doesn't, and it lets the narration name the day
("у неділю") without doing calendar work of its own.
"""
import datetime as dt
from typing import Any, List, Optional, Union

# Short and full Ukrainian weekday names, Monday-first (``date.weekday()`` order).
WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")
WEEKDAYS_FULL = ("понеділок", "вівторок", "середа", "четвер",
                 "п'ятниця", "субота", "неділя")

# Deltas that have their own word in Ukrainian; anything further out is counted in days.
_NEAR = {0: "сьогодні", -1: "вчора", -2: "позавчора", 1: "завтра", 2: "післязавтра"}

DateLike = Union[str, dt.date, dt.datetime, None]


def parse(value: DateLike) -> Optional[dt.date]:
    """Best-effort ISO/date → ``date``. Returns None for anything unparseable — a record
    with a junk date simply goes unlabelled rather than blowing up the report."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def weekday(value: DateLike, *, full: bool = False) -> Optional[str]:
    d = parse(value)
    if d is None:
        return None
    return (WEEKDAYS_FULL if full else WEEKDAYS_SHORT)[d.weekday()]


def label(value: DateLike, today: DateLike) -> Optional[str]:
    """Relative label for ``value`` seen from ``today`` — "сьогодні (ср)", "вчора (вт)",
    "3 дн тому (нд)", "через 5 дн (пн)". None when either date is unparseable."""
    d, t = parse(value), parse(today)
    if d is None or t is None:
        return None
    delta = (d - t).days
    dow = WEEKDAYS_SHORT[d.weekday()]
    near = _NEAR.get(delta)
    if near is not None:
        return f"{near} ({dow})"
    if delta < 0:
        return f"{-delta} дн тому ({dow})"
    return f"через {delta} дн ({dow})"


def annotate(items: Any, today: DateLike, *, key: str = "date",
             field: str = "day") -> Any:
    """Copy ``items`` with a relative-day ``field`` added to every dated dict.

    Never mutates the input: the payload it labels is shared with the dedup cache and the
    30-second per-user payload memo (PERF-05), so an in-place edit would leak labels into
    a *later* request's data — the very day-shift this module exists to prevent. Non-dict
    or undated entries pass through untouched.
    """
    if not isinstance(items, list):
        return items
    out: List[Any] = []
    for it in items:
        if isinstance(it, dict) and field not in it:
            lab = label(it.get(key), today)
            if lab:
                it = {**it, field: lab}
        out.append(it)
    return out


def today_context(today: DateLike) -> dict:
    """The ``today`` block for a prompt: the ISO date plus its weekday, so the model has
    the anchor spelled out rather than inferred."""
    d = parse(today) or dt.date.today()
    return {"today": d.isoformat(), "today_weekday": WEEKDAYS_FULL[d.weekday()]}
