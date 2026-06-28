"""Weather helpers — parsing + error tolerance, with requests mocked (no network)."""
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
