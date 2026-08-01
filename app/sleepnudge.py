"""Evening sleep-debt nudge before a heavy session (NF-16) — pure Python, zero LLM.

The whole product reacts in the morning, once a bad night is already spent. The data for a
preventive nudge is already in the DB by evening: tomorrow's plan session and the last few
nights' sleep. :func:`has_sleep_debt` + :func:`tomorrow_is_heavy` fire the nudge ONLY when
BOTH hold — tomorrow is a key session (tempo/intervals/long) AND recent sleep shows a debt
signal, reusing NF-01's own personal percentile band as the threshold (the same "personal,
not generic" rule EP-08 established) for BOTH sleep_h (duration) and sleep_score (quality —
a full night with poor quality is still a debt signal), plus Garmin's own sleep_need vs
actual gap as an earlier, band-free signal for a brand-new user. If NONE of the three hold,
the nudge stays silent — the EP-13 rule: "no conflict, no message" (never "before every
tempo run").

NF-21 adds a concrete bedtime: once ``extra.sleep_start``/``sleep_end`` (Garmin's own sleep
timing, ``service._local_hhmm``) has accumulated ``TIMING_MIN_NIGHTS`` nights, the nudge
names an actual clock time instead of "lie down earlier". Below that (a brand-new user, or
an unexpected DTO shape that never populated the fields — the ticket's AC-gate) it silently
falls back to the original number-free text — never a crash either way.
"""
import re
from statistics import median
from typing import List, Optional, Sequence

from app import baselines

HEAVY_TYPES = {"tempo", "intervals", "long"}

# How many of the last DEBT_WINDOW nights need sleep_h below the personal band to count as
# a real debt signal — a shorter cadence than EP-08's SUSTAIN_DAYS: an evening nudge reacts
# to THIS week's trend, not a month-long drift.
DEBT_WINDOW = 3
DEBT_MIN_NIGHTS = 2

# Garmin's own sleep_need_h vs actual sleep_h gap (hours) — a debt signal even before there
# is enough history for a personal band, so a brand-new user isn't silent by default.
NEED_GAP_H = 1.0

NUDGE_TEXT = (
    "🌙 Завтра важка сесія, а останні ночі сон нижче твоєї норми. Сьогодні варто лягти "
    "трохи раніше — тілу треба встигнути відновитись."
)

# NF-21: bedtime/regularity window and minimum nights of timing data before either feature
# activates — same 14-day window the ticket asks for, with a 7-night floor so a couple of
# stray nights don't drive a "confident" recommendation.
TIMING_WINDOW = 14
TIMING_MIN_NIGHTS = 7
BEDTIME_BUFFER_MIN = 15

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _recent(history: Sequence[dict], key: str, window: int) -> List[float]:
    """The last ``window`` rows' non-null values for ``key`` (oldest-first order)."""
    vals = [float(v) for r in history[-window:] if isinstance((v := r.get(key)), (int, float))]
    return vals


def has_sleep_debt(history: Sequence[dict]) -> bool:
    """Pure detector over recent daily rows (oldest-first, as ``repository.read_history``
    returns — including its ``extra`` dict, where Garmin's ``sleep_need_h`` lives).

    True when ANY of: sleep_h sat below the personal NF-01 band on at least
    ``DEBT_MIN_NIGHTS`` of the last ``DEBT_WINDOW`` nights, OR sleep_score did the same
    (duration and quality are independent failure modes — a full night with a low score
    is still a debt signal), OR the most recent night's Garmin-estimated need outpaces
    actual sleep by ``NEED_GAP_H`` or more.
    """
    norm = baselines.compute_baselines(list(history))
    for metric in ("sleep_h", "sleep_score"):
        if norm and metric in norm["metrics"]:
            low = norm["metrics"][metric]["band"][0]
            recent = _recent(history, metric, DEBT_WINDOW)
            if sum(1 for v in recent if v < low) >= DEBT_MIN_NIGHTS:
                return True

    last = history[-1] if history else None
    if last:
        extra = last.get("extra") or {}
        need = extra.get("sleep_need_h")
        actual = last.get("sleep_h")
        if (isinstance(need, (int, float)) and isinstance(actual, (int, float))
                and need - actual >= NEED_GAP_H):
            return True
    return False


def tomorrow_is_heavy(session_types: Sequence[str]) -> bool:
    """True when any of tomorrow's planned session types (caller already filtered to
    tomorrow's date) is a key session (tempo/intervals/long)."""
    return any((t or "").lower() in HEAVY_TYPES for t in session_types)


def _parse_hhmm(value) -> Optional[int]:
    """``"HH:MM"`` → minutes since midnight, or None for anything else (missing field,
    unexpected shape) — the AC-gate degrade path."""
    if not isinstance(value, str):
        return None
    m = _HHMM_RE.match(value)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _circular_median_minutes(values: Sequence[int]) -> Optional[int]:
    """Median clock time, robust to midnight wraparound (23:30 and 00:20 are 50 minutes
    apart, not ~1370). Shifts every value by 12h before taking a plain median, then shifts
    back — the shift moves the wraparound edge to midday, far from where bedtimes (and,
    incidentally, wake times) actually cluster."""
    if not values:
        return None
    shifted = sorted((v + 720) % 1440 for v in values)
    return round((median(shifted) - 720) % 1440)


def _fmt_hhmm(minutes: int) -> str:
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def _timing_values(history: Sequence[dict], key: str) -> List[int]:
    recent = history[-TIMING_WINDOW:]
    return [m for r in recent
            if (m := _parse_hhmm((r.get("extra") or {}).get(key))) is not None]


def recommended_bedtime(history: Sequence[dict]) -> Optional[str]:
    """NF-21: "lie down by HH:MM" — typical wake time (circular median of the last
    ``TIMING_WINDOW`` nights' ``sleep_end``) minus however much sleep this user actually
    needs (Garmin's own ``sleep_need_h``, or the personal NF-01 p50 ``sleep_h`` if that's
    higher) minus a ``BEDTIME_BUFFER_MIN`` buffer. None below ``TIMING_MIN_NIGHTS`` of
    timing data — including the AC-gate case where the DTO fields never populated at all."""
    wakes = _timing_values(history, "sleep_end")
    if len(wakes) < TIMING_MIN_NIGHTS:
        return None
    wake = _circular_median_minutes(wakes)

    last_extra = (history[-1].get("extra") or {}) if history else {}
    garmin_need = last_extra.get("sleep_need_h")
    norm = baselines.compute_baselines(list(history))
    personal_p50 = (norm["metrics"]["sleep_h"]["p50"]
                    if norm and "sleep_h" in norm["metrics"] else None)
    candidates = [v for v in (garmin_need, personal_p50) if isinstance(v, (int, float))]
    if not candidates:
        return None
    need_min = round(max(candidates) * 60)
    return _fmt_hhmm((wake - need_min - BEDTIME_BUFFER_MIN) % 1440)


def sleep_regularity(history: Sequence[dict]) -> Optional[dict]:
    """NF-21: how consistent bedtime has been over the last ``TIMING_WINDOW`` nights —
    ``{"std_min": ...}`` (circular std, minutes) for the weekly digest. None below
    ``TIMING_MIN_NIGHTS`` of timing data (same degrade rule as :func:`recommended_bedtime`)."""
    starts = _timing_values(history, "sleep_start")
    if len(starts) < TIMING_MIN_NIGHTS:
        return None
    center = _circular_median_minutes(starts)
    # circular deviation: the shorter arc between each night's bedtime and the median.
    deviations = [min((v - center) % 1440, (center - v) % 1440) for v in starts]
    variance = sum(d * d for d in deviations) / len(deviations)
    return {"std_min": round(variance ** 0.5)}


def nudge_text(history: Sequence[dict]) -> str:
    """The evening nudge's message: a concrete bedtime once there's enough timing data
    (NF-21), the original number-free text otherwise (NF-16's original fallback)."""
    bedtime = recommended_bedtime(history)
    if bedtime is None:
        return NUDGE_TEXT
    return (
        f"🌙 Завтра важка сесія, а останні ночі сон нижче твоєї норми. Щоб добрати сну до "
        f"звичного підйому — сьогодні відбій до {bedtime}."
    )
