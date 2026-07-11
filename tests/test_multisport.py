"""NF-05 multisport weekly load budget: the pure-Python load math (uniform HR/duration
TRIMP proxy), the per-ISO-week aggregation + non-run share, the repository reader over all
activity types, and that multisport enters the digest dedup-cache key."""
from app import multisport
from app.analysis.service import _digest_cache_key
from app.db.models import ActivityRecord

U1 = 1


# --- sport bucketing ----------------------------------------------------------

def test_sport_bucket_maps_known_types():
    assert multisport.sport_bucket("running") == "run"
    assert multisport.sport_bucket("trail_running") == "run"
    assert multisport.sport_bucket("cycling") == "bike"
    assert multisport.sport_bucket("lap_swimming") == "swim"
    assert multisport.sport_bucket("strength_training") == "strength"


def test_sport_bucket_unknown_is_other():
    assert multisport.sport_bucket("kiteboarding") == "other"
    assert multisport.sport_bucket("tennis") == "other"
    assert multisport.sport_bucket(None) == "other"


# --- activity load ------------------------------------------------------------

def test_hr_based_load_uses_edwards_zone():
    # 60 min at 152 bpm on a 190 max → frac 0.8 → zone 4 → 240.
    assert multisport.activity_load(type="running", dur_min=60, avg_hr=152) == 240.0


def test_low_hr_is_zone_one():
    assert multisport.activity_load(type="running", dur_min=30, avg_hr=100) == 30.0


def test_duration_fallback_when_no_hr():
    # No HR → per-sport duration weight (kite → "other" weight 2.5).
    assert multisport.activity_load(type="kiteboarding", dur_min=120, avg_hr=None) == 300.0
    # Run fallback weight is 3.0.
    assert multisport.activity_load(type="running", dur_min=40, avg_hr=0) == 120.0


def test_zero_duration_is_zero_load():
    assert multisport.activity_load(type="running", dur_min=0, avg_hr=150) == 0.0
    assert multisport.activity_load(type="running", dur_min=None, avg_hr=150) == 0.0


# --- weekly aggregation -------------------------------------------------------

def test_weekly_load_groups_by_iso_week_and_sport():
    acts = [
        {"date": "2026-07-06", "type": "running", "dur_min": 60, "avg_hr": 152},   # 240
        {"date": "2026-07-08", "type": "kiteboarding", "dur_min": 120, "avg_hr": None},  # 300
    ]
    weeks = multisport.weekly_load(acts)
    assert len(weeks) == 1
    w = weeks[0]
    assert w["load"] == 540.0
    assert w["by_sport"] == {"run": 240.0, "other": 300.0}
    assert w["sessions"] == 2
    assert w["hours"] == 3.0
    # non-run share = 300 / 540 ≈ 56%
    assert w["non_run_pct"] == 56


def test_weekly_load_skips_zero_and_bad_dates():
    acts = [
        {"date": None, "type": "running", "dur_min": 40, "avg_hr": 150},   # no date → skip
        {"date": "2026-07-06", "type": "running", "dur_min": 0, "avg_hr": 150},  # 0 load → skip
    ]
    assert multisport.weekly_load(acts) == []


def test_weekly_load_all_run_is_zero_non_run():
    acts = [{"date": "2026-07-06", "type": "running", "dur_min": 60, "avg_hr": 152}]
    assert multisport.weekly_load(acts)[0]["non_run_pct"] == 0


# --- budget summary -----------------------------------------------------------

def test_budget_summary_this_vs_prev():
    weekly = [
        {"week": "2026-W27", "load": 200.0, "by_sport": {"run": 200.0},
         "non_run_pct": 0, "sessions": 3, "hours": 2.0},
        {"week": "2026-W28", "load": 500.0, "by_sport": {"run": 200.0, "other": 300.0},
         "non_run_pct": 60, "sessions": 4, "hours": 4.0},
    ]
    s = multisport.budget_summary(weekly, "2026-W28", "2026-W27")
    assert s["load"] == 500.0
    assert s["load_prev"] == 200.0
    assert s["delta"] == 300.0
    assert s["non_run_pct"] == 60


def test_budget_summary_none_when_empty():
    assert multisport.budget_summary([], "2026-W28", "2026-W27") is None
    assert multisport.budget_summary(None, "2026-W28", "2026-W27") is None


# --- repository reader over all sports ----------------------------------------

async def test_weekly_activity_load_includes_all_sports(session):
    from app.garmin import repository

    session.add_all([
        ActivityRecord(user_id=U1, activity_id=1, date="2026-07-06",
                       type="running", dur_min=60, avg_hr=152),
        ActivityRecord(user_id=U1, activity_id=2, date="2026-07-07",
                       type="kiteboarding", dur_min=120, avg_hr=None),
        ActivityRecord(user_id=U1, activity_id=3, date="2026-07-08",
                       type="tennis", dur_min=90, avg_hr=140),
    ])
    await session.commit()
    weeks = await repository.weekly_activity_load(session, U1, weeks=52)
    assert len(weeks) == 1
    assert weeks[0]["sessions"] == 3
    # run + other (kite + tennis) all counted
    assert "run" in weeks[0]["by_sport"]
    assert "other" in weeks[0]["by_sport"]
    assert weeks[0]["non_run_pct"] > 0


# --- dedup-cache key ----------------------------------------------------------

def test_multisport_changes_digest_cache_key():
    base = {"iso_week": "2026-W28", "week": {}, "weekly_volume": None,
            "compliance": None, "recovery": None, "fitness": None, "goal": None,
            "records": None}
    k_without = _digest_cache_key(base, "claude-sonnet-5")
    k_with = _digest_cache_key({**base, "multisport": {"this_week": {"load": 500}}},
                               "claude-sonnet-5")
    assert k_without != k_with
