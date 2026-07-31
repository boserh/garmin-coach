"""Forward load forecast (NF-20) — pure Python, zero LLM.

ACWR/acute-load numbers (Garmin's own, and NF-04's injury radar) are strictly
retrospective — they show you're *already* overloaded. Nobody checks the next 7 days of
the PLAN ITSELF against a load number: a chat edit that stacks two extra sessions onto
Saturday sails past unnoticed until Garmin's own ACWR turns red days later, and only then
do NF-04/NF-09 start reacting.

:func:`session_load` turns one planned session (``type``/``dist_km``/``steps`` — the exact
shape ``fueling.estimate_minutes`` already reads) into a TRIMP-like number: an estimated
duration x a per-type intensity weight, in the same spirit as ``multisport``'s
duration-only fallback weight (deliberately reusing that scale rather than inventing a
new one). :func:`forecast_week` sums the CURRENT ISO week's still-``planned`` sessions
onto the week's already-happened actual load and compares the total to the trailing
chronic average (the last :data:`MIN_CHRONIC_WEEKS` completed weeks' actual load) — a
forward-looking ACWR, not a retrospective one. A cancelled (``skipped``) session drops out
the moment the caller re-lists ``planned``-status sessions, so the forecast tracks live
plan edits with zero extra wiring.

Deliberately coarse (v1, per the ticket): the per-type weight is a fixed table, never
calibrated against a user's own actual TRIMP — the "оцінка" (estimate) framing follows it
everywhere it's shown. Display-only, never blocks an edit. Fed into
``run_plan_adaptation``'s context as one extra line (not cached — adaptation never is).
"""
import datetime as dt
from typing import Iterable, List, Optional

from app import fueling

# Intensity weight per plan-session type — mirrors ``multisport._DUR_WEIGHT``'s per-sport
# scale (steady endurance ~2-3, hard efforts higher) rather than inventing a new one.
_TYPE_WEIGHT = {
    "easy": 1.5, "recovery": 1.5, "long": 2.0, "tempo": 3.0,
    "intervals": 4.0, "race": 4.0, "strength": 2.0,
}
_DEFAULT_WEIGHT = 2.0

ACWR_WARN = 1.4          # forecast ACWR at/above this → yellow
ACWR_HIGH = 1.6          # ...and at/above this → red
MIN_CHRONIC_WEEKS = 4    # trailing completed weeks averaged into the chronic load
MIN_HISTORY_DAYS = 28    # calibration gate: no ACWR number below this much stored history


def _session_hr_zone(session: dict) -> Optional[int]:
    """Highest HR zone anywhere in a session's steps tree. A cycling session has no fixed
    type-weight (km/h has no intensity table of its own) — its own working-interval zone
    stands in for one, same idea as the pace-zone target it's pushed to the watch with."""
    zones: List[int] = []

    def walk(steps):
        for s in steps or []:
            if not isinstance(s, dict):
                continue
            z = s.get("hr_zone")
            if isinstance(z, int):
                zones.append(z)
            if s.get("kind") == "repeat":
                walk(s.get("steps"))

    walk(session.get("steps"))
    return max(zones) if zones else None


def session_weight(type_str: Optional[str], hr_zone: Optional[int] = None) -> float:
    """Intensity weight for one planned session's type."""
    t = (type_str or "").lower()
    if t == "cycling":
        if isinstance(hr_zone, int) and 1 <= hr_zone <= 5:
            return float(hr_zone)
        return _DEFAULT_WEIGHT
    return _TYPE_WEIGHT.get(t, _DEFAULT_WEIGHT)


def session_load(session: dict, anchor_pace: Optional[float] = None) -> float:
    """A planned session's estimated TRIMP-like load: estimated duration
    (``fueling.estimate_minutes`` — already reads steps/dist_km/type) x its type's
    intensity weight. Zero when there's nothing to estimate a duration from (e.g. a bare
    rest/cross placeholder)."""
    minutes = fueling.estimate_minutes(session, anchor_pace)
    if not minutes:
        return 0.0
    weight = session_weight(session.get("type"), _session_hr_zone(session))
    return round(minutes * weight, 1)


def forecast_week(
    *, remaining_sessions: Iterable[dict], done_load: float,
    chronic_weekly_loads: List[float], history_days: int,
    anchor_pace: Optional[float] = None,
    min_history_days: int = MIN_HISTORY_DAYS,
    warn_acwr: float = ACWR_WARN, high_acwr: float = ACWR_HIGH,
) -> dict:
    """Forecast the CURRENT ISO week's total load and its forward-looking ACWR.

    ``remaining_sessions`` — this week's still-``planned`` sessions (today onward, each
    ``{type, dist_km?, steps?}``); ``done_load`` — this week's already-happened actual load
    (0.0 if nothing yet this week); ``chronic_weekly_loads`` — the trailing
    :data:`MIN_CHRONIC_WEEKS` completed weeks' actual load, a genuinely quiet week reading
    as ``0.0`` rather than being omitted (a real rest week is meaningful chronic signal,
    not missing data); ``history_days`` — total days of stored history (the calibration
    gate, mirrors ``app.injury``'s pattern).

    Returns ``{"load": ..., "calibrating": True}`` below :data:`MIN_HISTORY_DAYS` of
    history or with no chronic load to compare against — "calibrating", no ACWR number.
    Otherwise adds ``typical`` (the chronic weekly average), ``delta_pct`` (vs typical),
    ``acwr`` and ``level`` (``ok``/``warn``/``high``, per :data:`ACWR_WARN`/
    :data:`ACWR_HIGH`).
    """
    planned_load = sum(session_load(s, anchor_pace) for s in remaining_sessions)
    total = round((done_load or 0.0) + planned_load, 1)

    if history_days < min_history_days or not chronic_weekly_loads:
        return {"load": total, "calibrating": True}

    chronic = sum(chronic_weekly_loads) / len(chronic_weekly_loads)
    if chronic <= 0:
        return {"load": total, "calibrating": True}

    acwr = round(total / chronic, 2)
    delta_pct = round((total - chronic) / chronic * 100)
    level = "high" if acwr >= high_acwr else "warn" if acwr >= warn_acwr else "ok"
    return {
        "load": total, "typical": round(chronic, 1), "delta_pct": delta_pct,
        "acwr": acwr, "level": level, "calibrating": False,
    }


def week_end(today: dt.date) -> dt.date:
    """The Sunday (ISO week end) of ``today``'s week."""
    return today + dt.timedelta(days=6 - today.weekday())
