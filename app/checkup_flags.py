"""Best-effort "is this lab result out of range?" flag for the checkups UI.

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


def _num(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def is_out_of_range(value: Optional[str], ref_range: Optional[str]) -> Optional[bool]:
    """True/False when ``value``'s leading number is outside ``ref_range``, ``None``
    when either is missing or doesn't match a recognized shape. Handles a plain range
    ("30-400", either order), a bound ("<5.0", ">=10", "≤7"), and European decimal
    commas. Anything else (a non-numeric value, a prose ref_range) is left unhighlighted
    rather than guessed at."""
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
        return v < lo or v > hi

    m = _LT_RE.match(ref)
    if m:
        bound = _num(m.group(1))
        return None if bound is None else v > bound

    m = _GT_RE.match(ref)
    if m:
        bound = _num(m.group(1))
        return None if bound is None else v < bound

    return None
