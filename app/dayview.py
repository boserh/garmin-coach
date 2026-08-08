"""Pure view maths for the single-day recovery page (``/me/daily_metrics/{id}``).

The page's one idea: a physiological number means nothing on its own, so every metric
is drawn **against the range it normally sits in for this person** — Garmin's own HRV
baseline where it exists, the previous weeks' own spread everywhere else. This module
turns raw values into the geometry the template paints (percent offsets along a track)
and into readable Ukrainian for Garmin's SCREAMING_ENUM strings.

Pure by design: no DB, no network, no Jinja — the router feeds it numbers and hands the
result to the template, which re-derives nothing (`docs`: "pages display, modules
compute"). Everything degrades to ``None`` rather than raising: a watch that never
reports a field, or a fresh account with three days of history, must render a quieter
page, not a stack trace.
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

# A band needs enough prior days to mean anything; below this we show the number alone
# rather than a made-up "normal" built from three samples.
MIN_HISTORY = 8

# How far past the personal range the track extends, as a share of the range's width —
# so a value sitting exactly at the edge still has somewhere to be drawn.
_PAD = 0.35


def _nums(values: Iterable) -> list[float]:
    return [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """Linear-interpolated percentile (``p`` in 0..1) of an unsorted sequence."""
    vals = sorted(_nums(values))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    idx = (len(vals) - 1) * min(max(p, 0.0), 1.0)
    lo = int(idx)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo)


def _pct(value: float, axis_lo: float, axis_hi: float) -> float:
    """Position of ``value`` along the ``axis_lo..axis_hi`` track, clamped to 0..100."""
    span = axis_hi - axis_lo
    if span <= 0:
        return 50.0
    return round(min(max((value - axis_lo) / span, 0.0), 1.0) * 100, 2)


def gauge(
    value: Optional[float],
    *,
    core_lo: Optional[float],
    core_hi: Optional[float],
    axis_lo: Optional[float] = None,
    axis_hi: Optional[float] = None,
    lower_better: bool = False,
) -> Optional[dict]:
    """Geometry for one band row: a track, the shaded "normal" core, today's marker.

    ``core_lo``/``core_hi`` are the edges of what is normal for this person. The verdict
    is which side of that core today fell on, already flipped for metrics where lower is
    better (resting HR, stress), so the template never has to know the direction.
    """
    if value is None or core_lo is None or core_hi is None:
        return None
    if core_hi < core_lo:
        core_lo, core_hi = core_hi, core_lo
    width = core_hi - core_lo
    pad = (width * _PAD) or (abs(value) * 0.12) or 1.0
    lo = min(core_lo, value) if axis_lo is None else min(axis_lo, value)
    hi = max(core_hi, value) if axis_hi is None else max(axis_hi, value)
    lo, hi = lo - pad, hi + pad
    if hi <= lo:                                   # every input identical
        lo, hi = lo - 1, hi + 1

    above = value > core_hi
    below = value < core_lo
    if above:
        verdict, good = "вище звичного", not lower_better
    elif below:
        verdict, good = "нижче звичного", lower_better
    else:
        verdict, good = "у звичному діапазоні", None
    return {
        "pos": _pct(value, lo, hi),
        "core_start": _pct(core_lo, lo, hi),
        "core_end": _pct(core_hi, lo, hi),
        "core_width": round(_pct(core_hi, lo, hi) - _pct(core_lo, lo, hi), 2),
        "core_lo": core_lo, "core_hi": core_hi,
        "axis_lo": lo, "axis_hi": hi,
        "verdict": verdict, "good": good, "outside": above or below,
    }


def history_gauge(
    history: Iterable,
    value: Optional[float],
    *,
    lower_better: bool = False,
) -> Optional[dict]:
    """A band built from this person's own recent days: core = p25..p75, plus the delta
    against their median. ``None`` until there is enough history to be honest about."""
    vals = _nums(history)
    if value is None or len(vals) < MIN_HISTORY:
        return None
    g = gauge(
        float(value),
        core_lo=percentile(vals, 0.25), core_hi=percentile(vals, 0.75),
        axis_lo=percentile(vals, 0.05), axis_hi=percentile(vals, 0.95),
        lower_better=lower_better,
    )
    if g is None:
        return None
    median = percentile(vals, 0.5)
    g["median"] = median
    g["delta"] = round(float(value) - median, 2) if median is not None else None
    g["n"] = len(vals)
    return g


def hrv_gauge(
    value: Optional[float],
    *,
    baseline_low: Optional[float],
    baseline_high: Optional[float],
    weekly_avg: Optional[float] = None,
    night_high: Optional[float] = None,
) -> Optional[dict]:
    """HRV against **Garmin's own** balanced band — the one metric where we don't have
    to infer the normal range, so we don't. The weekly average rides along as a second
    marker, because a night inside the band while the week trends down still matters."""
    g = gauge(value, core_lo=baseline_low, core_hi=baseline_high)
    if g is None:
        return None
    marks = []
    for mv, label in ((weekly_avg, "тижд."), (night_high, "макс")):
        if isinstance(mv, (int, float)):
            marks.append({"pos": _pct(float(mv), g["axis_lo"], g["axis_hi"]),
                          "label": label, "value": mv})
    g["marks"] = marks
    return g


# Sleep stages, in the order they stack — deep first, because it is the one people read
# the bar for. Keys double as the CSS modifier (``.dv-seg--deep``).
_STAGES = (("deep", "глибокий"), ("rem", "REM"), ("light", "легкий"), ("awake", "неспання"))


def sleep_segments(*, deep=None, rem=None, light=None, awake=None) -> list[dict]:
    """The night as proportional segments. Hours in, percentages out; a stage the watch
    didn't report is simply absent rather than drawn as zero."""
    got = {"deep": deep, "rem": rem, "light": light, "awake": awake}
    total = sum(v for v in _nums(got.values()) if v > 0)
    if total <= 0:
        return []
    out = []
    for key, label in _STAGES:
        v = got.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            continue
        out.append({"key": key, "label": label, "hours": v,
                    "pct": round(v / total * 100, 2)})
    return out


def battery_span(low: Optional[float], high: Optional[float]) -> Optional[dict]:
    """Where the day's Body Battery lived on the fixed 0–100 scale."""
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    lo, hi = (low, high) if low <= high else (high, low)
    return {"low": lo, "high": hi, "start": _pct(lo, 0, 100),
            "width": round(_pct(hi, 0, 100) - _pct(lo, 0, 100), 2)}


def ratio_bar(value: Optional[float], target: Optional[float]) -> Optional[dict]:
    """Progress of ``value`` towards ``target`` (sleep got vs sleep needed). Over-target
    is reported, not clipped away — the caller decides how to colour it."""
    if not isinstance(value, (int, float)) or not isinstance(target, (int, float)) or target <= 0:
        return None
    share = value / target
    return {"pct": round(min(share, 1.0) * 100, 2), "share": round(share, 3),
            "met": share >= 1.0, "gap": round(target - value, 2)}


# ---- Garmin enum strings → Ukrainian -------------------------------------------------
# Garmin's feedback vocabulary is open-ended (sleepScoreFeedback alone has dozens of
# spellings), so this maps the ones that actually show up and everything else falls back
# to a de-shouted version — a readable "Highly increased" beats a raw HIGHLY_INCREASED,
# and a wrong translation would be worse than either.
_ENUM_UK = {
    # sleep need feedback
    "HIGHLY_INCREASED": "сильно підвищена",
    "SLIGHTLY_INCREASED": "трохи підвищена",
    "INCREASED": "підвищена",
    "BALANCED": "збалансована",
    "SLIGHTLY_DECREASED": "трохи знижена",
    "HIGHLY_DECREASED": "сильно знижена",
    "DECREASED": "знижена",
    "ACTUAL_UNAVAILABLE": "немає даних",
    # generic severity / level scales (breathing disruption, readiness level)
    "NONE": "немає", "LOW": "низька", "MODERATE": "помірна", "MEDIUM": "середня",
    "HIGH": "висока", "MAXIMUM": "максимальна", "PRIME": "пік", "READY": "готовність",
    "UNBALANCED": "розбалансований", "POOR": "низький", "GOOD": "добре",
    "EXCELLENT": "відмінно", "FAIR": "посередньо",
}


def humanize(value) -> str:
    """``HIGHLY_INCREASED`` → ``сильно підвищена`` (or ``Highly increased`` if unmapped)."""
    if not isinstance(value, str):
        return str(value)
    key = value.strip().upper()
    if key in _ENUM_UK:
        return _ENUM_UK[key]
    pretty = value.replace("_", " ").strip().lower()
    return pretty[:1].upper() + pretty[1:] if pretty else value
