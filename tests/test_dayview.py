"""Pure maths behind the single-day recovery page (app.dayview)."""

import pytest

from app import dayview


# ---- percentile -------------------------------------------------------------------

def test_percentile_interpolates_and_ignores_non_numbers():
    vals = [10, 20, 30, 40, None, "x", True]
    assert dayview.percentile(vals, 0.0) == 10
    assert dayview.percentile(vals, 1.0) == 40
    assert dayview.percentile(vals, 0.5) == 25

    assert dayview.percentile([], 0.5) is None
    assert dayview.percentile([7], 0.9) == 7


# ---- gauge ------------------------------------------------------------------------

def test_gauge_places_value_inside_its_core_band():
    g = dayview.gauge(50, core_lo=40, core_hi=60)
    assert g["verdict"] == "у звичному діапазоні"
    assert g["good"] is None
    assert g["outside"] is False
    assert g["core_start"] < g["pos"] < g["core_end"]


def test_gauge_verdict_flips_for_lower_is_better_metrics():
    high = dayview.gauge(70, core_lo=40, core_hi=60)
    assert (high["verdict"], high["good"]) == ("вище звичного", True)

    # resting HR above the personal band is the bad direction, same numbers
    rhr = dayview.gauge(70, core_lo=40, core_hi=60, lower_better=True)
    assert (rhr["verdict"], rhr["good"]) == ("вище звичного", False)
    low_rhr = dayview.gauge(30, core_lo=40, core_hi=60, lower_better=True)
    assert (low_rhr["verdict"], low_rhr["good"]) == ("нижче звичного", True)


def test_gauge_keeps_an_outlier_on_the_track():
    """A value far past the band must still be drawable — clamped into 0..100, never
    positioned off the element."""
    g = dayview.gauge(500, core_lo=40, core_hi=60)
    assert 0 <= g["pos"] <= 100
    assert g["outside"] is True


def test_gauge_survives_a_zero_width_band():
    g = dayview.gauge(42, core_lo=42, core_hi=42)
    assert g is not None
    assert 0 <= g["pos"] <= 100


def test_gauge_none_without_a_value_or_a_band():
    assert dayview.gauge(None, core_lo=1, core_hi=2) is None
    assert dayview.gauge(5, core_lo=None, core_hi=2) is None


# ---- history gauge ----------------------------------------------------------------

def test_history_gauge_needs_enough_history():
    assert dayview.history_gauge([50, 51, 52], 50) is None
    g = dayview.history_gauge(list(range(40, 60)), 50)
    assert g is not None and g["n"] == 20


def test_history_gauge_reports_delta_against_the_personal_median():
    g = dayview.history_gauge([60] * 10, 66)
    assert g["median"] == 60
    assert g["delta"] == 6


def test_history_gauge_ignores_missing_days():
    g = dayview.history_gauge([50, None, 52, None, 54, 56, 58, 60, 62, 64], 55)
    assert g["n"] == 8


# ---- HRV gauge --------------------------------------------------------------------

def test_hrv_gauge_uses_garmin_baseline_and_carries_extra_marks():
    g = dayview.hrv_gauge(31, baseline_low=31, baseline_high=37,
                          weekly_avg=33, night_high=45)
    assert g["core_lo"] == 31 and g["core_hi"] == 37
    assert [m["label"] for m in g["marks"]] == ["тижд.", "макс"]
    assert all(0 <= m["pos"] <= 100 for m in g["marks"])


def test_hrv_gauge_none_without_a_baseline():
    assert dayview.hrv_gauge(31, baseline_low=None, baseline_high=None) is None


# ---- sleep ------------------------------------------------------------------------

def test_sleep_segments_are_proportional_and_skip_absent_stages():
    segs = dayview.sleep_segments(deep=1, rem=1, light=2, awake=None)
    assert [s["key"] for s in segs] == ["deep", "rem", "light"]
    assert sum(s["pct"] for s in segs) == pytest.approx(100)
    assert segs[2]["pct"] == pytest.approx(50)


def test_sleep_segments_empty_for_a_night_with_no_stage_data():
    assert dayview.sleep_segments() == []
    assert dayview.sleep_segments(deep=0, rem=0, light=0, awake=0) == []


def test_ratio_bar_reports_the_gap_and_never_overflows_the_track():
    short = dayview.ratio_bar(7.5, 9.0)
    assert short["met"] is False and short["gap"] == 1.5
    assert short["pct"] < 100

    over = dayview.ratio_bar(10.0, 9.0)
    assert over["met"] is True and over["pct"] == 100     # clipped for drawing
    assert over["share"] > 1                              # but the truth is kept

    assert dayview.ratio_bar(8, 0) is None
    assert dayview.ratio_bar(None, 9) is None


# ---- body battery -----------------------------------------------------------------

def test_battery_span_maps_onto_the_fixed_scale_and_orders_its_ends():
    s = dayview.battery_span(13, 62)
    assert (s["start"], s["low"], s["high"]) == (13, 13, 62)
    assert s["width"] == pytest.approx(49)
    assert dayview.battery_span(62, 13)["low"] == 13      # swapped input still works
    assert dayview.battery_span(None, 62) is None


# ---- enum strings -----------------------------------------------------------------

def test_humanize_translates_known_enums_and_de_shouts_the_rest():
    assert dayview.humanize("HIGHLY_INCREASED") == "сильно підвищена"
    # unmapped Garmin vocabulary must stay readable rather than shout
    assert dayview.humanize("POSITIVE_LONG_BUT_NOT_ENOUGH_REM") == \
        "Positive long but not enough rem"
    assert dayview.humanize(7) == "7"
