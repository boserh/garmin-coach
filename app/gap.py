"""EP-15: grade-adjusted pace (GAP) — pure, zero-I/O elevation math.

Running costs more energy per metre climbed than on the flat, and less (down to a point)
descending — so a raw min/km split on a hilly route misrepresents effort relative to a flat
run: "6:10/km" on a steep climb can be a harder effort than "5:30/km" flat. GAP rescales the
raw pace by grade using Minetti et al. (2002)'s energy-cost-of-running polynomial
(``_cost_ratio``) — the same idea Strava/TrainingPeaks use, an off-the-shelf physiological
model rather than a proprietary fit.

Reused by ``app.analysis.reports._segments`` (per-segment GAP in the ``/activity`` payload)
and ``app.garmin.matching`` (EP-01 plan-vs-actual: judge "on pace" by GAP on a hilly route,
not the raw split).
"""
from typing import Optional

# Minetti et al. (2002) coefficients for the 5th-degree polynomial cost-of-running-per-metre
# curve, Cr(i) in J/kg/m, i = grade as a fraction (0.1 = 10%). Cr(0) = 3.6 (flat-ground cost).
_MINETTI = (155.4, -30.4, -43.3, 46.3, 19.5, 3.6)
_FLAT_COST = 3.6

# Clamp so one noisy point (barometric spike, a bridge, a GPS teleport) can't blow the
# adjustment up into nonsense — see the ticket's "барометрична висота шумить" pitfall.
_MAX_GRADE_PCT = 30.0
_MIN_FACTOR, _MAX_FACTOR = 0.6, 2.2

# A route counts as "hilly enough for GAP to matter" above this average gain — matches the
# SYSTEM_ACTIVITY instruction to mention terrain only when it's actually significant.
HILLY_GAIN_PER_KM = 10.0


def _cost_ratio(grade_pct: float) -> float:
    """Cr(grade)/Cr(0) — the energy cost of running at this grade vs flat ground."""
    i = max(-_MAX_GRADE_PCT, min(_MAX_GRADE_PCT, grade_pct)) / 100.0
    a, b, c, d, e, f = _MINETTI
    cr = a * i**5 + b * i**4 + c * i**3 + d * i**2 + e * i + f
    ratio = cr / _FLAT_COST
    return max(_MIN_FACTOR, min(_MAX_FACTOR, ratio))


def gap_pace_min_km(
    raw_pace_min_km: Optional[float], grade_pct: Optional[float]
) -> Optional[float]:
    """Grade-adjusted equivalent-flat pace for a raw ``min/km`` split at ``grade_pct``
    (positive = uphill, negative = downhill). ``None`` in (either arg) → ``None`` out."""
    if raw_pace_min_km is None or grade_pct is None:
        return None
    return round(raw_pace_min_km / _cost_ratio(grade_pct), 2)


def smooth_elevation(values: list, window: int = 5) -> list:
    """Rolling-mean smoothing of a (possibly gappy) elevation series in metres. Barometric
    altitude is noisy enough that an unsmoothed point-to-point grade swings wildly — the
    ticket's own pitfall. Missing values are forward/back-filled first so a hole doesn't
    starve the window; an all-``None`` input stays all-``None``."""
    n = len(values)
    if n == 0:
        return []
    filled = list(values)
    last = None
    for i in range(n):
        if filled[i] is None:
            filled[i] = last
        else:
            last = filled[i]
    nxt = None
    for i in range(n - 1, -1, -1):
        if filled[i] is None:
            filled[i] = nxt
        else:
            nxt = filled[i]
    if all(v is None for v in filled):
        return [None] * n
    half = window // 2
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        chunk = [v for v in filled[lo:hi] if v is not None]
        out.append(round(sum(chunk) / len(chunk), 1) if chunk else None)
    return out


def elevation_delta(smoothed: list) -> tuple:
    """(gain_m, loss_m) — sum of positive/negative consecutive deltas across a smoothed
    elevation series. ``None`` points are skipped (never treated as a 0 reading)."""
    gain = loss = 0.0
    prev = None
    for v in smoothed:
        if v is None:
            continue
        if prev is not None:
            d = v - prev
            if d > 0:
                gain += d
            else:
                loss += -d
        prev = v
    return round(gain, 1), round(loss, 1)


def segment_grade_pct(smoothed_chunk: list, dist_km: Optional[float]) -> Optional[float]:
    """Average grade (%) across a chunk, from its first/last valid smoothed elevation and
    the chunk's distance. ``None`` when there isn't enough data to compute one."""
    vals = [v for v in smoothed_chunk if v is not None]
    if len(vals) < 2 or not dist_km:
        return None
    rise = vals[-1] - vals[0]
    return round((rise / (dist_km * 1000.0)) * 100.0, 1)


def is_hilly(gain_m: float, dist_km: Optional[float]) -> bool:
    """Whether the average climb rate clears ``HILLY_GAIN_PER_KM`` — the gate for
    mentioning terrain/GAP at all (a flat run should never get GAP commentary)."""
    if not dist_km:
        return False
    return (gain_m / dist_km) > HILLY_GAIN_PER_KM


def activity_elevation_summary(series: list) -> Optional[dict]:
    """``{"gain_m", "loss_m", "hilly"}`` for a whole activity's ``series``, or ``None``
    when there's no elevation data at all (old, pre-backfill runs — never fabricated)."""
    if not series:
        return None
    elevs = [p.get("e") for p in series]
    if not any(v is not None for v in elevs):
        return None
    smoothed = smooth_elevation(elevs)
    gain, loss = elevation_delta(smoothed)
    dists = [p.get("d") for p in series if p.get("d") is not None]
    dist_km = (dists[-1] - dists[0]) if len(dists) >= 2 else None
    return {"gain_m": gain, "loss_m": loss, "hilly": is_hilly(gain, dist_km)}


def activity_gap_pace_min_km(series: list) -> Optional[float]:
    """Whole-activity distance-weighted GAP pace, or ``None`` when there isn't enough
    pace+elevation data. Averages point-to-point GAP-adjusted splits weighted by the
    distance each interval covers — fairer than one grade for the whole run on an
    out-and-back route where net elevation cancels out."""
    pts = [p for p in (series or []) if p.get("d") is not None and p.get("p") is not None]
    if len(pts) < 2:
        return None
    elevs = [p.get("e") for p in pts]
    if not any(v is not None for v in elevs):
        return None
    smoothed = smooth_elevation(elevs)
    weighted, total_dist = 0.0, 0.0
    for i in range(1, len(pts)):
        dd = pts[i]["d"] - pts[i - 1]["d"]
        if dd <= 0:
            continue
        grade = segment_grade_pct(smoothed[i - 1:i + 1], dd)
        adj = gap_pace_min_km(pts[i]["p"], grade) if grade is not None else pts[i]["p"]
        weighted += adj * dd
        total_dist += dd
    if total_dist <= 0:
        return None
    return round(weighted / total_dist, 2)


def effective_pace_min_km(
    series: Optional[list], raw_pace_min_km: Optional[float]
) -> Optional[float]:
    """GAP-adjusted average pace when the activity climbed enough to matter, else the raw
    pace unchanged — so a plan-vs-actual "on pace" judgement (EP-01) on a hilly route reads
    by effort, not penalised/credited for terrain it didn't choose."""
    summary = activity_elevation_summary(series)
    if not summary or not summary["hilly"]:
        return raw_pace_min_km
    gap_pace = activity_gap_pace_min_km(series)
    return gap_pace if gap_pace is not None else raw_pace_min_km
