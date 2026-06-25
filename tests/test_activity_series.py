"""Run pace/HR series: Garmin /details parsing + the detail-page charts helper."""
from unittest.mock import patch

from app.garmin import client
from app.routers.admin import _run_charts

_DETAILS = {
    "metricDescriptors": [
        {"key": "directSpeed", "metricsIndex": 0},
        {"key": "directHeartRate", "metricsIndex": 1},
        {"key": "sumDistance", "metricsIndex": 2},
    ],
    "activityDetailMetrics": [
        {"metrics": [2.5, 140.0, 100.0]},    # 2.5 m/s → 1000/2.5/60 = 6.67 min/km
        {"metrics": [0.0, 120.0, 500.0]},    # stopped → speed 0 → pace None
        {"metrics": [3.0, 150.0, 1000.0]},   # 1000/3/60 = 5.56, 1000 m → 1.0 km
    ],
}


def test_fetch_activity_series_parses_by_descriptor_key():
    # indices are resolved from metricDescriptors, not hard-coded
    with patch.object(client, "_api", return_value=_DETAILS), \
         patch.object(client, "_cache_get", return_value=None), \
         patch.object(client, "_cache_put"):
        s = client.fetch_activity_series(123)
    assert s[0] == {"d": 0.1, "p": 6.67, "hr": 140}   # 100 m → 0.1 km
    assert s[1]["p"] is None          # stopped point → no pace
    assert s[2] == {"d": 1.0, "p": 5.56, "hr": 150}


def test_fetch_activity_series_empty_on_error():
    with patch.object(client, "_api", return_value={"_error": "boom"}), \
         patch.object(client, "_cache_get", return_value=None):
        assert client.fetch_activity_series(123) == []


def test_run_charts_builds_pace_and_hr():
    series = [
        {"d": 0.0, "p": 7.0, "hr": 120},
        {"d": 0.5, "p": 6.5, "hr": 140},
        {"d": 1.0, "p": 6.0, "hr": 150},
    ]
    charts, first, last = _run_charts(series)
    labels = [c["label"] for c in charts]
    assert labels == ["Темп, хв/км", "Пульс"]
    assert first == "0.0 км" and last == "1.0 км"
    assert all("points" in c["s"] for c in charts)


def test_run_charts_empty():
    assert _run_charts([]) == ([], "", "")


def test_segments_capture_pace_and_hr_drift():
    from app.analysis.service import _segments

    # speeds up over the run, HR drifts up
    series = [{"d": i * 0.1, "p": 7.0 - i * 0.04, "hr": 120 + i} for i in range(12)]
    segs = _segments(series, n=4)
    assert 2 <= len(segs) <= 6
    assert all(s["avg_pace"] is not None and s["avg_hr"] is not None for s in segs)
    assert segs[0]["avg_pace"] > segs[-1]["avg_pace"]   # negative split captured
    assert segs[0]["avg_hr"] < segs[-1]["avg_hr"]       # HR drift captured


def test_activity_payload_run_vs_strength():
    from types import SimpleNamespace

    from app.analysis.service import activity_payload

    run = SimpleNamespace(
        type="running", date="2026-06-24", dur_min=30.0, dist_km=5.0,
        avg_hr=140, max_hr=155, load=80.0, exercises=None,
        series=[{"d": 0.0, "p": 7.0, "hr": 120}, {"d": 2.5, "p": 6.5, "hr": 140},
                {"d": 5.0, "p": 6.0, "hr": 150}],
    )
    p = activity_payload(run)
    assert p["type"] == "running" and "segments" in p
    assert p["avg_pace"] == 6.0          # 30 min / 5 km

    strength = SimpleNamespace(
        type="strength_training", date="2026-06-23", dur_min=45.0, dist_km=0.0,
        avg_hr=110, max_hr=140, load=60.0, exercises={"sets": {"присідання": 4}},
        series=None,
    )
    p2 = activity_payload(strength)
    assert "segments" not in p2 and p2["exercises"]
