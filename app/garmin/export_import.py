"""Backfill ``daily_metrics`` from a Garmin GDPR data export (``DI_CONNECT``).

A one-time, offline import — no Garmin API calls, so it can't be rate-limited. Reads the
per-date JSON files (sleep, daily user summary, VO2max, race predictions, endurance,
training readiness), merges them by ``calendarDate``, and inserts the days we don't
already have. Existing days (e.g. recently fetched live, which carry HRV the export
lacks) are skipped unless ``overwrite=True``.
"""
import datetime as dt
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


def _stress_total(uds: dict) -> dict:
    """The all-day (TOTAL) stress aggregator from a UDS record — avg + max stress."""
    for x in (uds.get("allDayStress") or {}).get("aggregatorList") or []:
        if x.get("type") == "TOTAL":
            return x
    return {}


def _build_day(date, sleep, uds, readiness, vo2, race, endurance, health) -> dict:
    sc = sleep.get("sleepScores") or {}
    bb = uds.get("bodyBattery") or {}
    resp = uds.get("respiration") or {}
    spo2s = sleep.get("spo2SleepSummary") or {}
    deep = sleep.get("deepSleepSeconds")
    hrv_m = _metric(health, "HRV")   # off-baseline health-status metric
    stress = _stress_total(uds)      # all-day stress (avg + max)

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
        "hrv_status": None,         # Garmin HRV Status (BALANCED) not a clean export field
        # all-day stress from UDS; fall back to the sleep-stress proxy if UDS is absent
        "stress_avg": _int(stress.get("averageStressLevel")) if stress
        else _int(sleep.get("avgSleepStress")),
        "stress_max": _int(stress.get("maxStressLevel")),
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


def parse_activities(folder: str) -> list:
    """All activities from the export's ``summarizedActivities`` file(s)."""
    acts: list = []
    for r in _load(folder, "*summarizedActivities*"):
        if isinstance(r.get("summarizedActivitiesExport"), list):
            acts += [a for a in r["summarizedActivitiesExport"] if isinstance(a, dict)]
        elif r.get("activityId"):
            acts.append(r)
    return acts


def _activity_row(a: dict):
    """(activity_id, row) in the shape ``repository.upsert_activity`` expects."""
    aid = a.get("activityId")
    ts = a.get("startTimeLocal") or a.get("beginTimestamp")
    date = None
    if isinstance(ts, (int, float)):
        date = dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).date().isoformat()
    dur, dist = a.get("duration"), a.get("distance")
    row = {
        "date": date,
        "type": a.get("activityType"),
        # the export's duration is ms and distance is centimetres (the live API uses
        # seconds·1000 and metres — different units)
        "dur_min": round(dur / 60000, 1) if isinstance(dur, (int, float)) else None,
        "dist_km": round(dist / 100000, 2) if isinstance(dist, (int, float)) else None,
        "avg_hr": _int(a.get("avgHr")),
        "max_hr": _int(a.get("maxHr")),
        "load": a.get("activityTrainingLoad"),
    }
    return aid, row


def _series_from_points(points: list) -> list:
    """[(dist_m, speed_mps, hr, altitude_m), ...] → the [{d, p, hr, e}] series our charts
    use, downsampled to ~150 points (same shape as ``client.fetch_activity_series``,
    EP-15's ``e`` field included). A 3-tuple (no altitude) is accepted too, for callers
    that never read a FIT ``altitude``/``enhanced_altitude`` field — ``e`` is then
    ``None`` on every point, same as an old pre-backfill series."""
    pts = [p for p in points if p[1] is not None or p[2] is not None]
    if len(pts) < 2:
        return []
    step = max(1, len(pts) // 150)
    out = []
    for p in pts[::step]:
        d, s, hr = p[0], p[1], p[2]
        alt = p[3] if len(p) > 3 else None
        out.append({
            "d": round(d / 1000, 2) if isinstance(d, (int, float)) else None,
            "p": round((1000.0 / s) / 60, 2) if isinstance(s, (int, float)) and s > 0 else None,
            "hr": int(hr) if isinstance(hr, (int, float)) else None,
            "e": round(alt, 1) if isinstance(alt, (int, float)) else None,
        })
    return out


# FIT files smaller than this are device settings / monitoring snapshots with no
# records — almost half the export. A real run (records every few seconds) is many KB,
# so skipping them by the zip-directory size (no decompression) avoids ~half the parses.
_FIT_MIN_BYTES = 4000


def _descend_to_di(folder: str, marker: str) -> str:
    """If ``folder`` doesn't directly contain ``marker``, descend into a ``DI_CONNECT``
    subfolder that does. Mirrors both importers' 'top-level export vs DI_CONNECT' probe."""
    if os.path.isdir(os.path.join(folder, marker)):
        return folder
    inner = os.path.join(folder, "DI_CONNECT")
    if os.path.isdir(inner):
        return inner
    return folder


def _ts_ms(ts) -> Optional[int]:
    """A FIT record ``timestamp`` (naive UTC datetime) → epoch milliseconds, to match an
    activity's ``beginTimestamp``. Returns None for anything not a datetime."""
    if not isinstance(ts, dt.datetime):
        return None
    return int(ts.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def _record_speed(m):
    """A FIT ``record``'s speed — prefer ``enhanced_speed`` (higher precision), fall back
    to ``speed``."""
    s = m.get_value("enhanced_speed")
    return s if s is not None else m.get_value("speed")


def _record_altitude(m):
    """A FIT ``record``'s altitude (metres) — EP-15: prefer ``enhanced_altitude`` (higher
    precision), fall back to ``altitude``. ``None`` when the device has no barometer/GPS
    altitude for this record — the caller treats that exactly like an old series with no
    elevation at all."""
    a = m.get_value("enhanced_altitude")
    return a if a is not None else m.get_value("altitude")


def build_targets(runs, aid_begin: dict) -> dict:
    """Map each run to its session-start timestamp (ms) via the activity index, so a FIT
    file's first-record timestamp resolves straight to a run. FIT filenames are upload ids,
    not activity ids (CLAUDE.md) — the session start time is the only join key."""
    return {aid_begin[r.activity_id]: r for r in runs if r.activity_id in aid_begin}


def read_fit_activity(messages, resolve_target):
    """Stream one FIT file's messages **once** and return ``(target, points)``.

    ``resolve_target(start_ms)`` maps the first-record timestamp (== the session start ==
    the activity ``beginTimestamp``) to the run row we want, or None to skip. Bails early:
    a ``file_id`` that isn't an ``activity``, a missing/unresolvable first-record timestamp,
    all short-circuit before decoding the rest — so the ~half of the export that isn't a
    run we need is cheap. ``messages`` is any iterable of objects with ``.name`` and
    ``.get_value(key)`` (a ``fitparse.FitFile``, or fakes in tests) — no fitparse import
    here, keeping this pure and unit-testable."""
    target = None
    points: list = []
    for m in messages:
        name = m.name
        if name == "file_id":
            if m.get_value("type") != "activity":
                return None, []
        elif name == "record":
            if target is None:               # first record → the start timestamp
                start_ms = _ts_ms(m.get_value("timestamp"))
                if start_ms is None:
                    return None, []
                target = resolve_target(start_ms)
                if target is None:           # not a run we need — stop reading this file
                    return None, []
            points.append((m.get_value("distance"), _record_speed(m),
                           m.get_value("heart_rate"), _record_altitude(m)))
    return target, points


def _iter_fit_bytes(uploaded: str):
    """Yield the raw bytes of every activity-sized ``.fit`` member across the upload zips
    (skipping tiny non-activity files by their zip-directory size, no decompression). A
    flat generator so the caller's scan loop stays a single level of nesting and can break
    out the moment every target run is matched."""
    import zipfile

    for z in sorted(glob.glob(os.path.join(uploaded, "*.zip"))):
        try:
            zf = zipfile.ZipFile(z)
        except Exception as e:
            logger.warning(f"EXPORT fit-series skip {os.path.basename(z)}: {e}")
            continue
        for info in zf.infolist():
            if info.filename.endswith(".fit") and info.file_size >= _FIT_MIN_BYTES:
                yield zf.read(info.filename)


async def _load_series_targets(session, user_id: int, di: str, since: Optional[str]):
    """(runs, targets): the stored runs missing a series and their start-ms → run index."""
    from sqlalchemy import select

    from app.db.models import ActivityRecord

    aid_begin = {a["activityId"]: int(a["beginTimestamp"])
                 for a in parse_activities(di)
                 if a.get("activityId") and a.get("beginTimestamp")}
    # NB: `series.is_(None)` would miss rows — a JSON column stores Python None as JSON
    # `null`, not SQL NULL — so filter for a missing series in Python instead.
    stmt = select(ActivityRecord).where(
        ActivityRecord.user_id == user_id, ActivityRecord.type.like("%run%"))
    if since:
        stmt = stmt.where(ActivityRecord.date >= since)
    runs = [r for r in (await session.execute(stmt)).scalars().all() if not r.series]
    return runs, build_targets(runs, aid_begin)


async def import_fit_series(
    session, user_id: int, folder: str, since: Optional[str] = None,
) -> dict:
    """Backfill the pace/HR ``series`` for stored runs from the export's FIT files
    (``DI-Connect-Uploaded-Files``) — no API. Scans FIT files, matches each activity FIT
    to a run by its session start time (== the activity ``beginTimestamp``), parses the
    records, and stores the downsampled series. Runs only (where the charts apply).

    Cost control: the FIT filenames are upload ids (not activity ids), so we must read
    each file's session to match — but we skip tiny non-activity files by size, stop as
    soon as every target run is matched, and parse each file only once. Orchestration only;
    the parsing/matching/scan steps are the pure helpers above."""
    import io

    import fitparse

    di = _descend_to_di(folder, "DI-Connect-Fitness")
    uploaded = os.path.join(di, "DI-Connect-Uploaded-Files")
    if not os.path.isdir(uploaded):
        return {"error": "DI-Connect-Uploaded-Files not found (copy the FIT zips)"}

    runs, targets = await _load_series_targets(session, user_id, di, since)
    if not targets:
        logger.info(f"EXPORT fit-series user={user_id}: nothing to match ({len(runs)} runs)")
        return {"runs": len(runs), "series_added": 0}

    total = len(targets)
    done = scanned = 0
    resolve = lambda ms: targets.pop(ms, None)  # noqa: E731 — match + remove, None on miss
    for raw in _iter_fit_bytes(uploaded):
        scanned += 1
        if scanned % 500 == 0:
            logger.info(f"EXPORT fit-series: scanned {scanned} files, matched {done}/{total}")
        try:
            row, points = read_fit_activity(fitparse.FitFile(io.BytesIO(raw)), resolve)
        except Exception:
            continue
        if row is not None:
            series = _series_from_points(points)
            if series:
                row.series = series
                done += 1
        if not targets:                # every run filled; stop scanning
            break
    await session.commit()
    stats = {"runs": len(runs), "series_added": done, "scanned": scanned}
    logger.info(f"EXPORT fit-series user={user_id}: {stats}")
    return stats


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

    from app.db.models import ActivityRecord, DailyMetric
    from app.garmin import repository

    folder = _descend_to_di(folder, "DI-Connect-Wellness")

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

    # activities — insert only ones we don't already have (never clobber live rows that
    # carry the pace/HR series + analysis); summary fields only, no series.
    seen_aids = set((
        await session.execute(
            select(ActivityRecord.activity_id).where(ActivityRecord.user_id == user_id)
        )
    ).scalars().all())
    act_ins = 0
    for a in parse_activities(folder):
        aid, arow = _activity_row(a)
        if not aid or aid in seen_aids:
            continue
        if since is not None and (arow["date"] or "") < since:
            continue
        await repository.upsert_activity(session, user_id, aid, arow)
        seen_aids.add(aid)
        act_ins += 1

    await session.commit()
    stats = {"parsed": len(days), "inserted": inserted, "filled": filled,
             "unchanged": unchanged, "activities": act_ins}
    logger.info(f"EXPORT import user={user_id}: {stats}")
    return stats
