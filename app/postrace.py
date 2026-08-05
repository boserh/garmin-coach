"""NF-23 · post-race debrief — splits against the plan, fade point, heart-rate drift.

The product walks a runner all the way to the start line (the T-7 pack, the T-3 checklist,
the T-1 brief) and then goes quiet exactly when the most valuable data of the season lands.
The race falls into ``activities`` as an ordinary run: the auto-analysis doesn't know it was a
race, doesn't know the target pace from the pack, doesn't compare the splits against the
planned schedule, and produces nothing for the next cycle. The debrief that forms in the
runner's head over the following week is recorded nowhere, so the next build-up starts from
scratch — the one loop in the whole product that never closed.

Pure Python, zero network, zero LLM (the narration is one Sonnet call in
``app.analysis.reports.run_race_debrief``; everything numeric is decided here):

* **the pace curve, GAP-normalised** — on any real course, raw splits describe the hills
  rather than the runner, so every judgement below is made on ``app.gap``-adjusted pace;
* **positive / negative split** — how the second half compared with the first;
* **the fade point** — the kilometre after which the pace never comes back to the corridor,
  which is the number that actually informs the next block ("held to 15 k, then went");
* **heart-rate decoupling** — speed-per-beat in the first half vs the second, the classic
  aerobic-durability read;
* **against the schedule** — only when a target time exists (NF-17's ``target_time_s`` or a
  pack's stored pace). No target → the section is simply absent, never invented.

Degradation is a feature here, not an edge case: a race with auto-lap switched off still gets
a curve (rebuilt from the per-point series), and a race with no series at all still gets its
aggregates and its narration.
"""
from typing import List, Optional

from app import gap

# A lap counts as a "kilometre split" when it's within this fraction of 1000 m. Races are
# usually recorded with auto-lap on, and those laps ARE the runner's own splits — reusing them
# is more faithful than re-slicing the downsampled series.
KM_LAP_TOLERANCE = 0.15

# The pace corridor that defines a fade: once GAP pace sits this much above the first-half
# average and never returns, the runner has faded rather than merely wobbled.
FADE_TOLERANCE = 0.04

# Below this many kilometres there's no shape to analyse (a 3 k parkrun is a single effort).
MIN_KM = 4

# Decoupling above this is worth calling out; below it is measurement noise.
DECOUPLING_NOTE_PCT = 5.0


def _pace_from(dist_m: Optional[float], dur_s: Optional[float]) -> Optional[float]:
    if not dist_m or not dur_s or dist_m <= 0:
        return None
    return (dur_s / 60.0) / (dist_m / 1000.0)


def _km_from_splits(splits: Optional[List[dict]]) -> Optional[List[dict]]:
    """Per-kilometre curve straight from Garmin's laps, when the laps ARE kilometres.

    ``None`` when auto-lap was off (one giant lap) or the workout was lapped by structure
    rather than distance — the caller then falls back to the series."""
    laps = [s for s in (splits or []) if s.get("dist_m")]
    if len(laps) < MIN_KM:
        return None
    km_like = [s for s in laps if abs(s["dist_m"] - 1000.0) <= 1000.0 * KM_LAP_TOLERANCE]
    if len(km_like) < len(laps) - 1:      # allow one short final lap
        return None
    curve = []
    for i, lap in enumerate(laps, start=1):
        pace = lap.get("pace_min_km") or _pace_from(lap.get("dist_m"), lap.get("dur_s"))
        if pace is None:
            continue
        curve.append({"km": i, "pace_min_km": round(pace, 2)})
    return curve or None


def _km_from_series(series: Optional[List[dict]]) -> Optional[List[dict]]:
    """Per-kilometre curve rebuilt from the per-point series — the degradation path for a
    race recorded without usable laps."""
    pts = [p for p in (series or []) if p.get("d") is not None and p.get("p")]
    if len(pts) < MIN_KM * 2:
        return None
    buckets: dict = {}
    for p in pts:
        buckets.setdefault(int(p["d"]) + 1, []).append(p)
    curve = []
    for km in sorted(buckets):
        chunk = buckets[km]
        paces = [p["p"] for p in chunk if p.get("p")]
        if not paces:
            continue
        curve.append({"km": km, "pace_min_km": round(sum(paces) / len(paces), 2)})
    return curve if len(curve) >= MIN_KM else None


def _enrich_with_series(curve: List[dict], series: Optional[List[dict]]) -> List[dict]:
    """Add per-kilometre HR, grade and GAP pace from the series. Without elevation the GAP
    pace equals the raw pace (honest: we can't correct what we can't see)."""
    pts = [p for p in (series or []) if p.get("d") is not None]
    by_km: dict = {}
    for p in pts:
        by_km.setdefault(int(p["d"]) + 1, []).append(p)
    # One smoothing pass over the WHOLE track, then read each kilometre's rise from its first
    # point to the next kilometre's first point. Smoothing per bucket instead would measure
    # only the wobble inside a kilometre and miss a steady climb that spans several of them —
    # which is precisely the case GAP exists for.
    elevs = [p.get("e") for p in pts]
    smoothed = gap.smooth_elevation(elevs) if any(v is not None for v in elevs) else None
    first_index: dict = {}
    for i, p in enumerate(pts):
        first_index.setdefault(int(p["d"]) + 1, i)

    for row in curve:
        km = row["km"]
        chunk = by_km.get(km) or []
        hrs = [c["hr"] for c in chunk if c.get("hr")]
        if hrs:
            row["hr"] = round(sum(hrs) / len(hrs))
        grade = None
        if smoothed is not None and km in first_index:
            start = first_index[km]
            end = first_index.get(km + 1, len(pts) - 1)
            if end > start:
                grade = gap.segment_grade_pct([smoothed[start], smoothed[end]], 1.0)
        if grade is not None:
            row["grade_pct"] = grade
            row["gap_pace_min_km"] = gap.gap_pace_min_km(row["pace_min_km"], grade)
        row.setdefault("gap_pace_min_km", row["pace_min_km"])
    return curve


def _effective(row: dict) -> Optional[float]:
    return row.get("gap_pace_min_km") or row.get("pace_min_km")


def split_halves(curve: List[dict]) -> Optional[dict]:
    """First-half vs second-half GAP pace: ``{"first", "second", "delta_pct", "negative"}``.
    ``delta_pct`` > 0 means the second half was slower (a positive split)."""
    paces = [_effective(r) for r in curve if _effective(r)]
    if len(paces) < MIN_KM:
        return None
    mid = len(paces) // 2
    first = sum(paces[:mid]) / mid
    second = sum(paces[mid:]) / len(paces[mid:])
    return {
        "first": round(first, 2),
        "second": round(second, 2),
        "delta_pct": round((second - first) / first * 100.0, 1),
        "negative": second < first,
    }


def fade_point(curve: List[dict]) -> Optional[int]:
    """The first kilometre after which GAP pace never returns to the first-half corridor —
    the number that actually feeds the next training block. ``None`` when the runner held
    (or when a slow kilometre was recovered from, which is a wobble, not a fade)."""
    paces = [(r["km"], _effective(r)) for r in curve if _effective(r)]
    if len(paces) < MIN_KM:
        return None
    mid = len(paces) // 2
    baseline = sum(p for _km, p in paces[:mid]) / mid
    ceiling = baseline * (1 + FADE_TOLERANCE)
    for i, (km, _p) in enumerate(paces):
        rest = paces[i:]
        if all(p > ceiling for _km, p in rest) and len(rest) >= 2:
            return km
    return None


def decoupling_pct(series: Optional[List[dict]]) -> Optional[float]:
    """Aerobic decoupling: speed-per-beat in the first half vs the second, in percent.
    Positive = the runner was giving away speed per heartbeat by the end. ``None`` without
    both pace and HR (a race run without a strap)."""
    pts = [p for p in (series or []) if p.get("p") and p.get("hr")]
    if len(pts) < 20:
        return None
    elevs = [p.get("e") for p in pts]
    smoothed = gap.smooth_elevation(elevs) if any(v is not None for v in elevs) else None

    def ef(chunk, offset):
        vals = []
        for i, p in enumerate(chunk):
            pace = p["p"]
            if smoothed is not None and offset + i > 0:
                grade = gap.segment_grade_pct(smoothed[offset + i - 1:offset + i + 1], 0.1)
                pace = gap.gap_pace_min_km(pace, grade) or pace
            if pace and pace > 0:
                vals.append((1000.0 / pace) / p["hr"])
        return sum(vals) / len(vals) if vals else None

    mid = len(pts) // 2
    first, second = ef(pts[:mid], 0), ef(pts[mid:], mid)
    if not first or not second:
        return None
    return round((first - second) / first * 100.0, 1)


def target_comparison(curve: List[dict], target_pace_min_km: Optional[float]) -> Optional[dict]:
    """How the race ran against its planned schedule, or ``None`` when there is no target —
    in which case the section is absent from the debrief entirely (an AC), not zero-filled."""
    if not target_pace_min_km:
        return None
    paces = [_effective(r) for r in curve if _effective(r)]
    if not paces:
        return None
    actual = sum(paces) / len(paces)
    per_km = [
        {"km": r["km"], "delta_s": round((_effective(r) - target_pace_min_km) * 60.0)}
        for r in curve if _effective(r)
    ]
    return {
        "target_pace_min_km": round(target_pace_min_km, 2),
        "actual_gap_pace_min_km": round(actual, 2),
        "delta_s_per_km": round((actual - target_pace_min_km) * 60.0),
        "total_delta_s": round((actual - target_pace_min_km) * 60.0 * len(paces)),
        "per_km": per_km,
        "km_on_target": sum(1 for r in per_km if abs(r["delta_s"]) <= 5),
    }


def build_debrief(*, splits: Optional[List[dict]] = None,
                  series: Optional[List[dict]] = None,
                  dist_km: Optional[float] = None,
                  dur_min: Optional[float] = None,
                  avg_hr: Optional[int] = None,
                  target_pace_min_km: Optional[float] = None) -> dict:
    """Everything numeric about one race, ready to narrate.

    Always returns a dict — a race with neither laps nor a series still carries its
    aggregates, and the narration works from those (the "degrade, don't crash" AC). The
    ``source`` field says which input the curve came from, so the prompt can hedge honestly.
    """
    curve = _km_from_splits(splits)
    source = "splits" if curve else None
    if not curve:
        curve = _km_from_series(series)
        source = "series" if curve else None
    if curve:
        curve = _enrich_with_series(curve, series)

    out: dict = {
        "source": source,
        "dist_km": dist_km,
        "dur_min": dur_min,
        "avg_hr": avg_hr,
    }
    if dist_km and dur_min:
        raw = dur_min / dist_km
        out["avg_pace_min_km"] = round(raw, 2)
        out["avg_gap_pace_min_km"] = gap.effective_pace_min_km(series, raw)
    if curve:
        out["km_curve"] = curve
        out["halves"] = split_halves(curve)
        out["fade_km"] = fade_point(curve)
        target = target_comparison(curve, target_pace_min_km)
        if target:
            out["target"] = target
    dec = decoupling_pct(series)
    if dec is not None:
        out["decoupling_pct"] = dec
    return out


# ---------- target pace ----------

def target_pace_for_plan(plan, dist_km: Optional[float]) -> Optional[float]:
    """The planned race pace in min/km from the plan's own intake (NF-17's ``target_time_s``)
    over the goal's distance, or ``None``.

    Reading the structured intake rather than parsing the narrated pack is deliberate: the
    ticket's own risk note is that a pace scraped out of prose is brittle, and the number is
    already stored as a number."""
    if plan is None:
        return None
    target_s = (getattr(plan, "intake", None) or {}).get("target_time_s")
    if not target_s:
        return None
    from app import race

    km = race.distance_for_goal(getattr(plan, "goal", None)) or dist_km
    if not km:
        return None
    return (float(target_s) / 60.0) / km


# ---------- deterministic fallback text ----------

def _fmt_pace(pace: Optional[float]) -> str:
    if not pace:
        return "—"
    total = round(pace * 60)
    return f"{total // 60}:{total % 60:02d}"


def summary(debrief: dict) -> str:
    """A deterministic Ukrainian summary of the numbers — used as the LLM-free fallback if
    the narration call fails, so the debrief never depends on Claude being reachable."""
    lines = ["🏁 Розбір старту"]
    if debrief.get("dist_km") and debrief.get("dur_min"):
        lines.append(f"• Дистанція {debrief['dist_km']:.1f} км за "
                     f"{int(debrief['dur_min'])} хв, темп "
                     f"{_fmt_pace(debrief.get('avg_pace_min_km'))}/км.")
    halves = debrief.get("halves")
    if halves:
        word = "негативний спліт" if halves["negative"] else "позитивний спліт"
        lines.append(f"• Половини (GAP): {_fmt_pace(halves['first'])} → "
                     f"{_fmt_pace(halves['second'])}/км — {word} "
                     f"({halves['delta_pct']:+.1f}%).")
    if debrief.get("fade_km"):
        lines.append(f"• Тримався(лась) до {debrief['fade_km']}-го км, далі темп не повернувся.")
    dec = debrief.get("decoupling_pct")
    if dec is not None and abs(dec) >= DECOUPLING_NOTE_PCT:
        lines.append(f"• Дрейф пульсу: {dec:+.1f}% швидкості на удар у другій половині.")
    target = debrief.get("target")
    if target:
        lines.append(f"• Проти розкладки: {target['delta_s_per_km']:+d} с/км "
                     f"({target['km_on_target']} км у цілі).")
    return "\n".join(lines)
