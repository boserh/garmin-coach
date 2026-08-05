"""NF-25 · running dynamics — cadence, ground contact, vertical oscillation, form drift.

``fetch_activity_series`` used to pull exactly four channels (distance, pace, HR, elevation).
Cadence, ground-contact time and vertical oscillation sat in the very same ``/details``
response, never asked for — and they are the most direct biomechanical signal the account
holds. NF-04's radar watches volume, ACWR and reported pain, so it sees over-training but is
structurally blind to **form**, which collapses inside a single session: a cadence that falls
away at minute 40 of a long run is stabiliser fatigue, not "an easy day".

Two questions, both pure Python over the stored series (zero LLM, zero network):

* **within a session** — does cadence hold from the first third to the last third? Measured on
  FLAT points only: a climb legitimately shortens the stride, and reading that as "form drift"
  would make the number worthless on any hilly route (the ticket's own gate).
* **across weeks** — is the median cadence/GCT on *easy* runs moving? Comparability is the
  whole problem here, so easy runs are picked by the runner's own HR corridor
  (``app.efficiency._easy_corridor``, the same normalisation NF-19 needed) rather than by a
  plan label the activity doesn't carry.

Deliberately NOT in scope: technique advice ("land midfoot"). We report the fact and the
change; prescriptions about how somebody should run are exactly what ANALYSIS §2.2 records
users hating in competitors. A watch without the running-dynamics accessory reports none of
these channels — every function then returns ``None`` and every consumer stays silent, which
is the common case, not an error.
"""
from typing import List, Optional

from app import gap
from app.efficiency import _easy_corridor, _iso_week, _linear_trend

# A session shorter than this can't show a fatigue drift — the last third is still fresh.
MIN_DURATION_MIN = 30

# Below this many usable points the thirds are too small to median honestly.
MIN_POINTS = 15

# |grade| at or under this counts as flat. Cadence naturally drops uphill and rises downhill;
# mixing those in would produce a "drift" that is really a description of the terrain.
FLAT_GRADE_PCT = 2.0

# A within-session cadence loss of at least this much (%) is called a drift. Normal
# session-to-session noise sits around 1%; 2% is a change you can feel in the last kilometres.
CADENCE_DRIFT_PCT = 2.0

# Ground contact lengthening by this much (%) across the session is the same story told by
# the other channel — it usually moves together with cadence, so it corroborates rather than
# adds a second independent warning.
GCT_DRIFT_PCT = 3.0

# Weekly-trend honesty gates (mirroring efficiency.py's): no trend fitted on noise.
TREND_MIN_WEEKS = 4
TREND_MIN_RUNS = 6

# app.injury consumes this: cadence drift on this many CONSECUTIVE recent sessions is a
# pattern, one session is a bad night's sleep.
DRIFT_STREAK = 3


def _median(values: List[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return vals[len(vals) // 2]


def _flat_points(series: List[dict]) -> tuple:
    """``(points, flat_filtered)`` — the subset of points on flat ground, and whether the
    filter could actually be applied.

    With no elevation channel at all (an old series, a watch without an altimeter) there is
    nothing to filter by: we keep every point and say so, rather than silently pretending the
    route was flat OR dropping a perfectly usable run.
    """
    pts = [p for p in (series or []) if p.get("cad") is not None or p.get("gct") is not None]
    if not pts:
        return [], False
    elevs = [p.get("e") for p in pts]
    if not any(v is not None for v in elevs):
        return pts, False
    smoothed = gap.smooth_elevation(elevs)
    flat = []
    for i in range(1, len(pts)):
        d_prev, d_cur = pts[i - 1].get("d"), pts[i].get("d")
        if d_prev is None or d_cur is None:
            continue
        dd = d_cur - d_prev
        if dd <= 0:
            continue
        grade = gap.segment_grade_pct(smoothed[i - 1:i + 1], dd)
        if grade is None or abs(grade) <= FLAT_GRADE_PCT:
            flat.append(pts[i])
    # A route that is climbing (or descending) essentially throughout leaves nothing flat —
    # honest answer: we cannot judge form drift on it.
    return (flat, True) if len(flat) >= MIN_POINTS else ([], True)


def _thirds_drift(values: List[Optional[float]]) -> Optional[float]:
    """Percent change from the median of the first third to the median of the last third.
    Positive = the value grew. ``None`` when either third has no data."""
    vals = [v for v in values if v is not None]
    if len(vals) < MIN_POINTS:
        return None
    third = max(1, len(vals) // 3)
    first, last = _median(vals[:third]), _median(vals[-third:])
    if not first or last is None:
        return None
    return round((last - first) / first * 100.0, 1)


def session_dynamics(series: Optional[List[dict]], *,
                     dur_min: Optional[float] = None) -> Optional[dict]:
    """Running-dynamics summary for ONE activity, or ``None`` when the watch reported none of
    the channels (the majority of setups — no accessory, no dynamics).

    Returns the session averages that are actually present plus, for sessions of at least
    :data:`MIN_DURATION_MIN`, the within-session drift measured on flat ground:
    ``cadence_drift_pct`` (negative = cadence fell away), ``gct_drift_pct`` (positive =
    contact time grew) and a ``drift`` flag when either clears its threshold.
    ``flat_filtered`` says whether the elevation filter could be applied, so a consumer can
    phrase a drift on an unknown profile more carefully.
    """
    pts = [p for p in (series or [])
           if p.get("cad") is not None or p.get("gct") is not None or p.get("vo") is not None]
    if not pts:
        return None

    out: dict = {}
    cad_avg = _median([p.get("cad") for p in pts])
    gct_avg = _median([p.get("gct") for p in pts])
    vo_avg = _median([p.get("vo") for p in pts])
    if cad_avg is not None:
        out["avg_cadence"] = round(cad_avg)
    if gct_avg is not None:
        out["avg_gct_ms"] = round(gct_avg)
    if vo_avg is not None:
        out["avg_vo_cm"] = round(vo_avg, 1)

    # Stride length is derived (speed / cadence), never fetched — one less channel to depend
    # on, and it comes out of numbers we already trust.
    paces = [p.get("p") for p in pts if p.get("p")]
    pace = _median(paces) if paces else None
    if pace and cad_avg:
        out["stride_m"] = round((1000.0 / pace) / cad_avg, 2)

    if (dur_min or 0) < MIN_DURATION_MIN:
        return out

    flat, flat_filtered = _flat_points(pts)
    if not flat:
        return out
    out["flat_filtered"] = flat_filtered
    cad_drift = _thirds_drift([p.get("cad") for p in flat])
    gct_drift = _thirds_drift([p.get("gct") for p in flat])
    if cad_drift is not None:
        out["cadence_drift_pct"] = cad_drift
    if gct_drift is not None:
        out["gct_drift_pct"] = gct_drift
    out["drift"] = bool(
        (cad_drift is not None and cad_drift <= -CADENCE_DRIFT_PCT)
        or (gct_drift is not None and gct_drift >= GCT_DRIFT_PCT)
    )
    return out


def build_trend(runs: List[dict], *, weeks: int = 12) -> Optional[dict]:
    """Weekly cadence/GCT trend across *easy* runs — ``None`` when there's no dynamics data
    at all, ``{"status": "calibrating", ...}`` under the honesty gates, otherwise
    ``{"status": "ok", "weekly": [...], "current_cadence", "cadence_slope_per_week",
    "current_gct_ms", "gct_slope_per_week", "n_weeks"}``.

    ``runs`` is the ``repository.runs_for_efficiency`` shape (``{date, dur_min, dist_km,
    avg_hr, series}``). The easy-only rule is not a nicety: cadence on a 5×1000 session is
    structurally higher than on a recovery jog, so a trend over mixed intensities would
    describe the training plan rather than the runner.
    """
    long_enough = [r for r in runs
                   if r.get("avg_hr") and (r.get("dur_min") or 0) >= MIN_DURATION_MIN]
    corridor = _easy_corridor([float(r["avg_hr"]) for r in long_enough])
    if corridor is None:
        return None
    lo, hi = corridor
    easy = [r for r in long_enough if lo <= float(r["avg_hr"]) <= hi]

    buckets: dict = {}
    for r in easy:
        week = _iso_week(r.get("date"))
        dyn = session_dynamics(r.get("series"), dur_min=r.get("dur_min"))
        if week is None or not dyn:
            continue
        if "avg_cadence" not in dyn and "avg_gct_ms" not in dyn:
            continue
        buckets.setdefault(week, []).append(dyn)
    if not buckets:
        return None

    weekly = []
    for week, dyns in sorted(buckets.items())[-weeks:]:
        row = {"week": week}
        cad = _median([d.get("avg_cadence") for d in dyns])
        gct = _median([d.get("avg_gct_ms") for d in dyns])
        if cad is not None:
            row["cadence"] = round(cad)
        if gct is not None:
            row["gct_ms"] = round(gct)
        weekly.append(row)

    n_weeks = len(weekly)
    n_runs = sum(len(v) for v in buckets.values())
    if n_weeks < TREND_MIN_WEEKS or n_runs < TREND_MIN_RUNS:
        return {"status": "calibrating", "n_weeks": n_weeks, "weekly": weekly}

    out = {"status": "ok", "n_weeks": n_weeks, "weekly": weekly}
    cads = [w["cadence"] for w in weekly if w.get("cadence") is not None]
    if len(cads) >= 2:
        fit = _linear_trend(cads)
        if fit:
            out["cadence_slope_per_week"] = round(fit[0], 2)
        out["current_cadence"] = cads[-1]
    gcts = [w["gct_ms"] for w in weekly if w.get("gct_ms") is not None]
    if len(gcts) >= 2:
        fit = _linear_trend(gcts)
        if fit:
            out["gct_slope_per_week"] = round(fit[0], 2)
        out["current_gct_ms"] = gcts[-1]
    return out


def drift_streak(recent: List[Optional[dict]]) -> int:
    """How many of the MOST RECENT consecutive sessions ended with a form drift.

    ``recent`` is oldest-first (the repository convention), each item a
    :func:`session_dynamics` result or ``None``. A session with no dynamics data breaks the
    streak rather than continuing it silently — an absent measurement is not evidence.
    """
    streak = 0
    for dyn in reversed(recent or []):
        if not dyn or not dyn.get("drift"):
            break
        streak += 1
    return streak


def summary(dyn: Optional[dict]) -> Optional[str]:
    """One deterministic Ukrainian line for the activity view — fact and change only, never
    a technique prescription. ``None`` when there's nothing measured to say."""
    if not dyn:
        return None
    bits = []
    if dyn.get("avg_cadence"):
        bits.append(f"каденс {dyn['avg_cadence']} кр/хв")
    if dyn.get("stride_m"):
        bits.append(f"крок {dyn['stride_m']:.2f} м")
    if dyn.get("avg_gct_ms"):
        bits.append(f"контакт {dyn['avg_gct_ms']} мс")
    if dyn.get("avg_vo_cm"):
        bits.append(f"коливання {dyn['avg_vo_cm']:.1f} см")
    if not bits:
        return None
    line = "👟 Динаміка бігу: " + ", ".join(bits) + "."
    drift = dyn.get("cadence_drift_pct")
    if dyn.get("drift") and drift is not None:
        tail = "" if dyn.get("flat_filtered") else " (профіль траси невідомий)"
        line += f" Каденс до кінця сесії {drift:+.1f}%{tail}."
    elif drift is not None:
        line += f" Каденс тримався ({drift:+.1f}%)."
    return line
