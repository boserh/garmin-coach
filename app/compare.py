"""Compare-past-self (NF-06) — "am I fitter than a year ago?".

No competitor answers "am I faster now than before last year's half?", yet the raw material
has sat in our DB since the GDPR backfill: years of runs and daily metrics. This module does
the **pure-Python** part — pick two comparable windows (now vs the same calendar span a year
ago) and let :func:`app.analysis.service.run_compare` narrate the assembled numbers with one
Sonnet call. The honesty guard (different season/conditions) lives in the prompt.

Only the date arithmetic + light formatting are here; the DB aggregation is
``repository.window_stats`` (one query per window) and the LLM call is in the service, so this
stays trivially unit-testable.
"""
import datetime as dt
from typing import List, Optional, Tuple

DEFAULT_WEEKS = 4      # last N weeks vs the same N weeks a year ago
MIN_WEEKS = 1
MAX_WEEKS = 26         # half a year — beyond that the "same span" framing stops meaning much
DEFAULT_YEARS_BACK = 1

_MONTHS_UK = [
    "січня", "лютого", "березня", "квітня", "травня", "червня",
    "липня", "серпня", "вересня", "жовтня", "листопада", "грудня",
]


def parse_period(args: Optional[List[str]]) -> int:
    """Parse the ``/compare`` argument into a window length in weeks. The leading number is
    taken as weeks (``/compare 8``, ``8w``, ``12тиж`` all → the number); anything without a
    leading digit (or nothing) → the default. Clamped to a sane range."""
    if args:
        digits = ""
        for ch in args[0].strip():
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            return max(MIN_WEEKS, min(MAX_WEEKS, int(digits)))
    return DEFAULT_WEEKS


def _shift_years(d: dt.date, years: int) -> dt.date:
    """``d`` shifted back ``years`` calendar years, Feb-29-safe (→ Feb 28)."""
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(year=d.year - years, day=28)


def window_pair(
    today: dt.date, weeks: int, years_back: int = DEFAULT_YEARS_BACK
) -> Tuple[str, str, str, str]:
    """Return ``(cur_start, cur_end, past_start, past_end)`` ISO dates: the current
    ``weeks``-long window ending today, and the same calendar span ``years_back`` years ago.
    Windows are inclusive of both ends."""
    cur_end = today
    cur_start = today - dt.timedelta(weeks=weeks) + dt.timedelta(days=1)
    past_start = _shift_years(cur_start, years_back)
    past_end = _shift_years(cur_end, years_back)
    return cur_start.isoformat(), cur_end.isoformat(), past_start.isoformat(), past_end.isoformat()


def fmt_range(start: str, end: str) -> str:
    """Human ISO range → "1 черв – 28 черв 2025" (Ukrainian months), for the message header."""
    try:
        a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except (TypeError, ValueError):
        return f"{start} – {end}"
    return f"{a.day} {_MONTHS_UK[a.month - 1]} – {b.day} {_MONTHS_UK[b.month - 1]} {b.year}"


def build_context(
    weeks: int, years_back: int, current: dict, past: dict,
) -> dict:
    """Assemble the Claude context for a comparison: the two windows' stat dicts (as
    ``repository.window_stats`` returns them) plus the framing. The LLM computes nothing —
    it interprets these numbers and flags seasonal caveats."""
    return {
        "weeks": weeks,
        "years_back": years_back,
        "current": current,
        "past": past,
    }


def has_signal(current: dict, past: dict) -> bool:
    """True when there's enough in BOTH windows to compare at all (at least one run each, or
    a fitness metric each) — otherwise the comparison is meaningless and the caller bails."""
    def _any(stats: dict) -> bool:
        return bool(stats.get("runs")) or stats.get("vo2max") is not None \
            or stats.get("avg_hrv") is not None
    return _any(current) and _any(past)
