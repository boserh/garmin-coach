"""Backfill ``daily_metrics`` from a Garmin GDPR data export (``DI_CONNECT``).

A one-time, offline import — no Garmin API calls, so it can't be rate-limited. Reads the
per-date JSON files (sleep, daily user summary, VO2max, race predictions, endurance,
training readiness), merges them by ``calendarDate``, and inserts the days we don't
already have. Existing days (e.g. recently fetched live, which carry HRV the export
lacks) are skipped unless ``overwrite=True``.
"""
import glob
import json
import logging
import os
from typing import Optional

logger = logging.getLogger("garmin")

# DailyMetric column fields (minus date/extra) we map from the export
_COLS = (
    "sleep_score", "sleep_h", "deep_h", "rem_h", "light_h", "awake_h",
    "hrv_avg", "hrv_status", "stress_avg", "stress_max", "bb_charged", "bb_drained",
)


def _load(folder: str, pattern: str) -> list:
    """All dict records across every file matching ``pattern`` (recursively)."""
    recs: list = []
    for f in glob.glob(os.path.join(folder, "**", pattern), recursive=True):
        try:
            data = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            logger.warning(f"EXPORT skip {os.path.basename(f)}: {e}")
            continue
        if isinstance(data, list):
            recs += [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            recs.append(data)
    return recs


def _by_date(recs: list) -> dict:
    """Index records by ISO ``calendarDate`` (last record for a date wins). Some files
    carry a non-ISO/epoch calendarDate — keep only ``YYYY-MM-DD`` strings."""
    out: dict = {}
    for r in recs:
        d = r.get("calendarDate")
        if isinstance(d, str) and len(d) == 10 and d[4] == "-" and d[7] == "-":
            out[d] = r
    return out


def _num(v):
    return v.get("value") if isinstance(v, dict) else v


def _hours(sec):
    return round(sec / 3600, 2) if isinstance(sec, (int, float)) else None


def _int(v):
    return round(v) if isinstance(v, (int, float)) else None


def _metric(health: dict, mtype: str) -> dict:
    """One metric (HRV/HR/SPO2/…) from a healthStatusData record's ``metrics`` list."""
    for m in health.get("metrics") or []:
        if m.get("type") == mtype:
            return m
    return {}


def _build_day(date, sleep, uds, readiness, vo2, race, endurance, health) -> dict:
    sc = sleep.get("sleepScores") or {}
    bb = uds.get("bodyBattery") or {}
    resp = uds.get("respiration") or {}
    spo2s = sleep.get("spo2SleepSummary") or {}
    deep = sleep.get("deepSleepSeconds")
    hrv_m = _metric(health, "HRV")   # off-baseline health-status metric

    cols = {
        "date": date,
        # export uses `overallScore` (int); the live API uses `overall.value`
        "sleep_score": _int(sc.get("overallScore")) if sc.get("overallScore") is not None
        else _num(sc.get("overall")),
        "sleep_h": _hours((deep or 0) + (sleep.get("lightSleepSeconds") or 0)
                          + (sleep.get("remSleepSeconds") or 0)) if deep is not None else None,
        "deep_h": _hours(deep),
        "rem_h": _hours(sleep.get("remSleepSeconds")),
        "light_h": _hours(sleep.get("lightSleepSeconds")),
        "awake_h": _hours(sleep.get("awakeSleepSeconds")),
        "hrv_avg": _int(hrv_m.get("value")),       # from healthStatusData metrics
        "hrv_status": None,         # Garmin HRV Status (BALANCED) not in the export
        "stress_avg": _int(sleep.get("avgSleepStress")),
        "stress_max": None,
        "bb_charged": _int(bb.get("chargedValue")),
        "bb_drained": _int(bb.get("drainedValue")),
    }
    extra = {
        "resting_hr": uds.get("restingHeartRate"),
        "min_hr": uds.get("minHeartRate"),
        "max_hr": uds.get("maxHeartRate"),
        "steps": uds.get("totalSteps"),
        "distance_m": uds.get("totalDistanceMeters"),
        "active_kcal": uds.get("activeKilocalories"),
        "moderate_min": uds.get("moderateIntensityMinutes"),
        "vigorous_min": uds.get("vigorousIntensityMinutes"),
        "respiration_avg": resp.get("avgWakingRespirationValue") or sleep.get("averageRespiration"),
        "spo2_avg": _num(spo2s.get("averageSpO2")) if spo2s else None,
        "hrv_baseline_low": hrv_m.get("baselineLowerLimit"),
        "hrv_baseline_high": hrv_m.get("baselineUpperLimit"),
        "awake_count": sleep.get("awakeCount"),
        "restless_moments": sleep.get("restlessMomentCount"),
        "breathing_disruption_sev": sleep.get("breathingDisruptionSeverity"),
        "vo2max": vo2.get("vo2MaxValue"),
        "fitness_age": vo2.get("fitnessAge"),
        "race_5k_s": race.get("raceTime5K"),
        "race_10k_s": race.get("raceTime10K"),
        "race_half_s": race.get("raceTimeHalf"),
        "race_marathon_s": race.get("raceTimeMarathon"),
        "endurance_score": endurance.get("overallScore"),
        "endurance_class": endurance.get("classification"),
        "recovery_time_h": readiness.get("recoveryTime"),
        "acwr_pct": readiness.get("acwrFactorPercent"),
        "acwr_feedback": readiness.get("acwrFactorFeedback"),
        "readiness_feedback": readiness.get("feedbackShort"),
    }
    cols["extra"] = {k: v for k, v in extra.items() if v is not None} or None
    # a real day has wellness/activity data (not just the daily-repeated race prediction)
    cols["has_data"] = (cols["sleep_score"] is not None
                        or cols["bb_charged"] is not None
                        or extra.get("steps") is not None)
    return cols


def parse_export(folder: str) -> dict:
    """Map every ``calendarDate`` in the export to a day dict (columns + extra)."""
    sleep = _by_date(_load(folder, "*sleepData*"))
    uds = _by_date(_load(folder, "*UDSFile*"))
    readiness = _by_date(_load(folder, "*TrainingReadinessDTO*"))
    vo2 = _by_date(_load(folder, "*MetricsMaxMetData*"))
    race = _by_date(_load(folder, "*RunRacePredictions*"))
    endurance = _by_date(_load(folder, "*EnduranceScore*"))
    health = _by_date(_load(folder, "*healthStatusData*"))
    dates = (set(sleep) | set(uds) | set(readiness) | set(vo2) | set(race)
             | set(endurance) | set(health))
    return {
        d: _build_day(d, sleep.get(d, {}), uds.get(d, {}), readiness.get(d, {}),
                      vo2.get(d, {}), race.get(d, {}), endurance.get(d, {}), health.get(d, {}))
        for d in sorted(dates)
    }


async def import_export(
    session, user_id: int, folder: str, overwrite: bool = False,
    since: Optional[str] = None,
) -> dict:
    """Backfill the export's days for ``user_id``. New dates are inserted; existing dates
    are **merge-filled** — only NULL columns are set and missing ``extra`` keys are added,
    so live-fetched data (HRV status, etc.) is never clobbered. ``overwrite=True`` instead
    replaces any value present in the export. ``since`` (ISO) limits the range (the app
    shows ~365 days of trend, so a year is plenty). Locates ``DI_CONNECT`` if given the
    top-level export folder. Idempotent."""
    from sqlalchemy import select

    from app.db.models import DailyMetric

    if not os.path.isdir(os.path.join(folder, "DI-Connect-Wellness")):
        inner = os.path.join(folder, "DI_CONNECT")
        if os.path.isdir(inner):
            folder = inner

    days = {d: row for d, row in parse_export(folder).items()
            if row["has_data"] and (since is None or d >= since)}
    existing = {m.date: m for m in (
        await session.execute(
            select(DailyMetric).where(DailyMetric.user_id == user_id)
        )
    ).scalars().all()}

    inserted = filled = unchanged = 0
    for date, row in days.items():
        m = existing.get(date)
        if m is None:
            session.add(DailyMetric(
                user_id=user_id, date=date, extra=row["extra"],
                **{c: row[c] for c in _COLS}))
            inserted += 1
            continue
        changed = False
        for c in _COLS:
            if row[c] is not None and (overwrite or getattr(m, c) is None):
                if getattr(m, c) != row[c]:
                    setattr(m, c, row[c])
                    changed = True
        merged = dict(m.extra or {})
        for k, v in (row["extra"] or {}).items():
            if v is not None and (overwrite or merged.get(k) is None) and merged.get(k) != v:
                merged[k] = v
                changed = True
        if changed:
            m.extra = merged or None
            filled += 1
        else:
            unchanged += 1
    await session.commit()
    stats = {"parsed": len(days), "inserted": inserted,
             "filled": filled, "unchanged": unchanged}
    logger.info(f"EXPORT import user={user_id}: {stats}")
    return stats
