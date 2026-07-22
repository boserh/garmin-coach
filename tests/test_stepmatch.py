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
    assert result == {"steps_hit": 4, "steps_total": 4, "misses": []}


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
