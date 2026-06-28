"""Garmin GDPR export → daily_metrics backfill (offline, no network)."""
import json
import os

from app.garmin import repository
from app.garmin.export_import import import_export, parse_export


def _write(folder, rel, data):
    p = os.path.join(folder, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_parse_export_builds_day_from_multiple_files(tmp_path):
    f = str(tmp_path)
    _write(f, "DI-Connect-Wellness/a_sleepData.json", [{
        "calendarDate": "2025-01-10", "deepSleepSeconds": 3600,
        "lightSleepSeconds": 7200, "remSleepSeconds": 5400, "awakeSleepSeconds": 600,
        "sleepScores": {"overallScore": 80}, "avgSleepStress": 18.6,
        "awakeCount": 1, "restlessMomentCount": 30,
    }])
    _write(f, "DI-Connect-Aggregator/a_UDSFile.json", [{
        "calendarDate": "2025-01-10", "totalSteps": 9000, "restingHeartRate": 49,
        "moderateIntensityMinutes": 25,
        "bodyBattery": {"chargedValue": 60, "drainedValue": 55},
    }])
    _write(f, "DI-Connect-Metrics/a_RunRacePredictions.json", [{
        "calendarDate": "2025-01-10", "raceTime5K": 1500,
    }])
    _write(f, "DI-Connect-Wellness/a_healthStatusData.json", [{
        "calendarDate": "2025-01-10", "metrics": [
            {"type": "HRV", "value": 52.0, "baselineLowerLimit": 38.0, "baselineUpperLimit": 60.0},
            {"type": "HR", "value": 53.0}],
    }])
    # a record with a non-ISO calendarDate must be ignored, not crash the sort
    _write(f, "DI-Connect-Metrics/b_UDSFile.json", [{"calendarDate": 1700000000, "totalSteps": 1}])

    d = parse_export(f)["2025-01-10"]
    assert d["sleep_score"] == 80 and d["deep_h"] == 1.0
    assert d["hrv_avg"] == 52                 # from healthStatusData metrics
    assert d["extra"]["hrv_baseline_low"] == 38.0
    assert d["stress_avg"] == 19              # float 18.6 rounded to int column
    assert d["bb_charged"] == 60 and d["bb_drained"] == 55
    assert d["extra"]["resting_hr"] == 49 and d["extra"]["steps"] == 9000
    assert d["extra"]["race_5k_s"] == 1500
    assert d["has_data"] is True


async def test_import_export_inserts_and_is_idempotent(session, tmp_path):
    f = str(tmp_path)
    _write(f, "DI-Connect-Aggregator/a_UDSFile.json", [{
        "calendarDate": "2025-02-01", "totalSteps": 7000, "restingHeartRate": 50,
        "bodyBattery": {"chargedValue": 50, "drainedValue": 45},
    }])
    st = await import_export(session, 1, f)
    assert st["inserted"] == 1
    got = await repository.read_daily_metrics(session, 1, ["2025-02-01"])
    assert got["2025-02-01"].extra["steps"] == 7000 and got["2025-02-01"].bb_charged == 50

    # re-running changes nothing
    st2 = await import_export(session, 1, f)
    assert st2["inserted"] == 0 and st2["filled"] == 0 and st2["unchanged"] == 1


async def test_import_export_merge_fills_gaps_without_clobbering(session, tmp_path):
    from app.garmin.schemas import DailySummary

    # an existing day with HRV but no sleep_score (e.g. live-fetched, or a partial import)
    await repository.upsert_daily(session, 1, DailySummary(
        date="2025-03-01", hrv_avg=60, hrv_status="BALANCED", has_data=True,
        extra={"resting_hr": 48}))
    await session.commit()

    f = str(tmp_path)
    _write(f, "DI-Connect-Wellness/a_sleepData.json", [{
        "calendarDate": "2025-03-01", "deepSleepSeconds": 3600,
        "lightSleepSeconds": 3600, "remSleepSeconds": 3600, "awakeSleepSeconds": 0,
        "sleepScores": {"overallScore": 88}}])
    _write(f, "DI-Connect-Aggregator/a_UDSFile.json", [{
        "calendarDate": "2025-03-01", "totalSteps": 8000, "restingHeartRate": 99}])

    st = await import_export(session, 1, f)
    assert st["filled"] == 1
    got = (await repository.read_daily_metrics(session, 1, ["2025-03-01"]))["2025-03-01"]
    assert got.sleep_score == 88              # null column filled
    assert got.hrv_status == "BALANCED"       # live value not clobbered
    assert got.extra["resting_hr"] == 48      # existing extra key kept (not 99)
    assert got.extra["steps"] == 8000         # new extra key added
