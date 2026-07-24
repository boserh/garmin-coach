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
import threading
import time as _time
from typing import Optional

from app.core.config import settings
from app.garmin.exercise_names import EXERCISE_NAMES
from app.garmin.providers import get_provider

logger = logging.getLogger("garmin")


class GarminRateLimited(Exception):
    """Raised once ``GARMIN_RETRIES`` backoff retries are exhausted and Garmin is
    still answering 429 — i.e. it's actively throttling/blocking us, not a one-off
    blip. Unlike every other Garmin error, ``_safe`` deliberately does NOT swallow
    this one into an ``{"_error": ...}`` dict — it re-raises so it can propagate all
    the way up to ``bot/jobs.py``, which catches this specific type to DM the user
    instead of silently logging a per-field fetch failure."""


# ---------- HELPERS ----------

def _safe(fn, *a, **kw):
    label = a[0] if a else getattr(fn, "__name__", "call")
    t0 = _time.perf_counter()
    try:
        r = fn(*a, **kw)
        dt_ms = (_time.perf_counter() - t0) * 1000
        logger.info(f"GARMIN OK  {label}  {dt_ms:.0f}ms")
        return r
    except GarminRateLimited as e:
        # Retries exhausted, Garmin still 429ing — a real degradation, record it. (A single
        # 429 that a retry cleared never reaches here, so it's correctly not counted.)
        _record_error(label, e, kind="429")
        raise
    except Exception as e:
        dt_ms = (_time.perf_counter() - t0) * 1000
        logger.warning(f"GARMIN ERR {label}  {dt_ms:.0f}ms  {type(e).__name__}: {e}")
        _record_error(label, e)
        return {"_error": str(e)}


# ---------- ERROR VISIBILITY RING BUFFER (OPS-05) ----------
# _safe is the single chokepoint every connectapi fetch flows through, so one in-process
# ring buffer here captures the last ~50 failures with a stable endpoint suffix + a coarse
# classification (401/403/429/5xx/network/other). This code is synchronous and has no DB
# session (it runs in the threadpool), so the buffer is module-level; build_payload_cached
# and the manual resync paths drain it into bot_state after their fetch phase (the bot and
# web processes each have their own buffer — both merge into the shared DB state). Read-only
# observation of failures that already happen: zero new Garmin requests.
_ERROR_BUFFER_MAX = 50
_error_buffer: list = []
_error_lock = threading.Lock()

# Endpoint suffixes whose failures are EXPECTED (garth can't reach them on this account —
# the long-standing resting-HR 403, say) — kept in the buffer for the record but flagged
# ``expected`` so the burst DM counter can exclude them and not cry wolf.
_EXPECTED_ERROR_SUFFIXES = ("/biometric-service/",)


def _classify_error(exc: Exception) -> str:
    """Coarse bucket for a Garmin failure: one of 401/403/429/5xx/network/other. Reads the
    nested ``.error.response.status_code`` garth wraps a requests error in, then falls back
    to the string form (same defensive shape as ``_is_rate_limited``)."""
    for obj in (exc, getattr(exc, "error", None)):
        resp = getattr(obj, "response", None)
        code = getattr(resp, "status_code", None)
        if isinstance(code, int):
            if code in (401, 403, 429):
                return str(code)
            if 500 <= code <= 599:
                return "5xx"
    text = str(exc).lower()
    if "429" in text or "too many requests" in text:
        return "429"
    if "403" in text or "forbidden" in text:
        return "403"
    if "401" in text or "unauthorized" in text:
        return "401"
    if any(k in text for k in ("timeout", "timed out", "connection", "network",
                               "ssl", "resolve", "refused", "reset")):
        return "network"
    if any(k in text for k in ("500", "502", "503", "504", "bad gateway",
                               "server error", "unavailable")):
        return "5xx"
    return "other"


def _endpoint_suffix(label) -> str:
    """A short, stable identifier for the failing endpoint — the service + resource path
    (first two path segments), so per-date/per-id calls group together (``/hrv-service/hrv``
    rather than ``.../hrv/2026-07-24``)."""
    s = str(label).split("?", 1)[0]
    if not s.startswith("/"):
        return s[:64]
    segs = [seg for seg in s.split("/") if seg]
    return "/" + "/".join(segs[:2]) if segs else s[:64]


def _record_error(label, exc: Exception, *, kind: Optional[str] = None) -> None:
    suffix = _endpoint_suffix(label)
    entry = {
        "ts": _time.time(),
        "endpoint": suffix,
        "kind": kind or _classify_error(exc),
        "detail": f"{type(exc).__name__}: {exc}"[:200],
        "expected": any(suffix.startswith(p) for p in _EXPECTED_ERROR_SUFFIXES),
    }
    with _error_lock:
        _error_buffer.append(entry)
        if len(_error_buffer) > _ERROR_BUFFER_MAX:
            del _error_buffer[:-_ERROR_BUFFER_MAX]


def recent_errors() -> list:
    """A copy of the current buffer (oldest first) without draining it — for tests/introspection."""
    with _error_lock:
        return list(_error_buffer)


def drain_errors() -> list:
    """Return and clear the buffer — the fetch paths call this after a fetch phase to move
    the captured failures into ``bot_state`` (see ``service._flush_garmin_errors``)."""
    with _error_lock:
        items = list(_error_buffer)
        _error_buffer.clear()
        return items


def _g(obj, *keys, default=None):
    cur = obj
    for k in keys:
        if cur is None:
            return default
        cur = cur.get(k) if isinstance(cur, dict) else getattr(cur, k, None)
    return cur if cur is not None else default


# ---------- RATE LIMIT + 429 BACKOFF (PERF-05) ----------
# Every Garmin fetch and write goes through _api, so one process-wide pacer here
# throttles the lot. Post-Cloudflare (2026) an aggressive request pattern risks an
# account ban, not just a 429 — a polite, predictable rate is survival, not tuning.

class _RateLimiter:
    """Process-wide request pacer: a leaky-bucket spacer that reserves the next
    slot under a short lock, then sleeps (outside the lock) until it's due, so the
    many anyio threadpool workers issue Garmin calls at a steady ~``rps`` instead
    of bursting. Synchronous by design — the client runs in the threadpool, so
    asyncio primitives don't apply. It never wraps the MFA login gate (that's a
    ~25s human wait handled in ``app.garmin.mfa``, not a request)."""

    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / rps if rps and rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = _time.monotonic()
            start = max(self._next, now)
            self._next = start + self._interval
            wait = start - now
        if wait > 0:
            _time.sleep(wait)


_limiter = _RateLimiter(settings.GARMIN_RPS)


def _is_rate_limited(exc: Exception) -> bool:
    """True if ``exc`` is a Garmin 429. garth raises ``GarthHTTPError`` wrapping a
    requests error (``.error.response``), so check the nested status and fall back
    to the string form."""
    for obj in (exc, getattr(exc, "error", None)):
        resp = getattr(obj, "response", None)
        if getattr(resp, "status_code", None) == 429:
            return True
    text = str(exc).lower()
    return "429" in text or "too many requests" in text


def _api(path: str, **kwargs):
    """Throttled connectapi call with exponential backoff on 429 (PERF-05). Once
    ``GARMIN_RETRIES`` are exhausted, a genuine rate-limit raises ``GarminRateLimited``
    (chained from the original exception) so callers can tell "Garmin is blocking us"
    apart from any other failure; any non-429 exception still propagates unchanged."""
    attempts = max(0, settings.GARMIN_RETRIES)
    for attempt in range(attempts + 1):
        _limiter.acquire()
        try:
            return get_provider().connectapi(path, **kwargs)
        except Exception as exc:
            if not _is_rate_limited(exc):
                raise
            if attempt < attempts:
                backoff = 2.0 ** attempt
                logger.warning(
                    f"GARMIN 429 {path} — backoff {backoff:.0f}s "
                    f"(retry {attempt + 1}/{attempts})"
                )
                _time.sleep(backoff)
                continue
            logger.error(f"GARMIN 429 {path} — retries exhausted, Garmin is blocking requests")
            raise GarminRateLimited(path) from exc


# ---------- DISK CACHE (stable, ID-keyed, immutable assets) ----------
# Exercise sets (never change), run series (never change) and workout details
# (rarely change) are keyed on stable Garmin IDs and cached as ONE FILE PER KEY
# under GARMIN_CACHE_DIR (PERF-02): the old single garmin_cache.json rewrote the
# whole cache on every put and each process held its own copy — cross-process
# writes silently dropped each other's entries. Per-key files make writes atomic
# and independent, and both processes read the same directory. Keys are
# "<kind>:<id>"; file payloads are [data, expires_at]. An in-process memo fronts
# the file reads (fine for these assets: immutable or slow-changing).
# (The one-time garmin_cache.json → per-key-file + .migrated seed was removed once
# every deployment had migrated — C1; git history keeps it if it's ever needed.)
GARMIN_CACHE_DIR = settings.GARMIN_CACHE_DIR
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


def fetch_activity(activity_id) -> dict:
    """The full detail DTO for one activity (``/activity-service/activity/{id}``) — its
    summary (duration/distance/HR/load/type/start) for resyncing a single stored activity
    that may be older than the recent-activities window (ST-15). The summary lives nested
    under ``summaryDTO``/``activityTypeDTO`` here, unlike the flat activity-list rows.
    Returns {} on error."""
    d = _safe(_api, f"/activity-service/activity/{activity_id}")
    return d if isinstance(d, dict) and "_error" not in d else {}


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


def fetch_exercise_summary(activity_id, force: bool = False) -> dict:
    """Per-exercise breakdown of a strength workout — set count plus the reps and weight of
    each set (Garmin's own per-set data, shown in Connect's "Overview").

    Shape: ``{"active_sets": int, "sets": {<name>: {"count": int, "reps": [int|None, ...],
    "weight_kg": [float|None, ...]}}}`` — ``reps``/``weight_kg`` are per active set, in order
    (``None`` where Garmin has none: a time-based hold has no reps, a bodyweight move no
    weight). Garmin stores set weight in grams → kg here.

    ``force=True`` bypasses the disk cache and refetches (overwriting it) — for a manual resync
    (ST-15/ST-16) of an activity whose exercises were edited in Garmin, since the cache
    otherwise treats a completed activity's sets as immutable. Cache key is ``v3`` (``v2`` held
    set counts only), so pre-existing cached entries are re-fetched rather than mis-parsed."""
    key = f"exercise:v3:{activity_id}"
    raw = None if force else _cache_get(key)
    if raw is None:
        d = _safe(_api, f"/activity-service/activity/{activity_id}/exerciseSets")
        if isinstance(d, dict) and "_error" in d:
            return {}  # transient error — don't cache
        by_code: dict = {}       # name code → {"count", "reps": [...], "weight_kg": [...]}
        total_active = 0
        for s in (_g(d, "exerciseSets") or []):
            if s.get("setType") != "ACTIVE":
                continue
            ex = (s.get("exercises") or [{}])[0]
            cat = ex.get("category")
            if cat == "RUN":
                continue  # a warm-up jog logged inside the workout, not a strength set
            # Keep any set that carries a real exercise name, even when Garmin couldn't
            # classify it (category UNKNOWN/None) — TRX, bodyweight and custom moves often
            # land there, and dropping them lost real sets ("не все синкає"). Only skip a set
            # that has neither a usable name nor a category.
            code = ex.get("name") or cat
            if not code or code == "UNKNOWN":
                continue
            total_active += 1
            entry = by_code.setdefault(code, {"count": 0, "reps": [], "weight_kg": []})
            entry["count"] += 1
            reps = s.get("repetitionCount")
            entry["reps"].append(int(reps) if isinstance(reps, (int, float)) and reps else None)
            w = s.get("weight")
            entry["weight_kg"].append(
                round(w / 1000.0, 1) if isinstance(w, (int, float)) and w else None)
        # Preserve Garmin's execution order (first appearance of each exercise) so the list
        # reads top-to-bottom like Connect's own — easier to spot anything missing than the
        # old "most sets first" sort. dict keeps insertion order.
        ordered = list(by_code.items())
        raw = {} if not by_code else {"active_sets": total_active,
                                      "sets": {c: info for c, info in ordered}}
        _cache_put(key, raw, EXERCISE_TTL_S)
    if not raw:
        return {}
    # Map raw name codes → readable names at return time (merge on collisions).
    named: dict = {}
    for code, info in raw["sets"].items():
        label = _exercise_name(code)
        cur = named.get(label)
        if cur is None:
            named[label] = {"count": info["count"], "reps": list(info["reps"]),
                            "weight_kg": list(info["weight_kg"])}
        else:
            cur["count"] += info["count"]
            cur["reps"] += info["reps"]
            cur["weight_kg"] += info["weight_kg"]
    return {"active_sets": raw["active_sets"], "sets": named}


SERIES_TTL_S = 365 * 24 * 3600   # a completed run's per-point series is immutable

# EP-10 phase 1: which metric Garmin descriptor keys to pull per sport bucket. Running
# stays pace-first (min/km, the original shape); cycling swaps pace for speed (km/h) +
# power (watts, when the device reports it) — the ticket's own framing ("вело: швидкість/
# потужність/HR замість темпу"). Unrecognised sports fall back to the running shape.
# EP-15: "elevation" is the same lookup mechanics, added to both buckets — a descriptor
# key that isn't present (older watch, missing altimeter) resolves to None like any other
# missing column, never a crash (unverified against a live account — see the module intro).
_SERIES_METRIC_KEYS = {
    "running": {"speed": "directSpeed", "hr": "directHeartRate", "dist": "sumDistance",
                "elevation": "directElevation"},
    "cycling": {"speed": "directSpeed", "hr": "directHeartRate", "dist": "sumDistance",
                "power": "directPower", "elevation": "directElevation"},
}


def fetch_activity_series(
    activity_id, max_points: int = 150, sport: str = "running", force: bool = False
) -> list:
    """Per-point series for one activity, for the detail-page chart / LLM segments.

    Reads Garmin's ``/details`` metrics, locates the columns by descriptor key (indices
    vary), and downsamples to ``max_points``. Shape depends on ``sport``:
    running → ``[{"d": dist_km, "p": pace_min_km, "hr": bpm, "e": elevation_m}, ...]``;
    cycling → ``[{"d": dist_km, "spd": speed_kmh, "pw": watts_or_None, "hr": bpm,
    "e": elevation_m}, ...]``. ``None`` where a point lacks the value (EP-15: ``e`` is
    ``None`` on every point when the watch/endpoint has no altitude — old series stay
    exactly as before). Immutable → disk-cached like exercises; ``force=True`` bypasses that
    cache and refetches (overwriting it) for a manual resync of an edited/cropped activity
    (ST-15/ST-16)."""
    key = f"series:v2:{activity_id}"
    cached = None if force else _cache_get(key)
    if cached is not None:
        return cached
    d = _safe(
        _api, f"/activity-service/activity/{activity_id}/details",
        params={"maxChartSize": max_points},
    )
    if not isinstance(d, dict) or "_error" in d:
        return []  # transient error — don't cache
    keys = _SERIES_METRIC_KEYS.get(sport, _SERIES_METRIC_KEYS["running"])
    idx = {x.get("key"): x.get("metricsIndex") for x in (_g(d, "metricDescriptors") or [])}
    i_speed = idx.get(keys["speed"])
    i_hr = idx.get(keys["hr"])
    i_dist = idx.get(keys["dist"])
    i_power = idx.get(keys["power"]) if keys.get("power") else None
    i_elev = idx.get(keys["elevation"])
    pts = _g(d, "activityDetailMetrics") or []
    step = max(1, len(pts) // max_points)  # downsample if Garmin returned more

    def val(metrics, i):
        return metrics[i] if i is not None and i < len(metrics) else None

    series = []
    for p in pts[::step]:
        m = p.get("metrics") or []
        speed, hr, dist = val(m, i_speed), val(m, i_hr), val(m, i_dist)
        elev = val(m, i_elev)
        point = {
            "d": round(dist / 1000.0, 2) if dist is not None else None,
            "hr": int(hr) if hr is not None else None,
            "e": round(elev, 1) if elev is not None else None,
        }
        if sport == "cycling":
            point["spd"] = round(speed * 3.6, 1) if speed and speed > 0 else None
            power = val(m, i_power)
            point["pw"] = round(power) if power is not None else None
        else:
            point["p"] = round((1000.0 / speed) / 60.0, 2) if speed and speed > 0 else None
        series.append(point)
    _cache_put(key, series, SERIES_TTL_S)
    return series


SPLITS_TTL_S = 365 * 24 * 3600   # a completed run's laps are immutable, like the series


def fetch_activity_splits(activity_id) -> list:
    """This run's lap/split breakdown — one row per structured-workout step actually
    executed on the watch (warmup, each work/recovery interval, cooldown), for NF-14
    step-level plan-vs-actual matching. Returns ``[{"dist_m", "dur_s", "pace_min_km"},
    ...]`` in lap order (``pace_min_km`` is ``None`` when the lap has no usable speed or
    distance/duration). Immutable once the activity is complete → disk-cached like the
    series/exercises."""
    key = f"splits:v1:{activity_id}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    d = _safe(_api, f"/activity-service/activity/{activity_id}/splits")
    if not isinstance(d, dict) or "_error" in d:
        return []  # transient error — don't cache
    laps = []
    for lap in (_g(d, "lapDTOs") or []):
        dist = lap.get("distance")
        dur = lap.get("duration") or lap.get("movingDuration")
        speed = lap.get("averageSpeed")
        pace = None
        if speed and speed > 0:
            pace = round((1000.0 / speed) / 60.0, 3)
        elif dist and dur and dist > 0:
            pace = round((dur / 60.0) / (dist / 1000.0), 3)
        laps.append({
            "dist_m": round(dist, 1) if dist is not None else None,
            "dur_s": round(dur, 1) if dur is not None else None,
            "pace_min_km": pace,
        })
    _cache_put(key, laps, SPLITS_TTL_S)
    return laps


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


# ---------- GEAR (NF-15) ----------
# NB the gear endpoints below are the two methods the community `python-garminconnect`
# library exposes (get_gear / get_gear_stats) — this codebase has no live-verified recon
# against a real account yet (the ticket's own AC #1 flags this as a blocker, not a
# detail: docs/backlog/NF-15-shoe-mileage-tracker.md). There is also no documented
# activity→gear link endpoint in that reference library, so — deliberately deviating from
# the ticket's original "sum our own activities' distance per gear_id" design — mileage
# comes straight from Garmin's own per-gear ``stats`` total (which the Connect UI already
# shows as a shoe's lifetime distance), never from an ActivityRecord column we'd have to
# backfill and could easily get wrong. Every parse is defensive (``app.gear``): an
# unrecognised shape is logged once and treated as "no gear data" rather than guessed at.
GEAR_TTL_S = 7 * 24 * 3600         # a gear list barely changes day to day, like workout:v2
GEAR_STATS_TTL_S = 24 * 3600       # a shoe's total distance grows with every run — refresh daily

_unmapped_gear: set = set()


def _profile_pk():
    """The numeric Garmin profile id the gear endpoints key on (``userProfilePk``) — NOT
    the username/displayName strings used elsewhere in this module. Never changes for a
    given account, so it's disk-cached like an immutable id (no point re-fetching
    ``socialProfile`` on every gear sync). Best-effort: returns None (never raises) on a
    fetch failure or an unrecognised shape, logging the latter once."""
    key = "gear_profile_pk:v1"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    prof = _safe(_api, "/userprofile-service/socialProfile")
    if not isinstance(prof, dict) or "_error" in prof:
        return None
    for pk_key in ("id", "profileId"):
        v = prof.get(pk_key)
        if v is not None:
            pk = str(v)
            _cache_put(key, pk, EXERCISE_TTL_S)
            return pk
    if "profile_pk" not in _unmapped_gear:
        _unmapped_gear.add("profile_pk")
        logger.warning(f"GEAR socialProfile has no id/profileId key: {sorted(prof)[:20]}")
    return None


def fetch_gear() -> list:
    """The user's saved Garmin gear (shoes/other equipment) — best-effort, [] on any
    failure or when the profile id can't be resolved (never breaks the sync tick). 7-day
    disk cache, keyed on the profile id (a gear ROSTER rarely changes day to day)."""
    pk = _profile_pk()
    if pk is None:
        return []
    key = f"gear:v1:{pk}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    r = _safe(_api, "/gear-service/gear/filterGear", params={"userProfilePk": pk})
    items = r if isinstance(r, list) else []
    _cache_put(key, items, GEAR_TTL_S)
    return items


def fetch_gear_stats(gear_uuid: str) -> dict:
    """Aggregate stats for one gear item (Garmin computes the lifetime total itself —
    see the module note above). Returns {} on error; short cache since it grows with
    every logged activity, unlike the roster itself."""
    key = f"gear_stats:v1:{gear_uuid}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    d = _safe(_api, f"/gear-service/gear/stats/{gear_uuid}")
    stats = d if isinstance(d, dict) and "_error" not in d else {}
    _cache_put(key, stats, GEAR_STATS_TTL_S)
    return stats
