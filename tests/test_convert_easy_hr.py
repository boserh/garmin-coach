"""Unit tests for the easy/long/recovery→HR-zone step transform used by the
`convert-easy-hr` CLI."""
from app.cli import _convert_easy_steps, _pace_hint


def test_run_pace_step_becomes_hr_zone():
    steps = [{"kind": "run", "dist_m": 4000, "pace_min_km": [6.75, 7.0]}]
    out, n = _convert_easy_steps(steps, zone=2)
    assert n == 1
    assert out == [{"kind": "run", "dist_m": 4000, "hr_zone": 2,
                     "note": "орієнтовно 6:45–7:00/км"}]
    assert "pace_min_km" not in out[0]


def test_warmup_cooldown_untouched():
    steps = [
        {"kind": "warmup", "dist_m": 1000, "pace_min_km": None},
        {"kind": "run", "dist_m": 4000, "pace_min_km": [6.5, 6.8]},
        {"kind": "cooldown", "dist_m": 1000},
    ]
    out, n = _convert_easy_steps(steps, zone=2)
    assert n == 1
    assert out[0] == {"kind": "warmup", "dist_m": 1000, "pace_min_km": None}
    assert out[1] == {"kind": "run", "dist_m": 4000, "hr_zone": 2,
                       "note": "орієнтовно 6:30–6:48/км"}
    assert out[2] == {"kind": "cooldown", "dist_m": 1000}


def test_already_hr_zone_is_noop():
    steps = [{"kind": "run", "dist_m": 4000, "hr_zone": 2}]
    out, n = _convert_easy_steps(steps, zone=2)
    assert n == 0
    assert out == steps


def test_existing_note_is_not_overwritten():
    steps = [{"kind": "run", "dist_m": 4000, "pace_min_km": [6.75, 7.0], "note": "довгий біг"}]
    out, n = _convert_easy_steps(steps, zone=2)
    assert n == 1
    assert out == [{"kind": "run", "dist_m": 4000, "hr_zone": 2, "note": "довгий біг"}]


def test_recurses_into_repeat_group():
    steps = [{"kind": "repeat", "reps": 3, "steps": [
        {"kind": "run", "dur_s": 300, "pace_min_km": [6.0, 6.2]},
        {"kind": "recovery", "dur_s": 60},
    ]}]
    out, n = _convert_easy_steps(steps, zone=1)
    assert n == 1
    inner = out[0]["steps"]
    assert inner[0] == {"kind": "run", "dur_s": 300, "hr_zone": 1,
                         "note": "орієнтовно 6:00–6:12/км"}
    assert inner[1] == {"kind": "recovery", "dur_s": 60}


def test_pace_hint_formats_mmss():
    assert _pace_hint([6.75, 7.0]) == "орієнтовно 6:45–7:00/км"


def test_does_not_mutate_input():
    steps = [{"kind": "run", "dist_m": 4000, "pace_min_km": [6.75, 7.0]}]
    _convert_easy_steps(steps, zone=2)
    assert steps[0]["pace_min_km"] == [6.75, 7.0]  # original untouched (fresh list built)


def test_empty_or_none():
    assert _convert_easy_steps(None, zone=2) == ([], 0)
    assert _convert_easy_steps([], zone=2) == ([], 0)
