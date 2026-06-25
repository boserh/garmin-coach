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
