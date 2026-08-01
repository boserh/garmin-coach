"""Best-effort "is this lab result out of range, and how badly?" flag for the checkups
UI.

``value``/``ref_range`` are free text — typed by hand or OCR'd off a lab report — so
exact parsing is impossible in general. This only ever drives a visual highlight, never
a diagnosis: an unrecognized shape returns ``None`` (no opinion) rather than a guess.
"""
import re
from typing import Optional

_NUM = r"[-+]?\d+(?:[.,]\d+)?"
_VALUE_RE = re.compile(_NUM)
_RANGE_RE = re.compile(rf"^\s*({_NUM})\s*[-–—]\s*({_NUM})\s*$")
_LT_RE = re.compile(rf"^\s*[<≤]=?\s*({_NUM})\s*$")
_GT_RE = re.compile(rf"^\s*[>≥]=?\s*({_NUM})\s*$")

# A value past the boundary by no more than this fraction of the range's width (or, for
# a one-sided bound, of the bound's own magnitude) reads as "borderline" (orange) rather
# than "clearly abnormal" (red) — a rough rule of thumb, not a clinical threshold.
MINOR_DEVIATION_FRACTION = 0.15


def _num(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _severity(deviation: float, scale: float) -> str:
    """``deviation`` and ``scale`` are both non-negative; ``scale`` of 0 (a zero-width
    range, or a bound of 0) can't express a fraction, so any deviation there is major."""
    frac = deviation / scale if scale > 0 else float("inf")
    return "minor" if frac <= MINOR_DEVIATION_FRACTION else "major"


def out_of_range_severity(value: Optional[str], ref_range: Optional[str]) -> Optional[str]:
    """``None`` (in range, or ``value``/``ref_range`` missing/unrecognized), ``"minor"``
    (just past the edge) or ``"major"`` (well outside) — same best-effort parsing as a
    plain in/out check, now graded by how far past the boundary the value falls so a
    borderline result doesn't read as alarming as a wildly abnormal one. Handles a plain
    range ("30-400", either order), a bound ("<5.0", ">=10", "≤7"), and European decimal
    commas; anything else is left unhighlighted rather than guessed at."""
    if not value or not ref_range:
        return None
    vm = _VALUE_RE.search(value)
    if vm is None:
        return None
    v = _num(vm.group(0))
    if v is None:
        return None

    ref = ref_range.strip()
    m = _RANGE_RE.match(ref)
    if m:
        lo, hi = _num(m.group(1)), _num(m.group(2))
        if lo is None or hi is None:
            return None
        if lo > hi:
            lo, hi = hi, lo
        if v < lo:
            return _severity(lo - v, hi - lo)
        if v > hi:
            return _severity(v - hi, hi - lo)
        return None

    m = _LT_RE.match(ref)
    if m:
        bound = _num(m.group(1))
        if bound is None or v <= bound:
            return None
        return _severity(v - bound, abs(bound))

    m = _GT_RE.match(ref)
    if m:
        bound = _num(m.group(1))
        if bound is None or v >= bound:
            return None
        return _severity(bound - v, abs(bound))

    return None
