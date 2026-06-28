"""Weather lookup for the morning report (Open-Meteo — free, no API key).

Two pure helpers, both network-bound and fully error-tolerant (return ``None`` on any
failure so the report still goes out without weather):

- :func:`geocode` — turn a city name typed in /settings into (lat, lon, label); used
  once on save so we store coordinates, not a name to re-resolve every morning.
- :func:`fetch_forecast` — today's compact forecast for a lat/lon: daily min/max +
  feels-like, precipitation, wind, a short condition, and a few daytime hourly slots so
  the analyst can advise on *when* to run. Shaped small (like the Garmin payload) to keep
  token cost down.
"""
import logging
from typing import Optional

import requests

logger = logging.getLogger("weather")

_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 8  # seconds — never hold up the morning job on a slow weather API

# WMO weather codes → short Ukrainian condition (the codes Open-Meteo returns).
_WMO = {
    0: "ясно", 1: "переважно ясно", 2: "мінлива хмарність", 3: "похмуро",
    45: "туман", 48: "паморозь",
    51: "слабка мряка", 53: "мряка", 55: "густа мряка",
    56: "крижана мряка", 57: "густа крижана мряка",
    61: "невеликий дощ", 63: "дощ", 65: "сильний дощ",
    66: "крижаний дощ", 67: "сильний крижаний дощ",
    71: "невеликий сніг", 73: "сніг", 75: "сильний сніг", 77: "снігова крупа",
    80: "короткочасний дощ", 81: "зливи", 82: "сильні зливи",
    85: "снігові зливи", 86: "сильні снігові зливи",
    95: "гроза", 96: "гроза з градом", 99: "сильна гроза з градом",
}

_HOURS = (6, 9, 12, 15, 18, 21)  # daytime slots we surface for run-timing advice


def geocode(name: str) -> Optional[tuple]:
    """Resolve a place name to ``(latitude, longitude, label)`` via Open-Meteo's
    geocoder, or ``None`` if not found / on error. ``label`` is the canonical
    "City, Country" we store back so the user sees what we matched."""
    name = (name or "").strip()
    if not name:
        return None
    try:
        r = requests.get(
            _GEO_URL,
            params={"name": name, "count": 1, "language": "uk", "format": "json"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
    except Exception as e:
        logger.warning(f"GEOCODE failed for {name!r}: {e}")
        return None
    if not results:
        return None
    g = results[0]
    lat, lon = g.get("latitude"), g.get("longitude")
    if lat is None or lon is None:
        return None
    label = ", ".join(p for p in (g.get("name"), g.get("country")) if p)
    return float(lat), float(lon), label or name


def _slot(hourly: dict, hour: int) -> dict:
    # With forecast_days=1 + timezone=auto, the hourly arrays start at 00:00 local,
    # so the hour of day is its own index.
    def at(key):
        vals = hourly.get(key) or []
        return vals[hour] if 0 <= hour < len(vals) else None

    return {
        "h": hour,
        "t_c": _r(at("temperature_2m")),
        "feels_c": _r(at("apparent_temperature")),
        "precip_pct": at("precipitation_probability"),
        "wind_kmh": _r(at("wind_speed_10m")),
    }


def fetch_forecast(lat: float, lon: float) -> Optional[dict]:
    """Today's compact forecast for ``lat``/``lon`` (local timezone), or ``None`` on
    error. Daily aggregates + a few daytime hourly slots; temps °C, wind km/h."""
    try:
        r = requests.get(
            _FORECAST_URL,
            params={
                "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1,
                "daily": ("temperature_2m_max,temperature_2m_min,apparent_temperature_max,"
                          "precipitation_sum,precipitation_probability_max,"
                          "wind_speed_10m_max,weather_code"),
                "hourly": ("temperature_2m,apparent_temperature,"
                           "precipitation_probability,wind_speed_10m"),
            },
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json() or {}
    except Exception as e:
        logger.warning(f"FORECAST failed for {lat},{lon}: {e}")
        return None

    daily = data.get("daily") or {}
    hourly = data.get("hourly") or {}

    def d(key):
        vals = daily.get(key) or []
        return vals[0] if vals else None

    code = d("weather_code")
    out = {
        "date": (daily.get("time") or [None])[0],
        "t_min_c": _r(d("temperature_2m_min")),
        "t_max_c": _r(d("temperature_2m_max")),
        "feels_max_c": _r(d("apparent_temperature_max")),
        "precip_mm": _r(d("precipitation_sum"), 1),
        "precip_prob_pct": d("precipitation_probability_max"),
        "wind_max_kmh": _r(d("wind_speed_10m_max")),
        "summary": _WMO.get(code, f"код {code}") if code is not None else None,
        "hourly": [_slot(hourly, h) for h in _HOURS],
    }
    return out


def _r(v, ndigits: int = 0):
    if not isinstance(v, (int, float)):
        return None
    return round(v) if ndigits == 0 else round(v, ndigits)
