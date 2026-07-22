"""Evening sleep-debt nudge before a heavy session (NF-16) — pure Python, zero LLM.

The whole product reacts in the morning, once a bad night is already spent. The data for a
preventive nudge is already in the DB by evening: tomorrow's plan session and the last few
nights' sleep. :func:`has_sleep_debt` + :func:`tomorrow_is_heavy` fire the nudge ONLY when
BOTH hold — tomorrow is a key session (tempo/intervals/long) AND recent sleep shows a debt
signal, reusing NF-01's own personal percentile band as the threshold (the same "personal,
not generic" rule EP-08 established) plus Garmin's own sleep_need vs actual gap as an
earlier, band-free signal for a brand-new user. Either condition alone stays silent — the
EP-13 rule: "no conflict, no message" (never "before every tempo run").

No specific bedtime clock time in this version: nothing currently stored gives a wake-time
to count back from — a documented, deliberate v1 limitation (the ticket itself names this
fallback as acceptable: name no time, just "lie down earlier").
"""
from typing import List, Sequence

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


def _recent(history: Sequence[dict], key: str, window: int) -> List[float]:
    """The last ``window`` rows' non-null values for ``key`` (oldest-first order)."""
    vals = [float(v) for r in history[-window:] if isinstance((v := r.get(key)), (int, float))]
    return vals


def has_sleep_debt(history: Sequence[dict]) -> bool:
    """Pure detector over recent daily rows (oldest-first, as ``repository.read_history``
    returns — including its ``extra`` dict, where Garmin's ``sleep_need_h`` lives).

    True when EITHER: sleep_h sat below the personal NF-01 band on at least
    ``DEBT_MIN_NIGHTS`` of the last ``DEBT_WINDOW`` nights, OR the most recent night's
    Garmin-estimated need outpaces actual sleep by ``NEED_GAP_H`` or more.
    """
    norm = baselines.compute_baselines(list(history))
    if norm and "sleep_h" in norm["metrics"]:
        low = norm["metrics"]["sleep_h"]["band"][0]
        recent = _recent(history, "sleep_h", DEBT_WINDOW)
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
