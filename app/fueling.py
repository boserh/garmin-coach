"""Heat/duration fueling advisor (NF-11) — a pure-Python, zero-LLM calculator.

EP-13 already moves a session off an extreme-weather day; it never says HOW to survive one
that stays. :func:`advise` turns today's session (from ``plan_today``) + today's forecast
(already fetched for the report, ST-03 — no new network call) into a compact
``{duration_min, fluid_ml_h, carbs_g_h, hot, notes}`` snapshot that rides into the morning
report's context exactly like ``norm``/``records`` (see ``app.analysis.reports.run_analysis``)
so the LLM narrates it in one line instead of the report inventing numbers.

Conservative sports-nutrition rules of thumb, not medical advice — same tone as
``app.injury``/``app.health``. Silent by default: a short/easy session, no forecast, or a
cool day with a short session all return ``None`` (the ``norm``/``records`` pattern — the
context key is simply absent, the ``SYSTEM`` prompt stays quiet).
"""
from typing import Optional

# A session must clear this floor to need ANY fueling guidance — a short easy run needs
# neither a water bottle nor a start-time nudge.
MIN_DURATION_MIN = 45
# Above this duration water becomes a should-not-skip, not just nice-to-have.
FLUID_DURATION_MIN = 60
# Above this duration glycogen depletion starts mattering (carb window).
CARB_DURATION_MIN = 90

FLUID_ML_H_MILD = 500
FLUID_ML_H_HOT = 750
CARB_G_H = 45

# Fallback easy pace (min/km) when there's no typical-pace anchor for this user yet.
DEFAULT_PACE_MIN_KM = 6.5

# Rough per-type floor (minutes) used only when a session has neither structured steps nor
# a dist_km to estimate from — a coarse guess, not a real estimate.
_TYPE_MINUTES = {"long": 75, "tempo": 45, "intervals": 45, "easy": 40, "race": 40}


def _steps_minutes(steps, anchor_pace: float) -> Optional[float]:
    """Sum a structured-steps tree's estimated duration (minutes): ``dur_s`` verbatim, a
    distance step at its own pace range (or the anchor pace), a ``repeat`` group as
    reps × inner time. Mirrors (a smaller, self-contained copy of) the estimator
    ``app.routers.plan`` uses for the '~NN хв' plan-page hint — kept separate rather than
    imported, since a web router isn't a dependency this module should carry."""
    total = 0.0
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        if s.get("kind") == "repeat":
            reps = s.get("reps")
            reps = reps if isinstance(reps, (int, float)) else 1
            inner = _steps_minutes(s.get("steps"), anchor_pace)
            if inner:
                total += reps * inner
            continue
        dur_s = s.get("dur_s")
        if isinstance(dur_s, (int, float)):
            total += dur_s / 60.0
            continue
        dist_m = s.get("dist_m")
        if isinstance(dist_m, (int, float)):
            pace = anchor_pace
            p = s.get("pace_min_km")
            if (isinstance(p, (list, tuple)) and len(p) == 2
                    and all(isinstance(x, (int, float)) for x in p)):
                pace = (p[0] + p[1]) / 2
            total += (dist_m / 1000.0) * pace
    return total or None


def estimate_minutes(session: dict, anchor_pace: Optional[float] = None) -> Optional[int]:
    """Best-effort session duration in minutes: structured ``steps`` first, else
    ``dist_km`` at the anchor pace, else a rough per-``type`` floor. ``None`` only when
    there's truly nothing to estimate from (no steps, no dist_km, unknown type)."""
    pace = anchor_pace or DEFAULT_PACE_MIN_KM
    steps = session.get("steps")
    if steps:
        mins = _steps_minutes(steps, pace)
        if mins:
            return int(round(mins))
    dist_km = session.get("dist_km")
    if isinstance(dist_km, (int, float)) and dist_km > 0:
        return int(round(dist_km * pace))
    fallback = _TYPE_MINUTES.get((session.get("type") or "").lower())
    return fallback


def advise(
    session: Optional[dict],
    forecast: Optional[dict],
    *,
    heavy_types=("tempo", "intervals", "long"),
    heat_feels_c: float = 30.0,
    min_duration_min: int = MIN_DURATION_MIN,
    anchor_pace: Optional[float] = None,
) -> Optional[dict]:
    """Fueling guidance for TODAY's session (already filtered by the caller — see the
    ST-03/weather proximity rule: only when the session is today, never a future day).

    ``session`` — one ``plan_today`` entry ``{type, dist_km?, steps?}``.
    ``forecast`` — today's compact forecast (``app.weather.fetch_forecast``); reused as-is,
    no extra network call. Returns ``None`` when there's no session/forecast, the session
    isn't a key type, or it's too short to matter — the caller simply omits the context key.
    """
    if not session or not forecast:
        return None
    if (session.get("type") or "").lower() not in {t.lower() for t in heavy_types}:
        return None
    minutes = estimate_minutes(session, anchor_pace)
    if not minutes or minutes < min_duration_min:
        return None

    feels = forecast.get("feels_max_c")
    hot = isinstance(feels, (int, float)) and feels >= heat_feels_c

    notes = []
    fluid_ml_h = None
    carbs_g_h = None
    if minutes >= FLUID_DURATION_MIN:
        fluid_ml_h = FLUID_ML_H_HOT if hot else FLUID_ML_H_MILD
        notes.append(f"вода ~{fluid_ml_h} мл/год")
    if minutes >= CARB_DURATION_MIN:
        carbs_g_h = CARB_G_H
        notes.append(f"вуглеводи ~{carbs_g_h} г/год (гелі/ізотонік)")
    if hot:
        notes.append("спека — додай електроліти")
        hourly = forecast.get("hourly") or []
        coolest = min(
            (h for h in hourly if isinstance(h.get("feels_c"), (int, float))),
            key=lambda h: h["feels_c"], default=None,
        )
        if coolest:
            notes.append(f"найпрохолодніший слот ~{coolest['h']}:00 ({coolest['feels_c']}°C)")

    if not notes:
        return None
    return {
        "duration_min": minutes, "fluid_ml_h": fluid_ml_h, "carbs_g_h": carbs_g_h,
        "hot": hot, "notes": notes,
    }
