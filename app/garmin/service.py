"""Aggregation + orchestration: raw Garmin fetches → the compact typed Payload.

This is the cost-control layer — raw Garmin responses are collapsed into ~12
fields per day and never sent to the LLM. The aggregation logic (pace handling,
exercise-set muscle grouping, sync flags) is preserved exactly from the old
``garmin_client``.

Two entry points:
* ``build_payload``        — synchronous, always fetches fresh; **test harness only**
  (the whole-payload shape assertion in ``tests/test_garmin_service.py`` — no
  production caller, the app runs through ``build_payload_cached``).
* ``build_payload_cached`` — async; serves immutable past days from the DB and
  persists what it fetches, so history accumulates and Garmin calls drop.
"""
import asyncio
import datetime as dt
import json
import logging
import time
from typing import List, Optional, Tuple
from weakref import WeakValueDictionary

from fastapi.concurrency import run_in_threadpool

from app.garmin import client
from app.garmin.client import _g
from app.garmin.providers import get_provider
from app.garmin.schemas import Activity, DailySummary, Payload, PlannedRun
from app.multisport import sport_bucket

logger = logging.getLogger("garmin")


# PERF-05: one asyncio.Lock per user around the Garmin fetch phase of
# ``build_payload_cached``. A morning tick and a concurrent ``/report`` for the
# same user would otherwise both hammer Garmin for the same days — wasted calls,
# extra 429 risk, and interleaved upserts of the same rows. A WeakValueDictionary
# lets locks for idle users get GC'd (a coroutine awaiting one keeps it alive).
_user_fetch_locks: "WeakValueDictionary[int, asyncio.Lock]" = WeakValueDictionary()

# A freshly-built payload is memoised briefly: a second caller that was blocked on
# the lock reuses it instead of re-fetching today (~a dozen Garmin calls). The
# reuser gets ``new_activities=[]`` — the first caller already owns them, so
# auto-analysis never double-fires. Keyed by (user_id, days, activity_limit) so a
# narrow-window tick (days=3) never serves a wider request (e.g. /deep's days=14).
_recent_payload: dict = {}  # (user_id, days, activity_limit) -> (monotonic_ts, Payload)

# OPS-05: bot_state key + retention for the drained Garmin-error log. ``recent`` keeps up to
# _ERR_KEEP entries no older than _ERR_RETENTION_S so the 24h counters stay accurate for a
# realistic error volume on a 1-3 user Pi (a bigger burst simply shows "≥N").
GARMIN_ERRORS_KEY = "garmin_errors"
_ERR_KEEP = 50
_ERR_RETENTION_S = 48 * 3600
_ERR_DISPLAY = 20


def summarize_garmin_errors(state_json: Optional[str], *, now: Optional[float] = None) -> dict:
    """Parse the stored ``garmin_errors`` blob into a display/counter summary:
    ``{count_24h, counts_24h:{401:n,...}, last, recent}``. Pure — used by /status and the
    dashboard banner. ``counts_24h`` excludes EXPECTED failures (known garth 403 gaps) so a
    long-standing 403 never inflates the "degradation" signal; ``last`` is the newest entry."""
    now = now if now is not None else time.time()
    try:
        data = json.loads(state_json) if state_json else {}
    except (ValueError, TypeError):
        data = {}
    recent = data.get("recent") or []
    cutoff = now - 24 * 3600
    counts: dict = {}
    count_24h = 0
    for e in recent:
        if not isinstance(e, dict) or (e.get("ts") or 0) < cutoff:
            continue
        if e.get("expected"):
            continue
        kind = e.get("kind") or "other"
        counts[kind] = counts.get(kind, 0) + 1
        count_24h += 1
    last = recent[-1] if recent else None
    return {
        "count_24h": count_24h,
        "counts_24h": counts,
        "last": last,
        "recent": recent[-_ERR_DISPLAY:],
    }


async def _flush_garmin_errors(session, user_id: int) -> None:
    """Drain the client's in-process error ring buffer into this user's ``bot_state``
    (OPS-05). Best-effort: a bad stored blob is replaced, never fatal. Does not commit —
    the caller's ``session.commit()`` persists it alongside the fetched data."""
    drained = client.drain_errors()
    if not drained:
        return
    from app.garmin import repository
    try:
        prev = await repository.get_state(session, user_id, GARMIN_ERRORS_KEY)
        try:
            data = json.loads(prev) if prev else {}
        except (ValueError, TypeError):
            data = {}
        recent = [e for e in (data.get("recent") or []) if isinstance(e, dict)]
        recent.extend(drained)
        now = time.time()
        recent = [e for e in recent if (e.get("ts") or 0) >= now - _ERR_RETENTION_S][-_ERR_KEEP:]
        blob = json.dumps({"updated": now, "recent": recent}, ensure_ascii=False)
        await repository.set_state(session, user_id, GARMIN_ERRORS_KEY, blob)
    except Exception:  # noqa: BLE001 — never let error-logging break the fetch path
        logger.exception("OPS-05: failed to flush Garmin errors to bot_state")
_PAYLOAD_REUSE_S = 30.0


def _user_fetch_lock(user_id: int) -> asyncio.Lock:
    lock = _user_fetch_locks.get(user_id)
    if lock is None:
        lock = _user_fetch_locks[user_id] = asyncio.Lock()
    return lock


def _remember_payload(key: tuple, payload: Payload) -> None:
    now = time.monotonic()
    _recent_payload[key] = (now, payload)
    # Drop stale entries so the dict doesn't grow with every (user, window) seen.
    for k in [k for k, (ts, _) in _recent_payload.items()
              if k != key and now - ts > _PAYLOAD_REUSE_S]:
        _recent_payload.pop(k, None)


def _drop_user_payload(user_id: int) -> None:
    """Invalidate the brief payload reuse-memo for a user after a manual resync (ST-15), so a
    following ``/report`` doesn't serve stale pre-resync data out of the 30s reuse window."""
    for k in [k for k in _recent_payload if k[0] == user_id]:
        _recent_payload.pop(k, None)


def _fetch_days(dates_to_fetch: List[dt.date]) -> dict:
    """Fetch several day summaries in ONE threadpool hop (PERF-04b): login is
    already done, so batching avoids a round trip through anyio's pool per day."""
    return {d.isoformat(): daily_summary(d) for d in dates_to_fetch}


# ---------- AUTH ----------

def login() -> None:
    get_provider().login()


# ---------- HELPERS ----------

def _date_range(days: int) -> List[dt.date]:
    today = dt.date.today()
    return [today - dt.timedelta(days=i) for i in range(days)]


# ---------- AGGREGATION ----------

def _daily_extra(sleep: dict, hrv: dict, dto: dict, readiness: dict) -> dict:
    """Everything useful we fetch but don't put in the typed columns — kept as a
    compact scalar dict on ``DailyMetric.extra`` (no per-minute arrays). RHR, SpO2 and
    respiration come free from the sleep DTO; readiness is the one extra fetch."""
    hs = _g(hrv, "hrvSummary") or {}
    bl = _g(hs, "baseline") or {}
    sn = _g(dto, "sleepNeed") or {}
    need = sn.get("actual")
    raw = {
        # recovery / cardio
        "resting_hr": _g(sleep, "restingHeartRate"),
        "avg_hr_sleep": _g(dto, "avgHeartRate"),
        "overnight_hrv": _g(sleep, "avgOvernightHrv"),
        "bb_change": _g(sleep, "bodyBatteryChange"),
        "skin_temp_dev_c": _g(sleep, "avgSkinTempDeviationC"),
        # sleep quality
        "awake_count": _g(dto, "awakeCount"),
        "restless_moments": _g(sleep, "restlessMomentsCount"),
        "avg_sleep_stress": _g(dto, "avgSleepStress"),
        "sleep_need_h": round(need / 60, 2) if isinstance(need, (int, float)) else None,
        "sleep_need_feedback": sn.get("feedback"),
        "sleep_feedback": _g(dto, "sleepScoreFeedback"),
        # spo2 + respiration (already in the sleep DTO)
        "spo2_avg": _g(dto, "averageSpO2Value"),
        "spo2_low": _g(dto, "lowestSpO2Value"),
        "respiration_avg": _g(dto, "averageRespirationValue"),
        "breathing_disruption_sev": _g(sleep, "breathingDisruptionSeverity"),
        # hrv detail
        "hrv_weekly_avg": _g(hs, "weeklyAvg"),
        "hrv_5min_high": _g(hs, "lastNight5MinHigh"),
        "hrv_baseline_low": _g(bl, "balancedLow"),
        "hrv_baseline_high": _g(bl, "balancedUpper"),
        "hrv_feedback": _g(hs, "feedbackPhrase"),
        # training readiness (extra fetch)
        "readiness_score": _g(readiness, "score"),
        "readiness_level": _g(readiness, "level"),
        "readiness_feedback": _g(readiness, "feedbackShort"),
        "recovery_time_h": _g(readiness, "recoveryTime"),
        "acute_load": _g(readiness, "acuteLoad"),
        "acwr_pct": _g(readiness, "acwrFactorPercent"),
        "acwr_feedback": _g(readiness, "acwrFactorFeedback"),
    }
    return {k: v for k, v in raw.items() if v is not None}


def _auto_activities(events: list) -> Optional[str]:
    """Compact summary of activities the watch auto-detected but that were never
    confirmed/saved as a real Activity (e.g. an unrecorded bike ride) — so they
    never reach ``recent_activities``. Skips anything carrying an ``activityId``
    (already a confirmed activity, covered by ``fetch_activities``) and non-sport
    events (sleep/naps).

    Garmin doesn't document ``dailyEvents``' exact field names, so this parses
    defensively (several key spellings) and logs the raw shape of anything it
    can't classify, so the parsing can be corrected against a real account."""
    labels: List[str] = []
    for e in events:
        if not isinstance(e, dict) or e.get("activityId"):
            continue
        act_type = e.get("activityType")
        sport = (
            _g(e, "activityType", "typeKey") or _g(e, "activityType", "parentTypeKey")
            or e.get("activityTypeKey")
            or (act_type if isinstance(act_type, str) else None)
        )
        event_kind = _g(e, "eventType", "typeKey") or e.get("eventTypeKey")
        if not sport or not isinstance(sport, str) or event_kind in ("sleep", "nap"):
            continue
        # duration: durationInSeconds (old nested format) or duration (minutes, flat format)
        dur_min: Optional[float] = None
        dur_s = e.get("durationInSeconds")
        if isinstance(dur_s, (int, float)):
            dur_min = dur_s / 60
        else:
            raw_dur = e.get("duration") or e.get("durationInMilliseconds")
            if isinstance(raw_dur, (int, float)):
                dur_min = raw_dur / 60000 if e.get("durationInMilliseconds") else raw_dur
        raw_start = e.get("startTimestampLocal") or e.get("startTimeLocal")
        start = raw_start[11:16] if isinstance(raw_start, str) else ""
        label = sport.replace("_", " ").lower()
        if dur_min:
            label += f" {round(dur_min)}хв"
        labels.append(f"{start} {label}".strip() if start else label)
    if events and not labels:
        logger.debug(f"DAILY EVENTS unclassified, raw sample: {events[:2]}")
    return "; ".join(labels) or None


def _daily_extra_metrics(uds: dict, vo2: dict, race: dict, endurance: dict) -> dict:
    """Daily summary (steps/intensity/floors/BB range), VO2max, race-time predictions
    and endurance score — from the metrics + usersummary endpoints."""
    raw = {
        "steps": _g(uds, "totalSteps"),
        "distance_m": _g(uds, "totalDistanceMeters"),
        "active_kcal": _g(uds, "activeKilocalories"),
        "moderate_min": _g(uds, "moderateIntensityMinutes"),
        "vigorous_min": _g(uds, "vigorousIntensityMinutes"),
        "floors_up": _g(uds, "floorsAscended"),
        "min_hr": _g(uds, "minHeartRate"),
        "bb_high": _g(uds, "bodyBatteryHighestValue"),
        "bb_low": _g(uds, "bodyBatteryLowestValue"),
        "vo2max": _g(vo2, "vo2MaxPreciseValue") or _g(vo2, "vo2MaxValue"),
        "race_5k_s": _g(race, "time5K"),
        "race_10k_s": _g(race, "time10K"),
        "race_half_s": _g(race, "timeHalfMarathon"),
        "race_marathon_s": _g(race, "timeMarathon"),
        "endurance_score": _g(endurance, "overallScore"),
        "endurance_class": _g(endurance, "classification"),
    }
    return {k: v for k, v in raw.items() if v is not None}


def daily_summary(date: dt.date) -> dict:
    sleep = client.fetch_sleep(date)
    hrv = client.fetch_hrv(date)
    stress = client.fetch_stress(date)
    bb = client.fetch_body_battery(date)
    readiness = client.fetch_training_readiness(date)
    uds = client.fetch_user_summary(date)
    vo2 = client.fetch_vo2max(date)
    race = client.fetch_race_predictions()
    endurance = client.fetch_endurance(date)
    events = client.fetch_daily_events(date)
    dto = _g(sleep, "dailySleepDTO") or {}
    sec = lambda v: round(v / 3600, 2) if isinstance(v, (int, float)) else None

    extra = {
        **_daily_extra(sleep, hrv, dto, readiness),
        **_daily_extra_metrics(uds, vo2, race, endurance),
    }
    auto = _auto_activities(events)
    if auto:
        extra["auto_activities"] = auto
    result = {
        "date": date.isoformat(),
        "sleep_score": _g(dto, "sleepScores", "overall", "value"),
        "sleep_h": sec((_g(dto, "deepSleepSeconds") or 0)
                       + (_g(dto, "lightSleepSeconds") or 0)
                       + (_g(dto, "remSleepSeconds") or 0)),
        "deep_h": sec(_g(dto, "deepSleepSeconds")),
        "rem_h": sec(_g(dto, "remSleepSeconds")),
        "light_h": sec(_g(dto, "lightSleepSeconds")),
        "awake_h": sec(_g(dto, "awakeSleepSeconds")),
        "hrv_avg": _g(hrv, "hrvSummary", "lastNightAvg"),
        "hrv_status": _g(hrv, "hrvSummary", "status"),
        "stress_avg": _g(stress, "avgStressLevel"),
        "stress_max": _g(stress, "maxStressLevel"),
        "bb_charged": _g(bb, "charged"),
        "bb_drained": _g(bb, "drained"),
        "extra": extra or None,
    }
    result["has_data"] = any(result[k] is not None
                             for k in ("sleep_score", "hrv_avg", "stress_avg"))
    return result


def _attach_detail(row: dict, activity_id, force: bool = False) -> bool:
    """Fetch and attach the per-sport detail onto ``row`` in place — a strength session's
    exercise sets, or a run/ride pace-or-speed ``series``. Returns True when a Garmin detail
    call was actually made, so a batch loop can space subsequent fetches. Shared by the
    recent-activities fetch and the single-activity resync (ST-15).

    ``force=True`` bypasses the immutable-asset disk cache so a resync of an activity edited
    in Garmin (a swapped exercise, a cropped track) actually picks up the change instead of
    returning the stale cached copy (ST-15/ST-16).

    EP-10 phase 1: cycling gets the same series treatment (speed/power, not pace) — reuse
    NF-05's sport bucket rather than hand-rolling a second keyword list."""
    typ = row.get("type") or ""
    if not activity_id:
        return False
    if typ == "strength_training":
        ex = client.fetch_exercise_summary(activity_id, force=force)
        if ex:
            row["exercises"] = ex
        return True
    if "run" in typ:
        sr = client.fetch_activity_series(activity_id, sport="running", force=force)
        if sr:
            row["series"] = sr
        return True
    if sport_bucket(typ) == "bike":
        sr = client.fetch_activity_series(activity_id, sport="cycling", force=force)
        if sr:
            row["series"] = sr
        return True
    return False


def _activity_rows(limit: int = 30) -> List[Tuple[Optional[int], dict]]:
    """Build clean activity rows paired with their Garmin id (for persistence).
    The row dict itself keeps the exact public shape — no id leaks into it."""
    acts = client.fetch_activities(limit)
    if not isinstance(acts, list):
        return []
    out: List[Tuple[Optional[int], dict]] = []
    for a in acts:
        if not isinstance(a, dict):
            continue
        row = {
            "date": (a.get("startTimeLocal") or "")[:10],
            "type": _g(a, "activityType", "typeKey"),
            "dur_min": round((a.get("duration") or 0) / 60, 1),
            "dist_km": round((a.get("distance") or 0) / 1000, 2),
            "avg_hr": a.get("averageHR"),
            "max_hr": a.get("maxHR"),
            "load": a.get("activityTrainingLoad"),
        }
        if _attach_detail(row, a.get("activityId")):
            time.sleep(0.3)
        out.append((a.get("activityId"), row))
    return out


def _row_from_detail(a: dict) -> dict:
    """Build an activity row (the shape ``_activity_rows`` produces) from the single-activity
    detail DTO (``client.fetch_activity``), whose summary lives under ``summaryDTO`` /
    ``activityTypeDTO`` rather than the flat keys the activity-list endpoint uses. Falls back
    to the flat keys so it also copes with a list-shaped dict."""
    summary = _g(a, "summaryDTO") or {}

    def pick(key):
        v = summary.get(key)
        return v if v is not None else a.get(key)

    return {
        "date": (pick("startTimeLocal") or "")[:10],
        "type": _g(a, "activityTypeDTO", "typeKey") or _g(a, "activityType", "typeKey"),
        "dur_min": round((pick("duration") or 0) / 60, 1),
        "dist_km": round((pick("distance") or 0) / 1000, 2),
        "avg_hr": pick("averageHR"),
        "max_hr": pick("maxHR"),
        "load": pick("activityTrainingLoad"),
    }


def _fetch_activity_row(activity_id, force: bool = False) -> Optional[dict]:
    """Login + fetch one activity's detail and enrich it with series/exercises — the blocking
    half of :func:`resync_activity`, run in a single threadpool hop. ``force`` bypasses the
    immutable-asset disk cache (so an edited exercise/track is actually re-read). None if
    Garmin returns nothing for the id."""
    login()
    a = client.fetch_activity(activity_id)
    if not a:
        return None
    row = _row_from_detail(a)
    _attach_detail(row, activity_id, force=force)
    return row


def activity_summary(limit: int = 30) -> List[dict]:
    return [row for _id, row in _activity_rows(limit)]


def fetch_planned(days_ahead: int = 14) -> List[dict]:
    """Upcoming planned workouts from the Garmin calendar (Runna puts them here)."""
    today = dt.date.today()
    end = (today + dt.timedelta(days=days_ahead)).isoformat()
    months = {(today.year, today.month),
              (today.year + (today.month // 12), (today.month % 12) + 1)}
    out = []
    for (y, m) in months:
        c = client.fetch_calendar(y, m - 1)
        for i in (_g(c, "calendarItems") or []):
            dd = i.get("date", "")
            if i.get("itemType") == "workout" and today.isoformat() <= dd <= end:
                wid = i.get("workoutId")
                out.append({"date": dd, "title": i.get("title"),
                            "workout_id": wid,
                            "detail": client.fetch_workout_detail(wid)})
    seen, uniq = set(), []
    for x in sorted(out, key=lambda x: x["date"]):
        key = (x["date"], x["workout_id"])
        if key not in seen:
            seen.add(key)
            uniq.append(x)
    return uniq


# ---------- PAYLOAD ----------

def build_payload(days: int = 7, activity_limit: int = 30) -> Payload:
    """Synchronous full fetch. **Test-harness only** — exercises the aggregation shape
    (``tests/test_garmin_service.py``) with no DB; production always uses the async,
    DB-backed ``build_payload_cached``."""
    login()
    daily = [daily_summary(d) for d in _date_range(days)]
    today = dt.date.today().isoformat()
    today_row = next((d for d in daily if d["date"] == today), None)
    synced_today = bool(today_row and today_row["has_data"])
    last_with_data = next((d["date"] for d in daily if d["has_data"]), None)

    return Payload(
        generated=dt.datetime.now().isoformat(timespec="minutes"),
        window_days=days,
        synced_today=synced_today,
        last_data_date=last_with_data,
        daily=daily,
        recent_activities=activity_summary(activity_limit),
        planned_runs=fetch_planned(14),
    )


async def build_payload_cached(
    session, user_id: int, days: int = 7, activity_limit: int = 30
) -> Tuple[Payload, List]:
    """Async build that uses the DB as a per-user day-level cache and persists
    results.

    Immutable past days already stored are served from the DB (no Garmin call);
    today is always refetched. Everything fetched is upserted back so history
    grows over time. Blocking Garmin/aggregation calls run in a threadpool. The
    Garmin provider for ``user_id`` is taken from the runtime context (see
    ``app.garmin.runtime.user_runtime``).

    Returns ``(payload, new_activities)`` — ``new_activities`` are the
    ``ActivityRecord`` rows that were newly inserted this call (never updates),
    used by the bot to trigger auto-analysis of freshly synced activities."""
    from app.garmin import repository  # local import to avoid an import cycle

    # Serialize concurrent fetches for the same user (PERF-05): the whole
    # fetch+persist runs under the per-user lock. build_payload_cached always
    # fetches (today is never cached), so there's no pure-DB fast path here to
    # starve — other endpoints read the DB directly, not through this function.
    reuse_key = (user_id, days, activity_limit)
    async with _user_fetch_lock(user_id):
        hit = _recent_payload.get(reuse_key)
        if hit and (time.monotonic() - hit[0]) < _PAYLOAD_REUSE_S:
            # A concurrent caller just built this exact payload while we waited on
            # the lock — reuse it and don't re-trigger its new activities.
            logger.info(f"PAYLOAD reuse user={user_id} (fetched <{_PAYLOAD_REUSE_S:.0f}s ago)")
            return hit[1], []

        await run_in_threadpool(login)
        today = dt.date.today()
        today_iso = today.isoformat()
        dates = _date_range(days)
        past_iso = [d.isoformat() for d in dates if d < today]

        cached = await repository.read_daily_metrics(session, user_id, past_iso)

        # Missing past days + today are fetched together in ONE threadpool hop
        # (PERF-04b), not a round trip per day.
        to_fetch = [d for d in dates if not (d < today and d.isoformat() in cached)]
        fetched = await run_in_threadpool(_fetch_days, to_fetch) if to_fetch else {}

        daily: List[DailySummary] = []
        for d in dates:
            iso = d.isoformat()
            if d < today and iso in cached:
                daily.append(cached[iso])
            else:
                daily.append(DailySummary(**fetched[iso]))

        act_pairs = await run_in_threadpool(_activity_rows, activity_limit)
        activities = [Activity(**row) for _id, row in act_pairs]
        planned_raw = await run_in_threadpool(fetch_planned, 14)
        planned = [PlannedRun(**p) for p in planned_raw]

        today_row = next((d for d in daily if d.date == today_iso), None)
        synced_today = bool(today_row and today_row.has_data)
        last_with_data = next((d.date for d in daily if d.has_data), None)

        payload = Payload(
            generated=dt.datetime.now().isoformat(timespec="minutes"),
            window_days=days,
            synced_today=synced_today,
            last_data_date=last_with_data,
            daily=daily,
            recent_activities=activities,
            planned_runs=planned,
        )

        new_activities = await repository.persist_payload(session, user_id, payload, act_pairs)
        await _flush_garmin_errors(session, user_id)   # OPS-05: record any endpoint failures
        await session.commit()
        _remember_payload(reuse_key, payload)
        return payload, new_activities


# ---------- MANUAL RESYNC (ST-15) ----------

MAX_RESYNC_DAYS = 31   # hard cap on a single range resync — bound the Garmin request burst


async def resync_days(
    session, user_id: int, dates: List[dt.date]
) -> Tuple[int, int]:
    """Force-refetch ``daily_metrics`` for these dates and upsert over (ST-15).

    Unlike ``build_payload_cached`` this ignores the DB day-cache entirely — the whole point
    is to overwrite a day that was stored wrong or incomplete. Only days that come back with
    real data are written; an empty day (watch never synced / future date) is skipped rather
    than nulling over a previously-good row. Returns ``(written, requested)``. Runs under the
    per-user fetch lock, and every underlying Garmin call goes through ``client._api`` (the
    ``GARMIN_RPS`` rate-limiter + 429-retry are inherited for free)."""
    from app.garmin import repository  # local import to avoid an import cycle

    if not dates:
        return 0, 0
    dates = list(dates)
    async with _user_fetch_lock(user_id):
        await run_in_threadpool(login)
        fetched = await run_in_threadpool(_fetch_days, dates)
        written = 0
        for d in dates:
            data = fetched.get(d.isoformat())
            if not data:
                continue
            summary = DailySummary(**data)
            if summary.has_data:
                await repository.upsert_daily(session, user_id, summary)
                written += 1
        await _flush_garmin_errors(session, user_id)   # OPS-05
        await session.commit()
        _drop_user_payload(user_id)
    logger.info(f"RESYNC days user={user_id} wrote {written}/{len(dates)}")
    return written, len(dates)


async def resync_activity(session, user_id: int, activity_db_id: int):
    """Re-fetch one stored activity's summary (+ series/exercises) from Garmin and overwrite
    its row (ST-15).

    Works for an activity older than the recent-activities window: it fetches by the stored
    Garmin ``activity_id`` directly, not by scanning the last N activities. The immutable-asset
    disk cache is **force-bypassed** so an edit made in Garmin (a swapped exercise, a cropped
    track) actually re-reads instead of returning the stale cached exercises/series — the main
    reason to resync a single activity. Never creates a duplicate (upsert keys on
    ``activity_id``); ``subjective``/``analysis``/``step_match`` are left untouched, and a
    transient series/exercises miss keeps the previously-stored value instead of nulling it.
    Returns the updated ORM row, or None if the id isn't this user's / carries no Garmin id /
    Garmin returned nothing. Runs under the per-user fetch lock (the ``client._api``
    rate-limiter + 429-retry are inherited)."""
    from app.garmin import repository  # local import to avoid an import cycle

    act = await repository.get_activity(session, user_id, activity_db_id)
    if act is None or not act.activity_id:
        return None
    activity_id = act.activity_id
    async with _user_fetch_lock(user_id):
        row = await run_in_threadpool(_fetch_activity_row, activity_id, True)
        if row is None:
            return None
        # Preserve a good series/exercises when this fetch produced none (transient error, or
        # a non-run/ride that never carried one) — never null over stored data.
        if row.get("series") is None and act.series is not None:
            row["series"] = act.series
        if row.get("exercises") is None and act.exercises is not None:
            row["exercises"] = act.exercises
        await repository.upsert_activity(session, user_id, activity_id, row)
        await _flush_garmin_errors(session, user_id)   # OPS-05
        await session.commit()
        _drop_user_payload(user_id)
    await session.refresh(act)
    logger.info(f"RESYNC activity user={user_id} id={activity_db_id} garmin={activity_id}")
    return act
