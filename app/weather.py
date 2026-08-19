"""Weather lookup for the morning report (Open-Meteo — free, no API key).

Two pure helpers, both network-bound and fully error-tolerant (return ``None`` on any
failure so the report still goes out without weather):

- :func:`geocode` — turn a city name typed in /settings into (lat, lon, label); used
  once on save so we store coordinates, not a name to re-resolve every morning.
- :func:`fetch_forecast` — today's compact forecast for a lat/lon: daily min/max +
  feels-like, precipitation, wind, a short condition, and a few daytime hourly slots so
  the analyst can advise on *when* to run. Shaped small (like the Garmin payload) to keep
  token cost down.
- :func:`fetch_forecast_week` — the same compact daily shape for the next 7 days (no
  hourly), for the weather-aware weekly planning check (EP-13).
- :func:`find_weather_conflicts` — a pure, network-free filter that flags key sessions
  (tempo/intervals/long) landing on an extreme-weather day, so we only call the LLM when
  there's an actual conflict.

All three network helpers share :func:`_get_json`, which retries the transient side of
Open-Meteo (timeouts, 429, 5xx) with exponential backoff before giving up.
"""
import datetime as dt
import logging
import time
from typing import Iterable, Optional, Sequence, Tuple

import requests
from fastapi.concurrency import run_in_threadpool

logger = logging.getLogger("weather")

_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 8  # seconds — never hold up the morning job on a slow weather API

# Open-Meteo is a free public service and answers 429/5xx (503 in particular) for short
# stretches; a single blip used to lose a whole day of weather (the morning report drops
# the block, EP-13's planning check goes silent). Retry only the failures a retry can fix
# — never a 4xx that says our request is wrong, and never a parse error.
_RETRIES = 2                       # extra attempts after the first
_BACKOFF_S = 1.0                   # doubled per retry: 1s, 2s
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def _is_transient(exc: Exception) -> bool:
    """True for a failure worth retrying: a timeout / dropped connection, or an HTTP
    status Open-Meteo hands out while it is overloaded."""
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status in _TRANSIENT_STATUS


def _get_json(url: str, params: dict, what: str) -> Optional[dict]:
    """GET ``url`` and return the decoded JSON object, or ``None`` on any failure —
    every caller degrades to "no weather" rather than propagating. Transient errors are
    retried ``_RETRIES`` times with exponential backoff; anything else fails at once."""
    for attempt in range(_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=_TIMEOUT)
            r.raise_for_status()
            return r.json() or {}
        except Exception as e:
            if attempt < _RETRIES and _is_transient(e):
                backoff = _BACKOFF_S * (2 ** attempt)
                logger.warning(f"{what} transient error ({e}) — retry "
                               f"{attempt + 1}/{_RETRIES} in {backoff:.0f}s")
                time.sleep(backoff)
                continue
            logger.warning(f"{what} failed: {e}")
            return None
    return None   # unreachable — every path inside the loop returns or continues


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

# WMO codes that mean ice on the ground / freezing precipitation — an EP-13 conflict
# regardless of temperature (freezing drizzle/rain, all snow, ice pellets).
_ICY_CODES = frozenset({56, 57, 66, 67, 71, 73, 75, 77, 85, 86})

# The daily forecast fields we pull for both today and the week (kept identical so the
# LLM sees a consistent shape).
_DAILY_PARAMS = ("temperature_2m_max,temperature_2m_min,apparent_temperature_max,"
                 "precipitation_sum,precipitation_probability_max,"
                 "wind_speed_10m_max,weather_code")


def geocode(name: str) -> Optional[tuple]:
    """Resolve a place name to ``(latitude, longitude, label)`` via Open-Meteo's
    geocoder, or ``None`` if not found / on error. ``label`` is the canonical
    "City, Country" we store back so the user sees what we matched."""
    name = (name or "").strip()
    if not name:
        return None
    data = _get_json(
        _GEO_URL,
        {"name": name, "count": 1, "language": "uk", "format": "json"},
        f"GEOCODE {name!r}",
    )
    if data is None:
        return None
    results = data.get("results") or []
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
    data = _get_json(
        _FORECAST_URL,
        {
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1,
            "daily": _DAILY_PARAMS,
            "hourly": ("temperature_2m,apparent_temperature,"
                       "precipitation_probability,wind_speed_10m"),
        },
        f"FORECAST today {lat},{lon}",
    )
    if data is None:
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


async def forecast_for_user(user) -> Optional[dict]:
    """Today's forecast for a user's stored location, or ``None`` if no location is set
    or Open-Meteo errors. Async wrapper over :func:`fetch_forecast` (offloaded to a
    threadpool) shared by every daily-report channel — the morning job, bot ``/report``
    and web ``/report.json`` (ST-03) — so the lookup lives in one place."""
    if user.latitude is None or user.longitude is None:
        return None
    wx = await run_in_threadpool(fetch_forecast, user.latitude, user.longitude)
    if wx:
        logger.info(f"WEATHER user={user.id}: {wx.get('summary')} "
                    f"{wx.get('t_min_c')}–{wx.get('t_max_c')}°C")
    return wx


def _day_row(daily: dict, i: int) -> dict:
    """One day's compact aggregate from the ``daily`` block at index ``i`` (same shape
    as :func:`fetch_forecast` minus the hourly slots). Keeps ``code`` so the conflict
    filter can spot freezing precipitation."""
    def d(key):
        vals = daily.get(key) or []
        return vals[i] if 0 <= i < len(vals) else None

    code = d("weather_code")
    return {
        "date": d("time"),
        "t_min_c": _r(d("temperature_2m_min")),
        "t_max_c": _r(d("temperature_2m_max")),
        "feels_max_c": _r(d("apparent_temperature_max")),
        "precip_mm": _r(d("precipitation_sum"), 1),
        "precip_prob_pct": d("precipitation_probability_max"),
        "wind_max_kmh": _r(d("wind_speed_10m_max")),
        "code": code,
        "summary": _WMO.get(code, f"код {code}") if code is not None else None,
    }


def fetch_forecast_week(lat: float, lon: float, days: int = 7) -> Optional[list]:
    """The next ``days`` days' compact daily forecast for ``lat``/``lon`` (local
    timezone), or ``None`` on error. One dict per day (see :func:`_day_row`); no hourly
    slots — used by the weather-aware weekly planning check (EP-13)."""
    data = _get_json(
        _FORECAST_URL,
        {
            "latitude": lat, "longitude": lon, "timezone": "auto",
            "forecast_days": days, "daily": _DAILY_PARAMS,
        },
        f"FORECAST week {lat},{lon}",
    )
    if data is None:
        return None

    daily = data.get("daily") or {}
    n = len(daily.get("time") or [])
    return [_day_row(daily, i) for i in range(n)]


def find_weather_conflicts(
    forecast: Iterable[dict],
    sessions: Sequence[Tuple[str, Optional[str]]],
    *,
    today: dt.date,
    decision_days: int,
    heavy_types: Iterable[str],
    heat_feels_c: float,
    rain_prob_pct: float,
    wind_kmh: float,
) -> list:
    """Pure, network-free filter (EP-13): flag key sessions (``heavy_types`` — tempo/
    intervals/long) in the next ``decision_days`` that land on an extreme-weather day.

    ``sessions`` is ``(date_iso, type)`` pairs. Returns a list of
    ``{date, type, reasons}`` — one per conflicting session (``reasons`` is a short
    Ukrainian list). Empty list ⇒ no conflict ⇒ the caller stays silent and never calls
    the LLM. Only looks ``decision_days`` ahead because the forecast lies further out."""
    by_date = {d.get("date"): d for d in forecast if d.get("date")}
    window_end = today + dt.timedelta(days=decision_days)
    heavy = {t.lower() for t in heavy_types}
    out = []
    for date_s, wtype in sessions:
        if (wtype or "").lower() not in heavy:
            continue
        try:
            d = dt.date.fromisoformat(date_s)
        except (TypeError, ValueError):
            continue
        if not (today <= d <= window_end):
            continue
        day = by_date.get(date_s)
        if not day:
            continue
        reasons = []
        feels = day.get("feels_max_c")
        if feels is not None and feels >= heat_feels_c:
            reasons.append(f"спека ~{feels}°C (відчувається)")
        prob = day.get("precip_prob_pct")
        if prob is not None and prob >= rain_prob_pct:
            reasons.append(f"дощ {prob}%")
        wind = day.get("wind_max_kmh")
        if wind is not None and wind >= wind_kmh:
            reasons.append(f"вітер {wind} км/год")
        t_max = day.get("t_max_c")
        if day.get("code") in _ICY_CODES or (t_max is not None and t_max <= 0):
            reasons.append("ожеледь/мороз")
        if reasons:
            out.append({"date": date_s, "type": wtype, "reasons": reasons})
    return out


def _r(v, ndigits: int = 0):
    if not isinstance(v, (int, float)):
        return None
    return round(v) if ndigits == 0 else round(v, ndigits)
