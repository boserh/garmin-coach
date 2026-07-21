"""NF-07 · Quarterly/yearly "Wrapped" — a season of training given a shape.

A year of running has no shape; the numbers are scattered across days. Once a quarter or
year, this turns the history already in the DB (volume, biggest week, records set in the
period, a VO2max arc) into one aesthetic Opus longread — pure fun/retention, a shareable
artefact for a group of friends.

Only the period arithmetic + context shaping live here (trivially unit-testable). The DB
aggregate is :func:`app.garmin.repository.wrapped_stats` and the LLM call is
:func:`app.analysis.reports.run_wrapped`, mirroring the compare-past-self split.
"""
import datetime as dt
from typing import List, Optional, Tuple

# Trailing-window lengths (weeks). On-demand, so a rolling window always has data — unlike
# a calendar-bounded "Jan–Dec" that would be empty every January.
PERIODS = {"quarter": 13, "year": 52}
DEFAULT_PERIOD = "year"

_LABELS_UK = {"quarter": "квартал", "year": "рік"}

_MONTHS_UK = [
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]


def parse_period(args: Optional[List[str]]) -> str:
    """Parse the ``/wrapped`` argument into a period kind. ``quarter``/``q``/``квартал`` →
    quarter; anything else (or nothing) → the default year."""
    if args:
        a = args[0].strip().lower()
        if a in ("quarter", "q", "квартал", "кв", "3м"):
            return "quarter"
    return DEFAULT_PERIOD


def period_window(today: dt.date, kind: str) -> Tuple[str, str]:
    """``(start, end)`` ISO dates for the trailing window of ``kind`` ending today
    (inclusive of both ends)."""
    weeks = PERIODS.get(kind, PERIODS[DEFAULT_PERIOD])
    start = today - dt.timedelta(weeks=weeks) + dt.timedelta(days=1)
    return start.isoformat(), today.isoformat()


def label(kind: str) -> str:
    """Ukrainian noun for the period ('рік' / 'квартал')."""
    return _LABELS_UK.get(kind, _LABELS_UK[DEFAULT_PERIOD])


def fmt_range(start: str, end: str) -> str:
    """ISO range → "1 серпня 2025 – 20 липня 2026" (Ukrainian months), for the header."""
    try:
        a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except (TypeError, ValueError):
        return f"{start} – {end}"
    return (f"{a.day} {_MONTHS_UK[a.month - 1]} {a.year} – "
            f"{b.day} {_MONTHS_UK[b.month - 1]} {b.year}")


def _record_line(r) -> dict:
    """A PersonalRecord row → a compact dict for the narration (kind/value/prev/date)."""
    return {
        "kind": r.kind,
        "value": r.value,
        "previous_value": r.previous_value,
        "date": r.date,
    }


def build_context(kind: str, start: str, end: str, stats: dict, records) -> dict:
    """Assemble the Claude context for a Wrapped review: the period's aggregate numbers
    (as ``repository.wrapped_stats`` returns them) plus the records set in the window. The
    LLM computes nothing — it narrates these into a celebratory-but-honest recap."""
    return {
        "period": kind,
        "start": start,
        "end": end,
        "stats": stats,
        "records": [_record_line(r) for r in (records or [])],
    }


def has_signal(stats: dict) -> bool:
    """True when the period has enough to recap at all — at least a couple of runs, or some
    volume. A near-empty window makes a "Wrapped" meaningless, so the caller bails."""
    return bool(stats) and ((stats.get("runs") or 0) >= 2 or (stats.get("run_km") or 0) > 0)
