"""Run pace/HR series: Garmin /details parsing + the detail-page charts helper."""
from unittest.mock import patch

from app.garmin import client, service
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
    assert s[0] == {"d": 0.1, "p": 6.67, "hr": 140, "e": None}   # 100 m → 0.1 km
    assert s[1]["p"] is None          # stopped point → no pace
    assert s[2] == {"d": 1.0, "p": 5.56, "hr": 150, "e": None}


def test_fetch_activity_series_empty_on_error():
    with patch.object(client, "_api", return_value={"_error": "boom"}), \
         patch.object(client, "_cache_get", return_value=None):
        assert client.fetch_activity_series(123) == []


# ---------- EP-15: elevation ----------

_DETAILS_WITH_ELEVATION = {
    "metricDescriptors": [
        {"key": "directSpeed", "metricsIndex": 0},
        {"key": "directHeartRate", "metricsIndex": 1},
        {"key": "sumDistance", "metricsIndex": 2},
        {"key": "directElevation", "metricsIndex": 3},
    ],
    "activityDetailMetrics": [
        {"metrics": [2.5, 140.0, 100.0, 250.4]},
        {"metrics": [2.6, 142.0, 200.0, 252.0]},
    ],
}


def test_fetch_activity_series_parses_elevation_when_present():
    with patch.object(client, "_api", return_value=_DETAILS_WITH_ELEVATION), \
         patch.object(client, "_cache_get", return_value=None), \
         patch.object(client, "_cache_put"):
        s = client.fetch_activity_series(123)
    assert s[0]["e"] == 250.4 and s[1]["e"] == 252.0


def test_fetch_activity_series_uses_v2_cache_key():
    with patch.object(client, "_cache_get", return_value=None) as get, \
         patch.object(client, "_api", return_value=_DETAILS), \
         patch.object(client, "_cache_put") as put:
        client.fetch_activity_series(123)
    get.assert_called_once_with("series:v2:123")
    assert put.call_args[0][0] == "series:v2:123"


# ---------- EP-10 phase 1: cycling series (speed/power instead of pace) ----------

_RIDE_DETAILS = {
    "metricDescriptors": [
        {"key": "directSpeed", "metricsIndex": 0},
        {"key": "directHeartRate", "metricsIndex": 1},
        {"key": "sumDistance", "metricsIndex": 2},
        {"key": "directPower", "metricsIndex": 3},
    ],
    "activityDetailMetrics": [
        {"metrics": [10.0, 140.0, 1000.0, 200.0]},   # 10 m/s = 36 km/h
        {"metrics": [0.0, 120.0, 2000.0, 0.0]},       # stopped → speed 0 → spd None
        {"metrics": [8.0, 150.0, 3000.0, 180.0]},     # 8 m/s = 28.8 km/h
    ],
}


def test_fetch_activity_series_cycling_uses_speed_and_power():
    with patch.object(client, "_api", return_value=_RIDE_DETAILS), \
         patch.object(client, "_cache_get", return_value=None), \
         patch.object(client, "_cache_put"):
        s = client.fetch_activity_series(456, sport="cycling")
    assert s[0] == {"d": 1.0, "hr": 140, "e": None, "spd": 36.0, "pw": 200}
    assert s[1]["spd"] is None and s[1]["pw"] == 0
    assert s[2] == {"d": 3.0, "hr": 150, "e": None, "spd": 28.8, "pw": 180}
    assert "p" not in s[0]


def test_fetch_activity_series_cycling_missing_power_descriptor():
    details = {
        "metricDescriptors": [d for d in _RIDE_DETAILS["metricDescriptors"]
                               if d["key"] != "directPower"],
        "activityDetailMetrics": _RIDE_DETAILS["activityDetailMetrics"],
    }
    with patch.object(client, "_api", return_value=details), \
         patch.object(client, "_cache_get", return_value=None), \
         patch.object(client, "_cache_put"):
        s = client.fetch_activity_series(789, sport="cycling")
    assert all(p["pw"] is None for p in s)


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


def test_run_charts_builds_speed_and_power_for_a_ride():
    series = [
        {"d": 0.0, "spd": 30.0, "pw": 180, "hr": 130},
        {"d": 5.0, "spd": 35.0, "pw": 210, "hr": 145},
        {"d": 10.0, "spd": 28.0, "pw": 160, "hr": 150},
    ]
    charts, first, last = _run_charts(series)
    labels = [c["label"] for c in charts]
    assert labels == ["Швидкість, км/год", "Потужність, Вт", "Пульс"]
    assert first == "0.0 км" and last == "10.0 км"
    fmts = {c["label"]: c["fmt"] for c in charts}
    assert fmts["Швидкість, км/год"] == "speed" and fmts["Потужність, Вт"] == "power"


def test_segments_capture_pace_and_hr_drift():
    from app.analysis.service import _segments

    # speeds up over the run, HR drifts up
    series = [{"d": i * 0.1, "p": 7.0 - i * 0.04, "hr": 120 + i} for i in range(12)]
    segs = _segments(series, n=4)
    assert 2 <= len(segs) <= 6
    assert all(s["avg_pace"] is not None and s["avg_hr"] is not None for s in segs)
    assert segs[0]["avg_pace"] > segs[-1]["avg_pace"]   # negative split captured
    assert segs[0]["avg_hr"] < segs[-1]["avg_hr"]       # HR drift captured


def test_segments_capture_speed_and_power_for_a_ride():
    from app.analysis.service import _segments

    series = [{"d": i * 1.0, "spd": 25.0 + i, "pw": 150 + i * 2, "hr": 130 + i}
              for i in range(12)]
    segs = _segments(series, n=4)
    assert all("avg_speed_kmh" in s and "avg_power_w" in s and "avg_hr" in s for s in segs)
    assert "avg_pace" not in segs[0]
    assert segs[0]["avg_speed_kmh"] < segs[-1]["avg_speed_kmh"]


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


def test_activity_payload_cycling_uses_speed_not_pace():
    from types import SimpleNamespace

    from app.analysis.service import activity_payload

    ride = SimpleNamespace(
        type="cycling", date="2026-06-24", dur_min=60.0, dist_km=30.0,
        avg_hr=140, max_hr=165, load=90.0, exercises=None,
        series=[{"d": 0.0, "spd": 28.0, "pw": 180, "hr": 130},
                {"d": 15.0, "spd": 32.0, "pw": 200, "hr": 145},
                {"d": 30.0, "spd": 30.0, "pw": 190, "hr": 150}],
    )
    p = activity_payload(ride)
    assert p["type"] == "cycling" and "segments" in p
    assert p["avg_speed_kmh"] == 30.0     # 30 km / 1h
    assert "avg_pace" not in p
    assert all("avg_speed_kmh" in s for s in p["segments"])


def test_activity_payload_includes_step_match_when_present():
    from types import SimpleNamespace

    from app.analysis.service import activity_payload

    run = SimpleNamespace(
        type="running", date="2026-06-24", dur_min=30.0, dist_km=5.0,
        avg_hr=140, max_hr=155, load=80.0, exercises=None, series=None,
        step_match={"steps_hit": 6, "steps_total": 8, "misses": []},
    )
    p = activity_payload(run)
    assert p["step_match"] == {"steps_hit": 6, "steps_total": 8, "misses": []}


def test_activity_payload_omits_step_match_when_absent():
    from types import SimpleNamespace

    from app.analysis.service import activity_payload

    run = SimpleNamespace(
        type="running", date="2026-06-24", dur_min=30.0, dist_km=5.0,
        avg_hr=140, max_hr=155, load=80.0, exercises=None, series=None,
    )
    p = activity_payload(run)
    assert "step_match" not in p


# ---------- fetch_activity_splits (NF-14) ----------

_SPLITS = {
    "lapDTOs": [
        {"distance": 1000.0, "duration": 300.0, "averageSpeed": 3.333333},  # 5:00/km
        {"distance": 400.0, "duration": 88.0, "averageSpeed": 4.545455},    # ~3:40/km
        {"distance": 200.0, "duration": 60.0},                              # no speed field
    ],
}


def test_fetch_activity_splits_parses_lap_dtos():
    with patch.object(client, "_api", return_value=_SPLITS), \
         patch.object(client, "_cache_get", return_value=None), \
         patch.object(client, "_cache_put"):
        laps = client.fetch_activity_splits(999)
    assert len(laps) == 3
    assert laps[0]["dist_m"] == 1000.0
    assert laps[0]["pace_min_km"] == round((1000.0 / 3.333333) / 60.0, 3)
    # third lap has no averageSpeed — falls back to distance/duration
    assert laps[2]["pace_min_km"] == round((60.0 / 60.0) / (200.0 / 1000.0), 3)


def test_fetch_activity_splits_empty_on_error():
    with patch.object(client, "_api", return_value={"_error": "boom"}), \
         patch.object(client, "_cache_get", return_value=None):
        assert client.fetch_activity_splits(999) == []


def test_fetch_activity_splits_uses_cache():
    with patch.object(client, "_cache_get", return_value=[{"dist_m": 1.0}]), \
         patch.object(client, "_api") as api:
        laps = client.fetch_activity_splits(999)
    assert laps == [{"dist_m": 1.0}]
    api.assert_not_called()


# ---------- EP-10 phase 1: _activity_rows fetches series for cycling too ----------

def test_activity_rows_fetches_series_for_cycling_and_running_not_others():
    acts = [
        {"activityId": 1, "activityType": {"typeKey": "running"},
         "startTimeLocal": "2026-06-24 08:00:00", "duration": 1800, "distance": 5000},
        {"activityId": 2, "activityType": {"typeKey": "road_biking"},
         "startTimeLocal": "2026-06-24 09:00:00", "duration": 3600, "distance": 30000},
        {"activityId": 3, "activityType": {"typeKey": "swimming"},
         "startTimeLocal": "2026-06-24 10:00:00", "duration": 1200, "distance": 1000},
    ]
    with patch.object(client, "fetch_activities", return_value=acts), \
         patch.object(client, "fetch_activity_series") as fetch_series, \
         patch("time.sleep"):
        fetch_series.side_effect = \
            lambda aid, sport="running", force=False: [{"d": 1.0, "hr": 140}]
        rows = service._activity_rows(limit=10)
    by_id = {aid: row for aid, row in rows}
    assert "series" in by_id[1] and "series" in by_id[2]
    assert "series" not in by_id[3]
    kwargs_by_call = {c.args[0]: c.kwargs.get("sport") for c in fetch_series.call_args_list}
    assert kwargs_by_call[1] == "running" and kwargs_by_call[2] == "cycling"
