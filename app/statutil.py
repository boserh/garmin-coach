"""Tiny numpy-free descriptive-statistics helpers shared across the pure detectors.

Consolidates the copies of ``_mean`` (``injury``/``correlations``/``subjective``) and
``_avg``/``_median`` (``garmin.repository``) that had drifted into five near-identical
definitions (CODE-AUDIT-2026-07 A5). Two flavours on purpose:

* :func:`mean` is the bare ``sum/len`` — callers already guarantee a non-empty list and
  want the raw value (it's fed straight into further arithmetic, e.g. a correlation).
* :func:`avg`/:func:`median` are the *display*-oriented variants: empty-safe (``None``)
  and rounded, for values that go into a payload/summary as-is.
"""
from typing import List, Optional


def mean(xs: List[float]) -> float:
    """Arithmetic mean. Raises on an empty list — callers gate on non-empty first."""
    return sum(xs) / len(xs)


def avg(xs: List[float]) -> Optional[float]:
    """Empty-safe mean rounded to 1 decimal, or ``None`` for an empty list."""
    return round(sum(xs) / len(xs), 1) if xs else None


def median(xs: List[float]) -> Optional[float]:
    """Empty-safe median rounded to 2 decimals, or ``None`` for an empty list."""
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return round(s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2, 2)
