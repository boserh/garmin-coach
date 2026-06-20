"""
garmin_client.py — фетч + агрегація даних Garmin через garth (0.4.47).
Тягне: сон, HRV, стрес, body battery, активності (+вправи силових), план Runna.
Пульс спокою недоступний через garth (403), тож головний маркер відновлення — HRV.
"""

import os
import time
import warnings
import logging
import datetime as dt
import time as _time
from collections import Counter
from typing import Optional

logger = logging.getLogger("garmin")

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

import garth

TOKEN_DIR = os.environ.get("GARTH_TOKEN_DIR", os.path.expanduser("~/.garth"))


# ---------- АВТОРИЗАЦІЯ ----------

def login(email: Optional[str] = None, password: Optional[str] = None) -> None:
    try:
        garth.resume(TOKEN_DIR)
        garth.client.username
        return
    except Exception:
        pass
    email = email or os.environ["GARMIN_EMAIL"]
    password = password or os.environ["GARMIN_PASSWORD"]
    garth.login(email, password, prompt_mfa=lambda: input("MFA код: "))
    garth.save(TOKEN_DIR)


# ---------- ДОПОМІЖНЕ ----------

def _date_range(days: int):
    today = dt.date.today()
    return [today - dt.timedelta(days=i) for i in range(days)]


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


# ---------- FETCH-И ----------

def fetch_sleep(date: dt.date) -> dict:
    return _safe(
        garth.connectapi,
        f"/wellness-service/wellness/dailySleepData/{garth.client.profile['userName']}",
        params={"date": date.isoformat(), "nonSleepBufferMinutes": 60},
    )


def fetch_hrv(date: dt.date) -> dict:
    return _safe(garth.connectapi, f"/hrv-service/hrv/{date.isoformat()}")


def fetch_stress(date: dt.date) -> dict:
    return _safe(garth.connectapi,
                 f"/wellness-service/wellness/dailyStress/{date.isoformat()}")


def fetch_body_battery(date: dt.date) -> dict:
    r = _safe(
        garth.connectapi,
        "/wellness-service/wellness/bodyBattery/reports/daily",
        params={"startDate": date.isoformat(), "endDate": date.isoformat()},
    )
    return r[0] if isinstance(r, list) and r else {}


def fetch_activities(limit: int = 30) -> list:
    return _safe(
        garth.connectapi,
        "/activitylist-service/activities/search/activities",
        params={"start": 0, "limit": limit},
    )


def fetch_exercise_summary(activity_id) -> dict:
    """Які групи м'язів і скільки активних підходів у силовій."""
    d = _safe(garth.connectapi, f"/activity-service/activity/{activity_id}/exerciseSets")
    sets = _g(d, "exerciseSets") or []
    groups = Counter()
    total_active = 0
    for s in sets:
        if s.get("setType") != "ACTIVE":
            continue
        total_active += 1
        ex = (s.get("exercises") or [{}])[0]
        cat = ex.get("category")
        if cat and cat not in ("RUN", "UNKNOWN"):
            groups[cat] += 1
    if not groups:
        return {}
    return {"active_sets": total_active, "muscle_groups": dict(groups.most_common())}


def fetch_workout_detail(workout_id) -> dict:
    """Структура запланованого тренування: кроки з цільовим темпом (хв/км)."""
    if not workout_id:
        return {}
    d = _safe(garth.connectapi, f"/workout-service/workout/{workout_id}")
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
    return {"steps": steps}


def fetch_planned(days_ahead: int = 14) -> list:
    """Найближчі заплановані тренування з календаря Garmin (Runna кладе сюди)."""
    today = dt.date.today()
    end = (today + dt.timedelta(days=days_ahead)).isoformat()
    months = {(today.year, today.month),
              (today.year + (today.month // 12), (today.month % 12) + 1)}
    out = []
    for (y, m) in months:
        c = _safe(garth.connectapi, f"/calendar-service/year/{y}/month/{m-1}")
        for i in (_g(c, "calendarItems") or []):
            dd = i.get("date", "")
            if i.get("itemType") == "workout" and today.isoformat() <= dd <= end:
                wid = i.get("workoutId")
                out.append({"date": dd, "title": i.get("title"),
                            "workout_id": wid,
                            "detail": fetch_workout_detail(wid)})
    seen, uniq = set(), []
    for x in sorted(out, key=lambda x: x["date"]):
        key = (x["date"], x["workout_id"])
        if key not in seen:
            seen.add(key)
            uniq.append(x)
    return uniq


# ---------- АГРЕГАЦІЯ ----------

def daily_summary(date: dt.date) -> dict:
    sleep = fetch_sleep(date)
    hrv = fetch_hrv(date)
    stress = fetch_stress(date)
    bb = fetch_body_battery(date)
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


def activity_summary(limit: int = 30) -> list:
    acts = fetch_activities(limit)
    if not isinstance(acts, list):
        return []
    out = []
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
            ex = fetch_exercise_summary(a["activityId"])
            if ex:
                row["exercises"] = ex
            time.sleep(0.3)
        out.append(row)
    return out


def build_payload(days: int = 7, activity_limit: int = 30) -> dict:
    login()
    daily = [daily_summary(d) for d in _date_range(days)]
    today = dt.date.today().isoformat()
    today_row = next((d for d in daily if d["date"] == today), None)
    synced_today = bool(today_row and today_row["has_data"])
    last_with_data = next((d["date"] for d in daily if d["has_data"]), None)

    return {
        "generated": dt.datetime.now().isoformat(timespec="minutes"),
        "window_days": days,
        "synced_today": synced_today,
        "last_data_date": last_with_data,
        "daily": daily,
        "recent_activities": activity_summary(activity_limit),
        "planned_runs": fetch_planned(14),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(build_payload(days=7), indent=2, ensure_ascii=False))
