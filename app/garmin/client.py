"""Low-level Garmin Connect fetches via the configured provider.

Endpoint URLs, the disk cache for immutable assets (exercise sets, workout
details), and the exercise-name mapping are all preserved exactly from the old
``garmin_client``. Day-level caching now lives in the database (see repository),
so the per-day disk cache was dropped here.
"""
import datetime as dt
import json
import logging
import os
import time as _time
from collections import Counter

from app.core.config import settings
from app.garmin.exercise_names import EXERCISE_NAMES
from app.garmin.providers import get_provider

logger = logging.getLogger("garmin")


# ---------- HELPERS ----------

def _safe(fn, *a, **kw):
    label = a[0] if a else getattr(fn, "__name__", "call")
    t0 = _time.perf_counter()
    try:
        r = fn(*a, **kw)
        dt_ms = (_time.perf_counter() - t0) * 1000
        logger.info(f"GARMIN OK  {label}  {dt_ms:.0f}ms")
        return r
    except Exception as e:
        dt_ms = (_time.perf_counter() - t0) * 1000
        logger.warning(f"GARMIN ERR {label}  {dt_ms:.0f}ms  {type(e).__name__}: {e}")
        return {"_error": str(e)}


def _g(obj, *keys, default=None):
    cur = obj
    for k in keys:
        if cur is None:
            return default
        cur = cur.get(k) if isinstance(cur, dict) else getattr(cur, k, None)
    return cur if cur is not None else default


def _api(path: str, **kwargs):
    return get_provider().connectapi(path, **kwargs)


# ---------- DISK CACHE (stable, ID-keyed, immutable assets) ----------
# Exercise sets (never change) and workout details (rarely change) are keyed on
# stable Garmin IDs, so we cache them on disk. Keyed by "<kind>:<id>"; values are
# [data, expires_at].
GARMIN_CACHE_FILE = settings.GARMIN_CACHE_FILE
EXERCISE_TTL_S = 365 * 24 * 3600   # a completed activity's sets are immutable
WORKOUT_TTL_S = 7 * 24 * 3600      # a planned workout can be edited; refresh weekly


def _cache_load() -> dict:
    try:
        with open(GARMIN_CACHE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}  # missing or empty/corrupt — start fresh
    except Exception as e:
        logger.warning(f"GCACHE load failed: {e}")
        return {}
    now = _time.time()
    return {k: v for k, v in raw.items() if v[1] > now}


_disk_cache = _cache_load()


def _cache_get(key: str):
    hit = _disk_cache.get(key)
    if hit and hit[1] > _time.time():
        logger.info(f"GARMIN CACHE  {key}")
        return hit[0]
    return None


def _cache_put(key: str, value, ttl_s: float) -> None:
    _disk_cache[key] = [value, _time.time() + ttl_s]
    now = _time.time()
    alive = {k: v for k, v in _disk_cache.items() if v[1] > now}
    try:
        tmp = GARMIN_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(alive, f, ensure_ascii=False)
        os.replace(tmp, GARMIN_CACHE_FILE)
    except Exception as e:
        logger.warning(f"GCACHE save failed: {e}")


# ---------- FETCHERS ----------

def fetch_sleep(date: dt.date) -> dict:
    return _safe(
        _api,
        f"/wellness-service/wellness/dailySleepData/{get_provider().username}",
        params={"date": date.isoformat(), "nonSleepBufferMinutes": 60},
    )


def fetch_hrv(date: dt.date) -> dict:
    return _safe(_api, f"/hrv-service/hrv/{date.isoformat()}")


def fetch_stress(date: dt.date) -> dict:
    return _safe(_api, f"/wellness-service/wellness/dailyStress/{date.isoformat()}")


def fetch_body_battery(date: dt.date) -> dict:
    r = _safe(
        _api,
        "/wellness-service/wellness/bodyBattery/reports/daily",
        params={"startDate": date.isoformat(), "endDate": date.isoformat()},
    )
    return r[0] if isinstance(r, list) and r else {}


def fetch_activities(limit: int = 30) -> list:
    return _safe(
        _api,
        "/activitylist-service/activities/search/activities",
        params={"start": 0, "limit": limit},
    )


def fetch_calendar(year: int, month_index: int):
    """Raw calendar for a month. Garmin's month index is 0-based."""
    return _safe(_api, f"/calendar-service/year/{year}/month/{month_index}")


# Garmin exercise NAME codes → readable names, mapped at return time so the cache
# stays language-neutral. Unknown names are logged once (so they can be added to
# exercise_names.py) and fall back to a prettified form.
_unmapped: set = set()


def _exercise_name(code: str) -> str:
    name = EXERCISE_NAMES.get(code)
    if name:
        return name
    if code not in _unmapped:
        _unmapped.add(code)
        logger.info(f"EXERCISE unmapped: {code} (add it to exercise_names.py)")
    return code.strip("_").replace("_", " ").lower()


def fetch_exercise_summary(activity_id) -> dict:
    """Specific exercises and how many active sets done in a strength workout."""
    key = f"exercise:v2:{activity_id}"
    raw = _cache_get(key)
    if raw is None:
        d = _safe(_api, f"/activity-service/activity/{activity_id}/exerciseSets")
        if isinstance(d, dict) and "_error" in d:
            return {}  # transient error — don't cache
        sets = Counter()
        total_active = 0
        for s in (_g(d, "exerciseSets") or []):
            if s.get("setType") != "ACTIVE":
                continue
            ex = (s.get("exercises") or [{}])[0]
            cat = ex.get("category")
            if cat in (None, "RUN", "UNKNOWN"):
                continue  # skip the warm-up jog / unrecognized sets
            total_active += 1
            sets[ex.get("name") or cat] += 1
        raw = {} if not sets else {"active_sets": total_active,
                                   "sets": dict(sets.most_common())}
        _cache_put(key, raw, EXERCISE_TTL_S)
    if not raw:
        return {}
    # Map raw name codes → readable names at return time (sum on collisions).
    named: dict = {}
    for code, n in raw["sets"].items():
        label = _exercise_name(code)
        named[label] = named.get(label, 0) + n
    return {"active_sets": raw["active_sets"], "sets": named}


def fetch_workout_detail(workout_id) -> dict:
    """Structure of a planned workout: name, coach description (Runna's free-text
    guidance, e.g. 'no faster than 7:15/km, a limit not a target'), and steps with
    target pace (min/km). The description often carries pace/effort cues that aren't
    in the structured targets."""
    if not workout_id:
        return {}
    key = f"workout:v2:{workout_id}"  # v2: now also stores name + description
    cached = _cache_get(key)
    if cached is not None:
        return cached
    d = _safe(_api, f"/workout-service/workout/{workout_id}")
    if isinstance(d, dict) and "_error" in d:
        return {}  # transient error — don't cache
    seg = (_g(d, "workoutSegments") or [{}])[0]
    steps = []
    for st in (seg.get("workoutSteps") or []):
        dist = st.get("endConditionValue")
        lo = st.get("targetValueOne")
        hi = st.get("targetValueTwo")

        def pace(v):
            return round((1000 / v) / 60, 2) if v else None

        steps.append({
            "dist_m": dist,
            "pace_min_km": [pace(hi), pace(lo)] if lo and hi else None,
        })
    result = {
        "name": _g(d, "workoutName"),
        "description": _g(d, "description"),
        "steps": steps,
    }
    _cache_put(key, result, WORKOUT_TTL_S)
    return result
