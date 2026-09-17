"""Step-level plan-vs-actual matching (NF-14) — pure Python, zero LLM, zero network.

EP-01 already links a completed activity to the planned session ("the tempo run
happened"), but we push *structured* workouts to the watch (``PlannedWorkout.steps`` →
``app.garmin.workout_export.build_workout``) — pace ranges per interval, not just a
total distance. Nobody checked whether the runner actually hit those ranges: "did 8x400"
can mean "nailed every one" or "blew up after the third", and those are opposite signals
for plan adaptation.

:func:`flatten_steps` expands a plan's ``steps`` tree into the flat, ordered sequence a
runner actually executes on the watch (a ``repeat`` block's children appear ``reps``
times in a row; the repeat container itself never becomes a lap — mirrors
``workout_export._build_steps``'s execution order). :func:`match` pairs that sequence
positionally against the activity's actual laps (``client.fetch_activity_splits`` — one
lap per executed step, since our push always sets a distance/time end condition) and
scores each *working* step (run/tempo/interval with a pace target) hit or miss, with a
small tolerance for lap-average noise on short intervals. Warmup/recovery/cooldown steps
still occupy a slot (so index alignment with the actual laps stays correct even when
they're not the ones scored) but are never counted as a "working" miss.
"""
from typing import List, Optional

# A lap's average pace within this tolerance of the target range still counts as a hit —
# lap-average pace on a short interval is noisy, and GPS/lap-button timing always blurs
# the edges a little. Whichever tolerance is looser wins.
PACE_TOLERANCE_PCT = 0.05
PACE_TOLERANCE_MIN_KM = 3.0 / 60.0   # ~3 sec/km floor, so tolerance never shrinks to ~0

# Step kinds actually run/rowed at pace — the only ones scored hit/miss. warmup/recovery/
# cooldown steps still take a slot in the flattened sequence (for lap alignment) but are
# never "working" misses.
_WORKING_KINDS = {"run", "tempo", "interval"}


def flatten_steps(steps: Optional[list]) -> List[dict]:
    """Expand a ``PlannedWorkout.steps`` tree (as pushed to Garmin) into the flat, ordered
    list of steps a runner actually executes — one entry per lap the watch produces.
    A ``repeat`` block's children are emitted ``reps`` times in sequence; the repeat
    container itself is never a step (it's a container on the watch, not a lap). Each
    entry keeps ``kind``/``dist_m``/``dur_s``/``pace_min_km``. Returns ``[]`` for
    missing/empty input or malformed entries."""
    out: List[dict] = []

    def walk(items) -> None:
        for s in items or []:
            if not isinstance(s, dict):
                continue
            if s.get("kind") == "repeat":
                reps = max(1, int(s.get("reps") or 1))
                children = s.get("steps") or []
                for _ in range(reps):
                    walk(children)
            else:
                out.append({
                    "kind": s.get("kind"),
                    "dist_m": s.get("dist_m"),
                    "dur_s": s.get("dur_s"),
                    "pace_min_km": s.get("pace_min_km"),
                })

    walk(steps)
    return out


def _is_hit(pace_actual: Optional[float], pace_range: Optional[list]) -> bool:
    """Whether an actual lap pace (min/km) falls within ``[fast, slow]`` (+ tolerance).
    False when there's nothing to compare (no actual pace — a missing/short lap)."""
    if pace_actual is None or not pace_range or len(pace_range) != 2:
        return False
    try:
        fast, slow = sorted(float(p) for p in pace_range)
    except (TypeError, ValueError):
        return False
    tol = max(fast * PACE_TOLERANCE_PCT, PACE_TOLERANCE_MIN_KM)
    return (fast - tol) <= pace_actual <= (slow + tol)


def _pace_delta_s(pace_actual: Optional[float], pace_range: Optional[list]) -> Optional[int]:
    """How far outside the target range the lap landed, in **seconds per km**, signed:
    negative = faster than the fast edge, positive = slower than the slow edge, 0 inside.

    Distance to the nearest EDGE, not to the midpoint (UI-08): a range is a range, and a
    lap sitting on the fast edge missed by nothing, not by half the window. ``None`` when
    there's no actual pace to compare — the step was never run."""
    if pace_actual is None or not pace_range or len(pace_range) != 2:
        return None
    try:
        fast, slow = sorted(float(p) for p in pace_range)
    except (TypeError, ValueError):
        return None
    if pace_actual < fast:
        return round((pace_actual - fast) * 60)
    if pace_actual > slow:
        return round((pace_actual - slow) * 60)
    return 0


def _group_laps_by_step(laps: List[dict]) -> List[dict]:
    """Re-derive one lap per plan step from the raw physical laps, using each lap's
    ``wkt_step_index`` (Garmin's own 0-based "which pushed workout step was this" tag —
    see :func:`app.garmin.client.fetch_activity_splits`).

    Exists because a watch with Auto Lap on keeps auto-lapping at ~1 km even mid-step, so
    a single plan step can arrive as several physical laps (a 1.5 km warmup as a 1000 m +
    a 500 m lap) — pairing :func:`flatten_steps`' output against the raw, ungrouped lap
    list by plain position then compares a step against whichever physical lap happens to
    sit at that index, which is only ever correct until the first split. Grouping by
    ``wkt_step_index`` and summing distance/duration within each group restores one
    aggregate lap per step, in step order, regardless of how many physical laps it split
    into. A lap with no ``wkt_step_index`` (e.g. a trailing GPS auto-stop sliver after the
    workout ended) belongs to no step and is dropped.

    Returns ``laps`` unchanged when none of them carry the field — an older disk-cached
    ``splits:v1`` entry (pre this fix) or a free run with no pushed structure, so the
    previous, purely-positional behaviour is preserved for data that predates it."""
    if not any(lap.get("wkt_step_index") is not None for lap in laps):
        return laps
    totals: dict = {}
    for lap in laps:
        idx = lap.get("wkt_step_index")
        if idx is None:
            continue
        g = totals.setdefault(idx, {"dist_m": 0.0, "dur_s": 0.0})
        if isinstance(lap.get("dist_m"), (int, float)):
            g["dist_m"] += lap["dist_m"]
        if isinstance(lap.get("dur_s"), (int, float)):
            g["dur_s"] += lap["dur_s"]
    grouped = []
    for idx in sorted(totals):
        dist_m, dur_s = totals[idx]["dist_m"], totals[idx]["dur_s"]
        pace = round((dur_s / 60.0) / (dist_m / 1000.0), 3) if dist_m > 0 and dur_s > 0 else None
        grouped.append({"dist_m": round(dist_m, 1), "dur_s": round(dur_s, 1),
                        "pace_min_km": pace})
    return grouped


def match(steps: Optional[list], laps: Optional[list]) -> Optional[dict]:
    """Pair the flattened plan steps with the activity's actual laps (same order — see
    :func:`flatten_steps`) and score each working (pace-targeted) step. ``laps`` is
    ``client.fetch_activity_splits``'s shape: ``[{"pace_min_km": float|None, ...}, ...]``,
    first re-grouped by :func:`_group_laps_by_step` so a plan step that Auto Lap split
    into several physical laps is compared as one.

    Returns ``{"steps_hit", "steps_total", "misses": [...], "steps": [...]}``, or ``None``
    when there's nothing structured to compare — no plan steps (a free run), no actual laps
    at all, or no working step carries a pace target (an all-HR-zone session, e.g.
    easy/long by effort). Fewer laps than steps (stopped early) scores the un-lapped
    working steps as an honest miss with ``actual: null``.

    UI-08 added ``steps`` — one entry per scored step
    (``{step, kind, planned, actual, hit, delta_s, from_m, to_m}``) — so the per-interval
    picture can be drawn instead of only counted. Purely **additive**: ``steps_hit`` /
    ``steps_total`` / ``misses`` keep their exact previous meaning and stay the source of
    truth for the counters, and a stored row written before this change simply has no
    ``steps`` key (rendered as the badge alone, no migration). ``from_m``/``to_m`` are the
    step's cumulative ACTUAL distance window, present only when the laps carry distances —
    that's what lets the pace curve shade the intervals.
    """
    flat = flatten_steps(steps)
    if not flat or not laps:
        return None
    laps = _group_laps_by_step(laps)

    hit = 0
    total = 0
    misses = []
    scored = []
    covered_m = 0.0        # cumulative actual distance walked through the laps so far
    for i, step in enumerate(flat):
        lap = laps[i] if i < len(laps) else None
        start_m = covered_m
        lap_dist = lap.get("dist_m") if lap else None
        if isinstance(lap_dist, (int, float)) and lap_dist > 0:
            covered_m += float(lap_dist)

        if step.get("kind") not in _WORKING_KINDS:
            continue
        pace_range = step.get("pace_min_km")
        if not pace_range:
            continue   # an hr_zone-targeted working step has no pace to hit/miss on
        total += 1
        actual_pace = lap.get("pace_min_km") if lap else None
        planned = [round(float(p), 2) for p in pace_range]
        actual = round(actual_pace, 2) if actual_pace is not None else None
        is_hit = _is_hit(actual_pace, pace_range)
        if is_hit:
            hit += 1
        else:
            misses.append({"step": i + 1, "planned": planned, "actual": actual})
        scored.append({
            "step": i + 1,
            "kind": step.get("kind"),
            "planned": planned,
            "actual": actual,
            "hit": is_hit,
            "delta_s": _pace_delta_s(actual_pace, pace_range),
            "from_m": round(start_m) if covered_m > start_m else None,
            "to_m": round(covered_m) if covered_m > start_m else None,
        })

    if total == 0:
        return None
    return {"steps_hit": hit, "steps_total": total, "misses": misses, "steps": scored}


def badge(step_match: Optional[dict]) -> Optional[str]:
    """A compact "🎯 7/8 у цілі" label for the DM/detail page, or None without data."""
    if not step_match or not step_match.get("steps_total"):
        return None
    return f"🎯 {step_match['steps_hit']}/{step_match['steps_total']} у цілі"


def aggregate(rows: Optional[List[dict]]) -> Optional[dict]:
    """Compact "share of steps hit" summary over recent structured sessions
    (``repository.recent_step_match`` rows: ``{date, steps_hit, steps_total}``), for the
    EP-02 adaptation context — "systematically can't hold the target pace" is a different
    signal from "missed one session", and this is what tells them apart. Returns ``None``
    when there's nothing scored yet."""
    if not rows:
        return None
    total_hit = sum(r.get("steps_hit") or 0 for r in rows)
    total_steps = sum(r.get("steps_total") or 0 for r in rows)
    if total_steps == 0:
        return None
    return {
        "sessions": len(rows),
        "steps_hit": total_hit,
        "steps_total": total_steps,
        "hit_rate": round(total_hit / total_steps, 2),
    }
