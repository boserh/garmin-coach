"""Weather helpers — parsing + error tolerance, with requests mocked (no network)."""
from datetime import date

from app import weather


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _patch_get(monkeypatch, payload=None, exc=None):
    def fake_get(url, params=None, timeout=None):
        if exc is not None:
            raise exc
        return _Resp(payload)

    monkeypatch.setattr(weather.requests, "get", fake_get)


def test_geocode_resolves_to_coords_and_label(monkeypatch):
    _patch_get(monkeypatch, {"results": [
        {"name": "Wrocław", "country": "Польща", "latitude": 51.1, "longitude": 17.03}]})
    lat, lon, label = weather.geocode("wroclaw")
    assert (lat, lon) == (51.1, 17.03)
    assert label == "Wrocław, Польща"


def test_geocode_empty_or_no_results_returns_none(monkeypatch):
    assert weather.geocode("   ") is None          # no request for blank input
    _patch_get(monkeypatch, {"results": []})
    assert weather.geocode("nowhere-xyz") is None


def test_geocode_swallows_errors(monkeypatch):
    _patch_get(monkeypatch, exc=RuntimeError("boom"))
    assert weather.geocode("wroclaw") is None


def test_fetch_forecast_builds_compact_today(monkeypatch):
    hours = list(range(24))
    _patch_get(monkeypatch, {
        "daily": {
            "time": ["2026-06-28"],
            "temperature_2m_min": [14.4], "temperature_2m_max": [27.6],
            "apparent_temperature_max": [29.1], "precipitation_sum": [2.16],
            "precipitation_probability_max": [60], "wind_speed_10m_max": [18.7],
            "weather_code": [61],
        },
        "hourly": {
            "temperature_2m": [10 + h for h in hours],
            "apparent_temperature": [11 + h for h in hours],
            "precipitation_probability": [h for h in hours],
            "wind_speed_10m": [h * 1.0 for h in hours],
        },
    })
    wx = weather.fetch_forecast(51.1, 17.03)
    assert wx["t_min_c"] == 14 and wx["t_max_c"] == 28      # rounded
    assert wx["feels_max_c"] == 29
    assert wx["precip_mm"] == 2.2 and wx["precip_prob_pct"] == 60
    assert wx["wind_max_kmh"] == 19
    assert wx["summary"] == "невеликий дощ"                 # WMO 61
    # six daytime slots (6,9,12,15,18,21), indexed by hour-of-day
    assert [s["h"] for s in wx["hourly"]] == [6, 9, 12, 15, 18, 21]
    six = wx["hourly"][0]
    assert six["t_c"] == 16 and six["feels_c"] == 17 and six["precip_pct"] == 6


def test_fetch_forecast_swallows_errors(monkeypatch):
    _patch_get(monkeypatch, exc=RuntimeError("api down"))
    assert weather.fetch_forecast(51.1, 17.03) is None


# ---------- weekly forecast + conflict filter (EP-13) ----------

def test_fetch_forecast_week_builds_daily_rows(monkeypatch):
    _patch_get(monkeypatch, {"daily": {
        "time": ["2026-07-09", "2026-07-10", "2026-07-11"],
        "temperature_2m_min": [18.2, 19.0, 20.5],
        "temperature_2m_max": [28.4, 34.1, 31.0],
        "apparent_temperature_max": [30.0, 36.6, 33.2],
        "precipitation_sum": [0.0, 0.0, 5.3],
        "precipitation_probability_max": [10, 5, 80],
        "wind_speed_10m_max": [12.0, 15.0, 45.0],
        "weather_code": [1, 0, 82],
    }})
    week = weather.fetch_forecast_week(51.1, 17.03)
    assert [d["date"] for d in week] == ["2026-07-09", "2026-07-10", "2026-07-11"]
    assert week[1]["feels_max_c"] == 37 and week[1]["t_max_c"] == 34
    assert week[2]["precip_prob_pct"] == 80 and week[2]["summary"] == "сильні зливи"
    assert "hourly" not in week[0]   # week rows are daily-only


def test_fetch_forecast_week_swallows_errors(monkeypatch):
    _patch_get(monkeypatch, exc=RuntimeError("api down"))
    assert weather.fetch_forecast_week(51.1, 17.03) is None


_HEAVY = {"tempo", "intervals", "long"}


def _week():
    return [
        {"date": "2026-07-09", "t_max_c": 26, "feels_max_c": 28,
         "precip_prob_pct": 10, "wind_max_kmh": 12, "code": 1},
        {"date": "2026-07-10", "t_max_c": 34, "feels_max_c": 36,   # heat
         "precip_prob_pct": 5, "wind_max_kmh": 15, "code": 0},
        {"date": "2026-07-11", "t_max_c": 20, "feels_max_c": 21,   # rain + wind
         "precip_prob_pct": 85, "wind_max_kmh": 48, "code": 82},
    ]


def test_conflicts_flags_key_session_on_extreme_day():
    today = date(2026, 7, 9)
    conflicts = weather.find_weather_conflicts(
        _week(), [("2026-07-10", "intervals")], today=today, decision_days=3,
        heavy_types=_HEAVY, heat_feels_c=30, rain_prob_pct=70, wind_kmh=40)
    assert len(conflicts) == 1
    assert conflicts[0]["date"] == "2026-07-10"
    assert any("спека" in r for r in conflicts[0]["reasons"])


def test_conflicts_reports_multiple_reasons():
    today = date(2026, 7, 9)
    conflicts = weather.find_weather_conflicts(
        _week(), [("2026-07-11", "long")], today=today, decision_days=3,
        heavy_types=_HEAVY, heat_feels_c=30, rain_prob_pct=70, wind_kmh=40)
    reasons = conflicts[0]["reasons"]
    assert any("дощ" in r for r in reasons) and any("вітер" in r for r in reasons)


def test_conflicts_ignores_easy_sessions_and_calm_days():
    today = date(2026, 7, 9)
    # easy session on the hot day → not a key session; key session on the calm day → fine
    conflicts = weather.find_weather_conflicts(
        _week(), [("2026-07-10", "easy"), ("2026-07-09", "tempo")], today=today,
        decision_days=3, heavy_types=_HEAVY, heat_feels_c=30, rain_prob_pct=70, wind_kmh=40)
    assert conflicts == []


def test_conflicts_ignores_sessions_past_decision_window():
    today = date(2026, 7, 9)
    # the hot key session is 5 days out — beyond decision_days=3 → no conflict
    conflicts = weather.find_weather_conflicts(
        [{"date": "2026-07-14", "t_max_c": 35, "feels_max_c": 37,
          "precip_prob_pct": 0, "wind_max_kmh": 10, "code": 0}],
        [("2026-07-14", "intervals")], today=today, decision_days=3,
        heavy_types=_HEAVY, heat_feels_c=30, rain_prob_pct=70, wind_kmh=40)
    assert conflicts == []


def test_conflicts_flags_freezing_precip_by_code():
    today = date(2026, 1, 9)
    week = [{"date": "2026-01-10", "t_max_c": 2, "feels_max_c": -3,
             "precip_prob_pct": 20, "wind_max_kmh": 10, "code": 66}]  # freezing rain
    conflicts = weather.find_weather_conflicts(
        week, [("2026-01-10", "long")], today=today, decision_days=3,
        heavy_types=_HEAVY, heat_feels_c=30, rain_prob_pct=70, wind_kmh=40)
    assert any("ожеледь" in r for r in conflicts[0]["reasons"])
