"""Aggregation + orchestration: raw Garmin fetches → the compact typed Payload.

This is the cost-control layer — raw Garmin responses are collapsed into ~12
fields per day and never sent to the LLM. The aggregation logic (pace handling,
exercise-set muscle grouping, sync flags) is preserved exactly from the old
``garmin_client``.

Two entry points:
* ``build_payload``        — synchronous, always fetches fresh (CLI / fallback).
* ``build_payload_cached`` — async; serves immutable past days from the DB and
  persists what it fetches, so history accumulates and Garmin calls drop.
"""
import datetime as dt
import logging
import time
from typing import List, Optional, Tuple

from fastapi.concurrency import run_in_threadpool

from app.garmin import client
from app.garmin.client import _g
from app.garmin.providers import get_provider
from app.garmin.schemas import Activity, DailySummary, Payload, PlannedRun

logger = logging.getLogger("garmin")


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
        sport = (_g(e, "activityType", "typeKey") or _g(e, "activityType", "parentTypeKey")
                 or e.get("activityTypeKey"))
        event_kind = _g(e, "eventType", "typeKey") or e.get("eventTypeKey")
        if not sport or event_kind in ("sleep", "nap"):
            continue
        dur_s = e.get("durationInSeconds") or e.get("duration")
        if dur_s is None and e.get("durationInMilliseconds"):
            dur_s = e["durationInMilliseconds"] / 1000
        start = (e.get("startTimestampLocal") or e.get("startTimeLocal") or "")[11:16]
        label = str(sport).replace("_", " ").lower()
        if dur_s:
            label += f" {round(dur_s / 60)}хв"
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
        if row["type"] == "strength_training" and a.get("activityId"):
            ex = client.fetch_exercise_summary(a["activityId"])
            if ex:
                row["exercises"] = ex
            time.sleep(0.3)
        elif "run" in (row["type"] or "") and a.get("activityId"):
            sr = client.fetch_activity_series(a["activityId"])
            if sr:
                row["series"] = sr
            time.sleep(0.3)
        out.append((a.get("activityId"), row))
    return out


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
    """Synchronous full fetch. Used by the CLI and as a DB-free fallback."""
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

    await run_in_threadpool(login)
    today = dt.date.today()
    today_iso = today.isoformat()
    dates = _date_range(days)
    past_iso = [d.isoformat() for d in dates if d < today]

    cached = await repository.read_daily_metrics(session, user_id, past_iso)

    daily: List[DailySummary] = []
    for d in dates:
        iso = d.isoformat()
        if d < today and iso in cached:
            daily.append(cached[iso])
        else:
            row = await run_in_threadpool(daily_summary, d)
            daily.append(DailySummary(**row))

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
    await session.commit()
    return payload, new_activities
