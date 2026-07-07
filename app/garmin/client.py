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
# Exercise sets (never change), run series (never change) and workout details
# (rarely change) are keyed on stable Garmin IDs and cached as ONE FILE PER KEY
# under GARMIN_CACHE_DIR (PERF-02): the old single garmin_cache.json rewrote the
# whole cache on every put and each process held its own copy — cross-process
# writes silently dropped each other's entries. Per-key files make writes atomic
# and independent, and both processes read the same directory. Keys are
# "<kind>:<id>"; file payloads are [data, expires_at]. An in-process memo fronts
# the file reads (fine for these assets: immutable or slow-changing).
GARMIN_CACHE_DIR = settings.GARMIN_CACHE_DIR
GARMIN_CACHE_FILE = settings.GARMIN_CACHE_FILE  # legacy single-file cache (seed source)
EXERCISE_TTL_S = 365 * 24 * 3600   # a completed activity's sets are immutable
WORKOUT_TTL_S = 7 * 24 * 3600      # a planned workout can be edited; refresh weekly

_memo: dict = {}


def _key_path(key: str) -> str:
    return os.path.join(GARMIN_CACHE_DIR, key.replace(":", "_") + ".json")


def _write_entry(path: str, entry: list) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False)
    os.replace(tmp, path)


def _seed_legacy_cache() -> None:
    """One-time migration: split the old single-file cache into per-key files (its
    series/exercise entries carry year-long TTLs — re-fetching hundreds of them
    from Garmin risks a 429), then rename it so this never runs again."""
    try:
        with open(GARMIN_CACHE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    except Exception as e:
        logger.warning(f"GCACHE seed read failed: {e}")
        return
    now = _time.time()
    n = 0
    try:
        os.makedirs(GARMIN_CACHE_DIR, exist_ok=True)
        for k, v in raw.items():
            path = _key_path(k)
            if isinstance(v, list) and len(v) == 2 and v[1] > now \
                    and not os.path.exists(path):
                _write_entry(path, v)
                n += 1
        os.replace(GARMIN_CACHE_FILE, GARMIN_CACHE_FILE + ".migrated")
        logger.info(f"GCACHE seeded {n} entries from {GARMIN_CACHE_FILE}")
    except Exception as e:
        logger.warning(f"GCACHE seed failed: {e}")


_seed_legacy_cache()


def _cache_get(key: str):
    entry = _memo.get(key)
    now = _time.time()
    if not (entry and entry[1] > now):
        # miss or expired in memory — another process may have written a fresher file
        try:
            with open(_key_path(key), encoding="utf-8") as f:
                entry = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        except Exception as e:
            logger.warning(f"GCACHE read failed: {e}")
            return None
        if not (isinstance(entry, list) and len(entry) == 2):
            return None
        _memo[key] = entry
    if entry[1] > now:
        logger.info(f"GARMIN CACHE  {key}")
        return entry[0]
    return None


def _cache_put(key: str, value, ttl_s: float) -> None:
    entry = [value, _time.time() + ttl_s]
    _memo[key] = entry
    try:
        os.makedirs(GARMIN_CACHE_DIR, exist_ok=True)
        _write_entry(_key_path(key), entry)
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


def fetch_training_readiness(date: dt.date) -> dict:
    """Garmin's composite readiness for the day: score/level + recovery time + the
    acute:chronic load (ACWR) and HRV/sleep/stress factor breakdown."""
    r = _safe(_api, f"/metrics-service/metrics/trainingreadiness/{date.isoformat()}")
    if isinstance(r, list):
        return r[0] if r else {}
    return r if isinstance(r, dict) and "_error" not in r else {}


def fetch_user_summary(date: dt.date) -> dict:
    """Daily user summary: steps, distance, calories, intensity minutes, floors,
    RHR/min HR, body-battery high/low, avg stress. (Needs the displayName, not email.)"""
    d = _safe(
        _api, f"/usersummary-service/usersummary/daily/{get_provider().display_name}",
        params={"calendarDate": date.isoformat()},
    )
    return d if isinstance(d, dict) and "_error" not in d else {}


def fetch_vo2max(date: dt.date) -> dict:
    """The day's VO2max (``generic`` = running). Updates only after qualifying
    activities, so most days are empty."""
    r = _safe(
        _api, f"/metrics-service/metrics/maxmet/daily/{date.isoformat()}/{date.isoformat()}")
    if isinstance(r, list) and r and isinstance(r[-1], dict):
        return r[-1].get("generic") or {}
    return {}


def fetch_race_predictions() -> dict:
    """Latest predicted race times (5K/10K/half/marathon, seconds) for current fitness."""
    d = _safe(
        _api, f"/metrics-service/metrics/racepredictions/latest/{get_provider().display_name}")
    return d if isinstance(d, dict) and "_error" not in d else {}


def fetch_endurance(date: dt.date) -> dict:
    """The day's endurance score + classification."""
    d = _safe(
        _api, "/metrics-service/metrics/endurancescore",
        params={"calendarDate": date.isoformat()})
    return d if isinstance(d, dict) and "_error" not in d else {}


def fetch_activities(limit: int = 30) -> list:
    return _safe(
        _api,
        "/activitylist-service/activities/search/activities",
        params={"start": 0, "limit": limit},
    )


def fetch_calendar(year: int, month_index: int):
    """Raw calendar for a month. Garmin's month index is 0-based."""
    return _safe(_api, f"/calendar-service/year/{year}/month/{month_index}")


def fetch_daily_events(date: dt.date) -> list:
    """Garmin's daily events feed: includes activities the watch auto-detected
    (e.g. a bike ride) even when the user never confirmed/saved them as a real
    Activity — those never show up in ``fetch_activities``. Parsed defensively
    by ``service._auto_activities`` since the exact field names aren't
    documented; unexpected shapes are logged there for tuning."""
    r = _safe(
        _api, "/wellness-service/wellness/dailyEvents",
        params={"calendarDate": date.isoformat()},
    )
    return r if isinstance(r, list) else []


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


SERIES_TTL_S = 365 * 24 * 3600   # a completed run's per-point series is immutable


def fetch_activity_series(activity_id, max_points: int = 150) -> list:
    """Per-point pace + HR series for a run, for the detail-page chart.

    Reads Garmin's ``/details`` metrics, locates the speed / HR / distance columns
    by descriptor key (indices vary), converts m/s → min/km, and downsamples to
    ``max_points``. Returns ``[{"d": dist_km, "p": pace_min_km, "hr": bpm}, ...]``
    (None where a point lacks the value). Immutable → disk-cached like exercises."""
    key = f"series:v1:{activity_id}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    d = _safe(
        _api, f"/activity-service/activity/{activity_id}/details",
        params={"maxChartSize": max_points},
    )
    if not isinstance(d, dict) or "_error" in d:
        return []  # transient error — don't cache
    idx = {x.get("key"): x.get("metricsIndex") for x in (_g(d, "metricDescriptors") or [])}
    i_speed = idx.get("directSpeed")
    i_hr = idx.get("directHeartRate")
    i_dist = idx.get("sumDistance")
    pts = _g(d, "activityDetailMetrics") or []
    step = max(1, len(pts) // max_points)  # downsample if Garmin returned more

    def val(metrics, i):
        return metrics[i] if i is not None and i < len(metrics) else None

    series = []
    for p in pts[::step]:
        m = p.get("metrics") or []
        speed, hr, dist = val(m, i_speed), val(m, i_hr), val(m, i_dist)
        series.append({
            "d": round(dist / 1000.0, 2) if dist is not None else None,
            "p": round((1000.0 / speed) / 60.0, 2) if speed and speed > 0 else None,
            "hr": int(hr) if hr is not None else None,
        })
    _cache_put(key, series, SERIES_TTL_S)
    return series


def fetch_workout_full(workout_id) -> dict:
    """The full raw workout DTO — for cloning a saved template (Day 1/Day 2) into our
    own copy. Returns {} on error."""
    d = _safe(_api, f"/workout-service/workout/{workout_id}")
    return d if isinstance(d, dict) and "_error" not in d else {}


def fetch_workouts(limit: int = 400) -> list:
    """List the user's saved workouts (``id`` / ``name`` / ``sport``) — for picking the
    strength routines (Day 1 / Day 2) to schedule. Own workouts only. **Paginated** (pages
    of 100 up to ``limit``): a long-standing routine like Day 1 sorts by update-date behind
    newer ones, so a single 60-row page silently dropped it (the picker then only saw
    'Day 1 manual'/'Day 2')."""
    out = []
    page = 100
    for start in range(0, max(limit, 1), page):
        r = _safe(
            _api, "/workout-service/workouts",
            params={"start": start, "limit": page, "myWorkoutsOnly": True,
                    "orderBy": "UPDATE_DATE", "orderSeq": "DESC"},
        )
        batch = r if isinstance(r, list) else []
        for w in batch:
            if isinstance(w, dict) and w.get("workoutId"):
                out.append({"id": w["workoutId"], "name": _g(w, "workoutName"),
                            "sport": _g(w, "sportType", "sportTypeKey")})
        if len(batch) < page:  # last page
            break
    return out


def create_workout(payload: dict) -> dict:
    """Create a saved workout in Garmin Connect; returns the created DTO (incl.
    ``workoutId``). Raises on HTTP error (callers handle write failures)."""
    return _api("/workout-service/workout", method="POST", json=payload)


def schedule_workout(workout_id, date_iso: str) -> dict:
    """Put a saved workout onto a calendar date; returns the schedule DTO (incl.
    ``workoutScheduleId``)."""
    return _api(
        f"/workout-service/schedule/{workout_id}", method="POST", json={"date": date_iso}
    )


def delete_workout(workout_id) -> None:
    """Delete a saved workout (also removes any calendar schedule for it)."""
    _api(f"/workout-service/workout/{workout_id}", method="DELETE")


def delete_schedule(schedule_id) -> None:
    """Unschedule a workout from the calendar (the saved workout stays)."""
    _api(f"/workout-service/schedule/{schedule_id}", method="DELETE")


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
