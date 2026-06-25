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

def daily_summary(date: dt.date) -> dict:
    sleep = client.fetch_sleep(date)
    hrv = client.fetch_hrv(date)
    stress = client.fetch_stress(date)
    bb = client.fetch_body_battery(date)
    dto = _g(sleep, "dailySleepDTO") or {}
    sec = lambda v: round(v / 3600, 2) if isinstance(v, (int, float)) else None

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
) -> Payload:
    """Async build that uses the DB as a per-user day-level cache and persists
    results.

    Immutable past days already stored are served from the DB (no Garmin call);
    today is always refetched. Everything fetched is upserted back so history
    grows over time. Blocking Garmin/aggregation calls run in a threadpool. The
    Garmin provider for ``user_id`` is taken from the runtime context (see
    ``app.garmin.runtime.user_runtime``)."""
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

    await repository.persist_payload(session, user_id, payload, act_pairs)
    await session.commit()
    return payload
