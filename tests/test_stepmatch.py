"""NF-14: step-level plan-vs-actual matching — pure Python, no DB/LLM."""
from app import stepmatch


def _interval(pace=(4.5, 4.7), dist_m=400):
    return {"kind": "run", "dist_m": dist_m, "pace_min_km": list(pace)}


def _repeat(reps, *children):
    return {"kind": "repeat", "reps": reps, "steps": list(children)}


def _recovery(dist_m=200):
    return {"kind": "recovery", "dist_m": dist_m}


def _lap(pace):
    return {"dist_m": 400.0, "dur_s": None, "pace_min_km": pace}


# ---------- flatten_steps ----------

def test_flatten_expands_repeat_children_in_order():
    steps = [
        {"kind": "warmup", "dist_m": 1000},
        _repeat(3, _interval(), _recovery()),
        {"kind": "cooldown", "dist_m": 800},
    ]
    flat = stepmatch.flatten_steps(steps)
    kinds = [s["kind"] for s in flat]
    assert kinds == ["warmup", "run", "recovery", "run", "recovery", "run", "recovery",
                     "cooldown"]


def test_flatten_nested_repeat():
    inner = _repeat(2, _interval())
    outer = _repeat(2, inner)
    flat = stepmatch.flatten_steps([outer])
    assert len(flat) == 4
    assert all(s["kind"] == "run" for s in flat)


def test_flatten_empty_or_none():
    assert stepmatch.flatten_steps(None) == []
    assert stepmatch.flatten_steps([]) == []


def test_flatten_ignores_malformed_entries():
    assert stepmatch.flatten_steps(["not a dict", 5]) == []


# ---------- match ----------

def test_match_steady_intervals_all_hit():
    steps = [_repeat(4, _interval(pace=(4.5, 4.7)))]
    laps = [_lap(4.55), _lap(4.6), _lap(4.65), _lap(4.6)]
    result = stepmatch.match(steps, laps)
    assert result["steps_hit"] == 4
    assert result["steps_total"] == 4
    assert result["misses"] == []


def test_match_blew_up_at_the_end():
    steps = [_repeat(4, _interval(pace=(4.5, 4.7)))]
    laps = [_lap(4.55), _lap(4.6), _lap(5.3), _lap(5.5)]   # last two too slow
    result = stepmatch.match(steps, laps)
    assert result["steps_hit"] == 2
    assert result["steps_total"] == 4
    assert [m["step"] for m in result["misses"]] == [3, 4]
    assert result["misses"][0]["actual"] == 5.3


def test_match_stopped_early_is_honest_partial():
    steps = [_repeat(4, _interval(pace=(4.5, 4.7)))]
    laps = [_lap(4.55), _lap(4.6)]   # only 2 of 4 laps recorded — stopped early
    result = stepmatch.match(steps, laps)
    assert result["steps_hit"] == 2
    assert result["steps_total"] == 4
    misses = {m["step"]: m["actual"] for m in result["misses"]}
    assert misses == {3: None, 4: None}


def test_match_free_run_without_structure_is_none():
    assert stepmatch.match(None, [_lap(5.0)]) is None
    assert stepmatch.match([], [_lap(5.0)]) is None


def test_match_none_when_no_laps_at_all():
    steps = [_repeat(4, _interval())]
    assert stepmatch.match(steps, []) is None
    assert stepmatch.match(steps, None) is None


def test_match_warmup_recovery_not_counted_as_working_misses():
    steps = [
        {"kind": "warmup", "dist_m": 1000},           # no pace target at all
        _repeat(2, _interval(pace=(4.5, 4.7)), _recovery()),
        {"kind": "cooldown", "dist_m": 800},
    ]
    # warmup/recovery/cooldown laps run at an unrelated (slow) pace — must not count
    laps = [_lap(7.0), _lap(4.6), _lap(8.0), _lap(4.6), _lap(8.0), _lap(7.5)]
    result = stepmatch.match(steps, laps)
    assert result["steps_total"] == 2   # only the two `run` steps
    assert result["steps_hit"] == 2
    assert result["misses"] == []


def test_match_hr_zone_working_step_has_no_pace_target():
    steps = [{"kind": "run", "dist_m": 5000, "hr_zone": 2}]   # effort target, no pace
    laps = [_lap(6.5)]
    assert stepmatch.match(steps, laps) is None


def test_match_tolerance_allows_small_pace_noise():
    steps = [_interval(pace=(4.5, 4.7))]
    laps = [_lap(4.72)]   # just past the slow bound, within the ~3s/km tolerance
    result = stepmatch.match(steps, laps)
    assert result["steps_hit"] == 1


def test_match_outside_tolerance_is_a_miss():
    steps = [_interval(pace=(4.5, 4.7))]
    laps = [_lap(5.2)]
    result = stepmatch.match(steps, laps)
    assert result["steps_hit"] == 0
    assert result["misses"][0]["planned"] == [4.5, 4.7]


# ---------- badge ----------

def test_badge_formats_hit_over_total():
    assert stepmatch.badge({"steps_hit": 6, "steps_total": 8}) == "🎯 6/8 у цілі"


def test_badge_none_without_data():
    assert stepmatch.badge(None) is None
    assert stepmatch.badge({"steps_hit": 0, "steps_total": 0}) is None


# ---------- aggregate ----------

def test_aggregate_sums_across_sessions():
    rows = [{"date": "2026-07-01", "steps_hit": 6, "steps_total": 8},
            {"date": "2026-07-05", "steps_hit": 3, "steps_total": 6}]
    agg = stepmatch.aggregate(rows)
    assert agg == {"sessions": 2, "steps_hit": 9, "steps_total": 14,
                   "hit_rate": round(9 / 14, 2)}


def test_aggregate_none_when_empty():
    assert stepmatch.aggregate([]) is None
    assert stepmatch.aggregate(None) is None


# ---------- UI-08: the per-step detail ----------

def test_per_step_detail_is_additive_and_agrees_with_the_counters():
    """``steps_hit``/``steps_total``/``misses`` stay the source of truth for the numbers;
    ``steps`` must never disagree with them, or the page and the badge tell two stories."""
    steps = [_repeat(4, _interval(pace=(4.5, 4.7)))]
    laps = [_lap(4.55), _lap(4.6), _lap(5.3), _lap(5.5)]
    r = stepmatch.match(steps, laps)

    assert len(r["steps"]) == r["steps_total"]
    assert sum(1 for s in r["steps"] if s["hit"]) == r["steps_hit"]
    assert [s["step"] for s in r["steps"] if not s["hit"]] == [m["step"] for m in r["misses"]]


def test_delta_is_signed_and_measured_from_the_nearest_edge():
    """A range is a range: a lap on the fast edge missed by nothing, not by half the
    window. Negative = faster than the target, positive = slower."""
    steps = [_repeat(3, _interval(pace=(4.5, 4.7)))]
    # inside · 12 s/km slower than the slow edge · 12 s/km faster than the fast edge
    laps = [_lap(4.6), _lap(4.9), _lap(4.3)]
    deltas = [s["delta_s"] for s in stepmatch.match(steps, laps)["steps"]]
    assert deltas == [0, 12, -12]


def test_a_step_that_was_never_run_reports_no_actual_rather_than_zero():
    """Stopped after two of four: the module already calls the rest an honest miss, and
    the UI must not turn a missing lap into a 0:00 that never happened."""
    steps = [_repeat(4, _interval(pace=(4.5, 4.7)))]
    r = stepmatch.match(steps, [_lap(4.6), _lap(4.6)])
    tail = r["steps"][2:]
    assert [s["actual"] for s in tail] == [None, None]
    assert [s["delta_s"] for s in tail] == [None, None]
    assert all(s["hit"] is False for s in tail)
    assert r["steps_hit"] == 2 and r["steps_total"] == 4


def test_step_windows_follow_the_actual_lap_distances():
    """The shaded bands on the pace curve are placed by distance, so each scored step
    carries the cumulative actual window it occupied — warm-up included in the offset."""
    steps = [{"kind": "warmup", "dist_m": 1000},
             _repeat(2, _interval(dist_m=400), _recovery())]
    laps = [{"dist_m": 1000.0, "pace_min_km": 6.0},
            {"dist_m": 400.0, "pace_min_km": 4.6},
            {"dist_m": 200.0, "pace_min_km": 7.0},
            {"dist_m": 400.0, "pace_min_km": 4.6},
            {"dist_m": 200.0, "pace_min_km": 7.0}]
    windows = [(s["from_m"], s["to_m"]) for s in stepmatch.match(steps, laps)["steps"]]
    assert windows == [(1000, 1400), (1600, 2000)]


def test_laps_without_distances_simply_have_no_window():
    """Some watches report a lap with no distance; a band we can't place must be absent,
    not guessed."""
    steps = [_repeat(2, _interval())]
    r = stepmatch.match(steps, [{"pace_min_km": 4.6}, {"pace_min_km": 4.6}])
    assert all(s["from_m"] is None and s["to_m"] is None for s in r["steps"])
