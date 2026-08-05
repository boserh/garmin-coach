"""NF-33 · "the same loop" — recognising repeated routes and comparing the repeats honestly.

The most natural question a runner asks is "was my usual loop faster today than a month
ago?", and the app could not answer it: nothing stored where a run happened, so ``/compare``
could only put whole PERIODS side by side. Any pace comparison across different routes is
contaminated by terrain and wind, which is why progress had to be argued indirectly (the
EF trend, Garmin's predictions) while the most convincing evidence — the same climb, 12 s
faster, at the same heart rate — was unavailable.

This module is the pure half: turn one activity's coordinates into a compact **fingerprint**,
and decide whether two fingerprints are the same route. Storage/clustering lives in
``app.garmin.repository.routes``.

Two design rules come straight from the ticket and are enforced by tests:

* **Coordinates stay on the Pi.** The fingerprint keeps a deliberately coarse start point
  (three decimals ≈ 110 m — a block, not a doorstep) and a *shape*, never a raw track; and
  nothing here is ever put into an LLM context or a ``report_logs`` row. Home addresses are
  the real risk of this feature, so the mitigation is structural rather than a convention.
* **The same loop run backwards is a DIFFERENT route.** Comparing them would be dishonest
  (the climb is in the other half), so the bearing sequence — which reverses — is part of
  the identity, and it keeps working on flat routes where the elevation profile says nothing.

Matching is tolerance-based rather than point-exact: city GPS drifts by tens of metres, so
identity rests on start proximity + total distance + the shape of the climb and the sequence
of directions, none of which a few metres of drift disturbs.
"""
import json
import math
from typing import List, Optional, Tuple

# Coarse enough that the stored start is a neighbourhood, not an address (see module intro).
START_PRECISION = 3

# Start points this close count as the same trailhead. Generous on purpose: it also absorbs
# the rounding above and the first-fix wobble of a cold GPS.
START_RADIUS_M = 200.0

# Total distance may differ by this fraction — a lap cut short or a longer cool-down loop is
# a different route, but a 200 m difference over 10 km is the same one.
DIST_TOLERANCE = 0.05

# Shape is resampled to this many equally spaced points along the route.
PROFILE_POINTS = 16

# Correlation of the two normalised elevation profiles at/above which the climbs are "the
# same shape". A mirrored profile (the loop run backwards) lands far below this.
PROFILE_MIN_CORR = 0.6

# Mean circular difference between the two bearing sequences, in degrees, at/below which the
# routes go the same way round. A reversed loop sits near 180°.
BEARING_MAX_DIFF_DEG = 50.0

# Below this there is no route to speak of (a warm-up jog around the block, a GPS stub).
MIN_DIST_KM = 1.0

# Points needed before a fingerprint is trustworthy at all.
MIN_POINTS = 8

_EARTH_R_M = 6_371_000.0


def _haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_R_M * math.asin(min(1.0, math.sqrt(h)))


def _bearing_deg(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _resample(points: List[dict], n: int) -> List[dict]:
    """``n`` points spread evenly by INDEX across the track. Index rather than distance on
    purpose: the series is already distance-downsampled by the fetcher, and a second
    distance interpolation would add error without adding information."""
    if len(points) <= n:
        return points
    step = (len(points) - 1) / (n - 1)
    return [points[min(len(points) - 1, round(i * step))] for i in range(n)]


def _normalise(values: List[Optional[float]]) -> Optional[List[float]]:
    """Scale a profile to 0..1 so two runs of the same hill compare by SHAPE, not by whatever
    absolute altitude the barometer decided on that day. ``None`` when it's flat or empty —
    a flat profile carries no identity information and must not pretend to."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    lo, hi = min(vals), max(vals)
    if hi - lo < 1.0:      # under a metre of range: flat, nothing to correlate
        return None
    return [round((v - lo) / (hi - lo), 3) for v in vals]


def _correlation(a: List[float], b: List[float]) -> Optional[float]:
    n = min(len(a), len(b))
    if n < 3:
        return None
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da == 0 or db == 0:
        return None
    return num / (da * db)


def _circular_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def fingerprint(series: Optional[List[dict]]) -> Optional[dict]:
    """Compact signature of one activity's track, or ``None`` when there is no usable GPS
    (a treadmill run, an indoor session, an old series without coordinates — the AC's
    "everything stays silent" case).

    Shape: ``{"start": [lat, lon], "dist_km", "gain_m", "profile": [...], "bearings": [...]}``
    — well under a kilobyte, and no reconstructable track.
    """
    pts = [p for p in (series or [])
           if p.get("lat") is not None and p.get("lon") is not None]
    if len(pts) < MIN_POINTS:
        return None
    dists = [p.get("d") for p in pts if p.get("d") is not None]
    dist_km = round(dists[-1] - dists[0], 2) if len(dists) >= 2 else None
    if not dist_km or dist_km < MIN_DIST_KM:
        return None

    sampled = _resample(pts, PROFILE_POINTS)
    coords = [(p["lat"], p["lon"]) for p in sampled]
    bearings = [round(_bearing_deg(coords[i], coords[i + 1]))
                for i in range(len(coords) - 1)]

    from app import gap

    elevs = [p.get("e") for p in sampled]
    profile = _normalise(gap.smooth_elevation(elevs)) if any(
        v is not None for v in elevs) else None
    gain = None
    if any(v is not None for v in [p.get("e") for p in pts]):
        gain = gap.elevation_delta(gap.smooth_elevation([p.get("e") for p in pts]))[0]

    fp = {
        "start": [round(pts[0]["lat"], START_PRECISION),
                  round(pts[0]["lon"], START_PRECISION)],
        "dist_km": dist_km,
        "bearings": bearings,
    }
    if profile:
        fp["profile"] = profile
    if gain is not None:
        fp["gain_m"] = gain
    return fp


def fingerprint_bytes(fp: Optional[dict]) -> int:
    """Serialized size of a fingerprint — the ticket caps this at 1 KB per activity, and the
    cap is a test rather than an intention."""
    return len(json.dumps(fp or {}, ensure_ascii=False).encode("utf-8"))


def similar(a: Optional[dict], b: Optional[dict]) -> bool:
    """Whether two fingerprints describe the same route, in the same direction.

    Start proximity and total distance are necessary; then the shape must agree on whatever
    evidence exists — the elevation profile when both carry one, the bearing sequence
    otherwise (and both when both are available, since a mirrored loop passes the profile
    test on a symmetric hill but never the bearing one).
    """
    if not a or not b:
        return False
    if _haversine_m(tuple(a["start"]), tuple(b["start"])) > START_RADIUS_M:
        return False
    da, db = a.get("dist_km") or 0, b.get("dist_km") or 0
    if not da or not db or abs(da - db) / max(da, db) > DIST_TOLERANCE:
        return False

    checks = []
    if a.get("profile") and b.get("profile"):
        corr = _correlation(a["profile"], b["profile"])
        checks.append(corr is not None and corr >= PROFILE_MIN_CORR)
    ba, bb = a.get("bearings") or [], b.get("bearings") or []
    if ba and bb:
        n = min(len(ba), len(bb))
        diff = sum(_circular_diff(ba[i], bb[i]) for i in range(n)) / n
        checks.append(diff <= BEARING_MAX_DIFF_DEG)
    if not checks:
        return False
    return all(checks)


def match(fp: Optional[dict], candidates: List[Tuple[int, dict]]) -> Optional[int]:
    """Id of the first known route this fingerprint belongs to, or ``None`` for a new one.

    First-match rather than best-match keeps clustering **idempotent**: re-running the
    backfill over the same activities in the same order always lands them in the same
    clusters instead of quietly re-partitioning them (an AC).
    """
    if not fp:
        return None
    for route_id, other in candidates:
        if similar(fp, other):
            return route_id
    return None


# ---------- COMPARING THE REPEATS ----------

def _pace_delta_s(now: Optional[float], other: Optional[float]) -> Optional[int]:
    """Seconds per km, ``now`` minus ``other`` (negative = this run was faster)."""
    if now is None or other is None:
        return None
    return round((now - other) * 60.0)


def build_comparison(current: dict, history: List[dict]) -> Optional[dict]:
    """The route context for one run: which pass this is, and how its GAP pace compares with
    the best and the previous pass of the SAME route.

    ``current``/``history`` items are ``{date, gap_pace_min_km, avg_hr, weather?}``;
    ``history`` is oldest-first and excludes the current run. Comparison is always on GAP
    pace (the raw split is only ever shown alongside, never used for the verdict) — that is
    the whole point of recognising the route in the first place.

    Returns ``None`` for a first-ever pass: there is nothing to compare, and inventing a
    "1st time!" line for every new route would make the feature noise.
    """
    if not history:
        return None
    pace = current.get("gap_pace_min_km")
    past = [h for h in history if h.get("gap_pace_min_km") is not None]
    out = {
        "run_number": len(history) + 1,
        "gap_pace_min_km": pace,
        "avg_hr": current.get("avg_hr"),
    }
    if past:
        best = min(past, key=lambda h: h["gap_pace_min_km"])
        prev = past[-1]
        out["best_gap_pace"] = best["gap_pace_min_km"]
        out["best_date"] = best.get("date")
        out["prev_gap_pace"] = prev["gap_pace_min_km"]
        out["prev_date"] = prev.get("date")
        out["avg_hr_prev"] = prev.get("avg_hr")
        out["delta_best_s"] = _pace_delta_s(pace, best["gap_pace_min_km"])
        out["delta_prev_s"] = _pace_delta_s(pace, prev["gap_pace_min_km"])
    return out


def _fmt_delta(seconds: Optional[int]) -> str:
    if seconds is None:
        return "—"
    sign = "-" if seconds < 0 else "+"
    s = abs(int(seconds))
    return f"{sign}{s // 60}:{s % 60:02d}" if s >= 60 else f"{sign}{s} с"


def summary(comparison: Optional[dict], name: Optional[str] = None) -> Optional[str]:
    """Deterministic Ukrainian line for the activity view / ``/compare route``. ``None`` when
    there's nothing to say (a first pass, or no GAP pace on record)."""
    if not comparison or comparison.get("delta_prev_s") is None:
        return None
    label = name or "цей маршрут"
    line = (f"🔁 {label}: {comparison['run_number']}-те проходження, "
            f"GAP-темп {_fmt_delta(comparison['delta_prev_s'])}/км до попереднього")
    if comparison.get("delta_best_s") is not None:
        best = comparison["delta_best_s"]
        line += (" — найкращий результат на цьому колі"
                 if best <= 0 else f", {_fmt_delta(best)}/км до найкращого")
    hr, hr_prev = comparison.get("avg_hr"), comparison.get("avg_hr_prev")
    if hr and hr_prev:
        line += f". Пульс {hr} проти {hr_prev}"
    return line + "."
