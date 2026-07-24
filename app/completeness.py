"""ST-18 · "is this day's data complete?" — a pure, zero-LLM, zero-network check over a
stored ``daily_metrics`` row.

``persist_payload`` writes a day the moment ``has_data`` is true, but "has something" is
not "has everything": the 7:05 morning tick often catches a day when sleep has synced but
HRV/readiness/RHR have not, and that day then freezes half-empty because
``build_payload_cached`` never refetches a stored past day. This module names the key
recovery fields and tells apart "missing but the user never has it" (no SpO2 sensor, say)
from "missing but the watch just synced late" — completeness is judged **relative to what
this user's own history actually carries** (the fields seen non-null in the last ~30 days),
so a metric the user never produces doesn't mark every day incomplete forever.
"""
from typing import Iterable, Optional, Set

# The recovery fields that matter for baselines (NF-01), health alerts (EP-08) and trends.
# Split into direct DailyMetric columns and keys that live inside the ``extra`` JSON blob.
_COLUMN_FIELDS = ("sleep_score", "hrv_avg", "stress_avg", "bb_charged")
_EXTRA_FIELDS = ("resting_hr", "readiness_score")
KEY_FIELDS = _COLUMN_FIELDS + _EXTRA_FIELDS

# Short Ukrainian labels for the /me "missing fields" badge.
FIELD_LABELS = {
    "sleep_score": "сон",
    "hrv_avg": "HRV",
    "stress_avg": "стрес",
    "bb_charged": "body battery",
    "resting_hr": "пульс спокою",
    "readiness_score": "готовність",
}


def labels(fields: Iterable[str]) -> list:
    """Missing-field slugs → ordered Ukrainian labels (KEY_FIELDS order) for display."""
    fs = set(fields)
    return [FIELD_LABELS[f] for f in KEY_FIELDS if f in fs]


def _get_field(row, field: str):
    """Read a key field from either a DailyMetric/DailySummary object or a plain dict —
    a column directly, or a key inside ``extra``."""
    if field in _EXTRA_FIELDS:
        extra = row.get("extra") if isinstance(row, dict) else getattr(row, "extra", None)
        return (extra or {}).get(field) if isinstance(extra, dict) else None
    return row.get(field) if isinstance(row, dict) else getattr(row, field, None)


def expected_fields(history: Iterable) -> Set[str]:
    """The subset of :data:`KEY_FIELDS` this user actually produces — a field is "expected"
    only if it came back non-null on at least one day in ``history`` (typically the last 30
    days). Prevents an eternally-"incomplete" day (and its eternal refetch) for a metric the
    user's device simply never reports."""
    seen: Set[str] = set()
    for row in history:
        for f in KEY_FIELDS:
            if f not in seen and _get_field(row, f) is not None:
                seen.add(f)
        if len(seen) == len(KEY_FIELDS):
            break
    return seen


def daily_completeness(row, expected: Optional[Set[str]] = None) -> Set[str]:
    """The expected key fields that are still null in ``row`` — an empty set means "complete".
    ``expected`` defaults to all of :data:`KEY_FIELDS` (used when there's no history context
    to narrow it, e.g. a single-row check); pass :func:`expected_fields` to make it per-user."""
    exp = expected if expected is not None else set(KEY_FIELDS)
    return {f for f in exp if _get_field(row, f) is None}
