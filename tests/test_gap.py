"""EP-15: grade-adjusted pace — pure math + wiring into segments/matching/activity payload."""
from app import gap

# ---------- core math ----------

def test_flat_grade_gap_equals_raw_pace():
    """AC: a flat run's GAP must be within <2% of the raw pace (sanity test)."""
    raw = 6.0
    adjusted = gap.gap_pace_min_km(raw, 0.0)
    assert abs(adjusted - raw) / raw < 0.02


def test_uphill_gap_is_faster_than_raw_pace():
    """The ticket's own example: 6:10/km raw on a climb reads as ~5:30 GAP-equivalent —
    GAP must always come out faster (lower minutes/km) than the raw split uphill."""
    raw = 6.17  # 6:10/km
    adjusted = gap.gap_pace_min_km(raw, 8.0)  # 8% grade
    assert adjusted < raw


def test_downhill_gap_is_slower_than_raw_pace():
    adjusted = gap.gap_pace_min_km(6.0, -8.0)
    assert adjusted > 6.0


def test_gap_pace_none_in_none_out():
    assert gap.gap_pace_min_km(None, 5.0) is None
    assert gap.gap_pace_min_km(6.0, None) is None


def test_extreme_grade_is_clamped_not_explosive():
    """A bogus 90% grade (bad GPS/barometer point) must not blow the adjustment up into
    nonsense — the cost ratio is clamped to a sane physiological range."""
    adjusted = gap.gap_pace_min_km(6.0, 90.0)
    assert 2.0 < adjusted < 15.0


# ---------- elevation smoothing ----------

def test_smooth_elevation_fills_gaps_and_averages():
    values = [100.0, None, 104.0, None, None, 110.0]
    smoothed = gap.smooth_elevation(values, window=3)
    assert len(smoothed) == len(values)
    assert all(v is not None for v in smoothed)


def test_smooth_elevation_all_none_stays_all_none():
    assert gap.smooth_elevation([None, None, None]) == [None, None, None]


def test_smooth_elevation_empty():
    assert gap.smooth_elevation([]) == []


def test_elevation_delta_sums_gain_and_loss():
    # up 10, down 5, up 3
    smoothed = [100.0, 110.0, 105.0, 108.0]
    gain, loss = gap.elevation_delta(smoothed)
    assert gain == 13.0
    assert loss == 5.0


def test_elevation_delta_skips_none():
    gain, loss = gap.elevation_delta([100.0, None, 105.0])
    assert gain == 5.0 and loss == 0.0


def test_segment_grade_pct_uphill():
    grade = gap.segment_grade_pct([100.0, 150.0], dist_km=0.5)  # 50m over 500m = 10%
    assert grade == 10.0


def test_segment_grade_pct_needs_two_points_and_distance():
    assert gap.segment_grade_pct([100.0], dist_km=0.5) is None
    assert gap.segment_grade_pct([100.0, 150.0], dist_km=None) is None


def test_is_hilly_threshold():
    assert gap.is_hilly(gain_m=150.0, dist_km=10.0) is True   # 15 m/km > 10
    assert gap.is_hilly(gain_m=50.0, dist_km=10.0) is False   # 5 m/km
    assert gap.is_hilly(gain_m=10.0, dist_km=0.0) is False    # no distance → not hilly


# ---------- whole-activity summary ----------

def _hilly_series():
    # 5 km climbing steadily ~100 m (20 m/km — well over the 10 m/km hilly threshold)
    return [
        {"d": i * 1.0, "p": 6.0, "hr": 140, "e": 100.0 + i * 20.0}
        for i in range(6)
    ]


def _flat_series():
    return [{"d": i * 1.0, "p": 6.0, "hr": 140, "e": 100.0} for i in range(6)]


def _no_elevation_series():
    return [{"d": i * 1.0, "p": 6.0, "hr": 140} for i in range(6)]


def test_activity_elevation_summary_none_without_elevation_data():
    assert gap.activity_elevation_summary(_no_elevation_series()) is None
    assert gap.activity_elevation_summary([]) is None
    assert gap.activity_elevation_summary(None) is None


def test_activity_elevation_summary_hilly():
    summary = gap.activity_elevation_summary(_hilly_series())
    assert summary["gain_m"] > 0
    assert summary["hilly"] is True


def test_activity_elevation_summary_flat_not_hilly():
    summary = gap.activity_elevation_summary(_flat_series())
    assert summary["hilly"] is False


def test_activity_gap_pace_min_km_hilly():
    pace = gap.activity_gap_pace_min_km(_hilly_series())
    assert pace is not None
    assert pace < 6.0   # uphill throughout → GAP reads faster than raw 6.0


def test_activity_gap_pace_min_km_needs_elevation():
    assert gap.activity_gap_pace_min_km(_no_elevation_series()) is None


def test_effective_pace_min_km_uses_gap_when_hilly():
    raw = 6.0
    effective = gap.effective_pace_min_km(_hilly_series(), raw)
    assert effective < raw


def test_effective_pace_min_km_falls_back_to_raw_when_flat():
    raw = 6.0
    assert gap.effective_pace_min_km(_flat_series(), raw) == raw


def test_effective_pace_min_km_falls_back_to_raw_when_no_series():
    assert gap.effective_pace_min_km(None, 6.0) == 6.0
    assert gap.effective_pace_min_km([], 6.0) == 6.0


# ---------- _segments wiring (EP-15) ----------

def test_segments_include_gap_fields_for_hilly_run():
    from app.analysis.reports import _segments

    segs = _segments(_hilly_series(), n=3)
    assert any("gap_pace" in s for s in segs)
    assert any(s.get("grade_pct", 0) > 0 for s in segs)
    assert any("gain_m" in s for s in segs)


def test_segments_omit_gap_fields_without_elevation():
    from app.analysis.reports import _segments

    segs = _segments(_no_elevation_series(), n=3)
    assert all("gap_pace" not in s and "grade_pct" not in s for s in segs)


def test_segments_flat_run_no_meaningful_grade_shift():
    from app.analysis.reports import _segments

    segs = _segments(_flat_series(), n=3)
    # flat series: grade should be (near) zero, gap_pace ~= avg_pace
    for s in segs:
        if "gap_pace" in s:
            assert abs(s["gap_pace"] - s["avg_pace"]) < 0.05


# ---------- activity_payload wiring ----------

def test_activity_payload_includes_elevation_summary_when_hilly():
    from types import SimpleNamespace

    from app.analysis.reports import activity_payload

    run = SimpleNamespace(
        type="running", date="2026-07-01", dur_min=35.0, dist_km=6.0,
        avg_hr=150, max_hr=170, load=90.0, exercises=None,
        series=_hilly_series(),
    )
    p = activity_payload(run)
    assert p["hilly"] is True
    assert p["elevation_gain_m"] > 0


def test_activity_payload_omits_elevation_without_data():
    from types import SimpleNamespace

    from app.analysis.reports import activity_payload

    run = SimpleNamespace(
        type="running", date="2026-07-01", dur_min=35.0, dist_km=6.0,
        avg_hr=150, max_hr=170, load=90.0, exercises=None,
        series=_no_elevation_series(),
    )
    p = activity_payload(run)
    assert "hilly" not in p and "elevation_gain_m" not in p
