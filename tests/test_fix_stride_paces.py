"""Unit tests for the stride→pace step transform + description parsing used by the
`fix-stride-paces` CLI."""
from app.cli import _parse_pace_ranges, _stride_pace_from_desc, _strides_to_pace

# ---- description parsing ---------------------------------------------------

def test_parse_pace_ranges_finds_all():
    desc = "Легкий біг 2.4 км у темпі 6:55–7:20/км, потім прискорення (темп ~4:50–5:10/км)"
    ranges = _parse_pace_ranges(desc)
    assert len(ranges) == 2
    fast, slow = ranges[1]
    assert round(fast, 2) == 4.83 and round(slow, 2) == 5.17


def test_stride_pace_is_the_faster_range():
    desc = "Легкий біг 2.4 км у темпі 6:55–7:20/км, потім 4 прискорення (темп ~4:50–5:10/км)"
    pace = _stride_pace_from_desc(desc)
    assert pace == [4.833, 5.167]


def test_no_stride_pace_when_single_easy_range():
    # a plain easy run with one pace range must NOT be treated as having strides
    assert _stride_pace_from_desc("Легкий біг 5 км у темпі 6:45–7:00/км") is None


def test_no_stride_pace_without_clear_gap():
    # two ranges but nearly the same speed → no real easy-vs-fast separation
    assert _stride_pace_from_desc("біг 6:50–7:00/км, потім 6:40–6:55/км") is None


# ---- step transform --------------------------------------------------------

def test_stride_in_repeat_becomes_pace():
    steps = [
        {"kind": "run", "dist_m": 3400, "hr_zone": 2, "note": "орієнтовно 6:55–7:20/км"},
        {"kind": "repeat", "reps": 4, "steps": [
            {"kind": "run", "dist_m": 100, "hr_zone": 2},
            {"kind": "recovery", "dist_m": 50}]},
    ]
    out, n = _strides_to_pace(steps, [4.833, 5.167])
    assert n == 1
    # the steady easy leg is untouched (not in a repeat)
    assert out[0]["hr_zone"] == 2 and "pace_min_km" not in out[0]
    # the stride now targets pace, HR zone/note dropped
    stride = out[1]["steps"][0]
    assert stride == {"kind": "run", "dist_m": 100, "pace_min_km": [4.833, 5.167]}
    # the recovery is left alone
    assert out[1]["steps"][1] == {"kind": "recovery", "dist_m": 50}


def test_long_reps_are_not_touched():
    # a 1 km rep is an interval, not a stride — left on its HR zone
    steps = [{"kind": "repeat", "reps": 3, "steps": [
        {"kind": "run", "dist_m": 1000, "hr_zone": 3},
        {"kind": "recovery", "dur_s": 90}]}]
    out, n = _strides_to_pace(steps, [4.5, 4.8])
    assert n == 0
    assert out == steps


def test_stride_already_on_pace_is_noop():
    steps = [{"kind": "repeat", "reps": 4, "steps": [
        {"kind": "run", "dist_m": 100, "pace_min_km": [4.8, 5.1]},
        {"kind": "recovery", "dist_m": 50}]}]
    out, n = _strides_to_pace(steps, [4.833, 5.167])
    assert n == 0
    assert out == steps


def test_top_level_run_not_treated_as_stride():
    # a short run NOT inside a repeat is not a stride pattern → untouched
    steps = [{"kind": "run", "dist_m": 200, "hr_zone": 2}]
    out, n = _strides_to_pace(steps, [4.8, 5.1])
    assert n == 0
    assert out == steps


def test_does_not_mutate_input():
    steps = [{"kind": "repeat", "reps": 4, "steps": [
        {"kind": "run", "dist_m": 100, "hr_zone": 2}]}]
    _strides_to_pace(steps, [4.8, 5.1])
    assert steps[0]["steps"][0] == {"kind": "run", "dist_m": 100, "hr_zone": 2}


def test_empty_or_none():
    assert _strides_to_pace(None, [4.8, 5.1]) == ([], 0)
    assert _strides_to_pace([], [4.8, 5.1]) == ([], 0)
