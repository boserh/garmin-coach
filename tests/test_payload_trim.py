"""What the daily report actually puts in front of the analyst.

The morning report is a needle-in-haystack task — one day's session among twenty
activities — and it failed as one: a 2-hour kite session went unmentioned while two
auto-detected evening walks were narrated as "two short bike rides", the word "bike"
carried over from the previous day's report (where the auto-detections really were
cycling). The payload was not wrong; it was diluted, and half of what diluted it was
data the system prompt documents no meaning for.

These pin the trim: undocumented and duplicated fields never reach the prompt, the
activity list is bounded by a window rather than only a count, and — the invariant that
keeps the dedup cache honest — a field trimmed out of the prompt is trimmed out of the
cache key too.
"""
import datetime as dt
import pathlib

from app.analysis.cache import (
    ACTIVITY_CONTEXT_MIN_DAYS,
    ACTIVITY_CONTEXT_MIN_KEEP,
    _as_dict,
    _cache_key,
)

TODAY = "2026-09-02"
PROMPTS = pathlib.Path(__file__).resolve().parent.parent / "app" / "analysis" / "prompts.py"


def _day(date: str, **extra) -> dict:
    return {
        "date": date, "hrv_avg": 46, "hrv_status": "UNBALANCED", "sleep_score": 83,
        "bb_charged": 58, "bb_drained": 1,
        "extra": {
            "resting_hr": 51, "readiness_score": 74, "readiness_level": "MODERATE",
            "acwr_pct": 100, "acute_load": 131, "recovery_time_h": 0.0,
            "hrv_baseline_low": 46, "sleep_start": "23:35", "skin_temp_dev_c": -0.1,
            "steps": 39, "auto_activities": "20:24 walking 24хв",
            # the ones that should not survive
            "overnight_hrv": 46.0, "bb_change": 58, "hrv_weekly_avg": 45,
            "restless_moments": 57, "min_hr": 47, "bb_high": 76, "floors_up": 0.0,
            "race_5k_s": 1672, "race_marathon_s": 19365, "endurance_score": 4951,
            **extra,
        },
    }


def _act(date: str, **kw) -> dict:
    return {
        "date": date, "type": "kiteboarding_v2", "dur_min": 127.7, "dist_km": 5.57,
        "avg_hr": 106, "max_hr": 141, "load": 15.9,
        "zones": {"te_aer": 1.8, "z1_s": 3350, "z2_s": 2135},
        "start_lat": 54.634, "start_lon": 18.512, "gear_id": "abc",
        "series": [{"d": 1, "p": 6.5}] * 500,
        **kw,
    }


def _payload(window_days: int, dates: list[str]) -> dict:
    return {
        "window_days": window_days,
        "daily": [_day(TODAY)],
        "recent_activities": [_act(d) for d in dates],
        "planned_runs": [],
    }


def test_daily_extra_keeps_what_the_prompt_documents_and_drops_the_rest():
    extra = _as_dict(_payload(3, [TODAY]), today=TODAY)["daily"][0]["extra"]
    # Documented, or genuinely read by the analyst — must survive.
    for key in ("resting_hr", "readiness_score", "readiness_level", "acwr_pct",
                "acute_load", "recovery_time_h", "hrv_baseline_low", "steps",
                "sleep_start", "skin_temp_dev_c", "auto_activities"):
        assert key in extra, key
    # Duplicates of a modelled column, undocumented noise, and values `fitness` carries.
    for key in ("overnight_hrv", "bb_change", "hrv_weekly_avg", "restless_moments",
                "min_hr", "bb_high", "floors_up", "race_5k_s", "race_marathon_s",
                "endurance_score"):
        assert key not in extra, key


def test_activity_rows_lose_what_the_analyst_is_told_nothing_about():
    act = _as_dict(_payload(3, [TODAY]), today=TODAY)["recent_activities"][0]
    for key in ("type", "dur_min", "dist_km", "avg_hr", "max_hr", "load", "date"):
        assert key in act, key
    # `series` was always stripped; zones/coords/gear are the new ones — all four are
    # either undocumented in the prompt or read from the DB, never from the payload.
    for key in ("series", "zones", "start_lat", "start_lon", "gear_id"):
        assert key not in act, key


def test_a_short_window_no_longer_drags_three_weeks_of_activities_along():
    """activity_limit is a COUNT: a 3-day morning report was handed 20 activities spanning
    24 days. The window follows window_days with a floor, so the holiday hiking goes."""
    today = dt.date.fromisoformat(TODAY)
    dates = [(today - dt.timedelta(days=n)).isoformat() for n in (1, 3, 5, 9, 12, 20, 24)]
    kept = _as_dict(_payload(3, dates), today=TODAY)["recent_activities"]
    assert [a["date"] for a in kept] == dates[:4]   # everything inside 10 days
    assert ACTIVITY_CONTEXT_MIN_DAYS == 10


def test_a_deep_dive_keeps_its_longer_history():
    today = dt.date.fromisoformat(TODAY)
    dates = [(today - dt.timedelta(days=n)).isoformat() for n in (1, 9, 13, 20)]
    kept = _as_dict(_payload(14, dates), today=TODAY)["recent_activities"]
    assert [a["date"] for a in kept] == dates[:3]   # 14-day window, not 10


def test_an_idle_athlete_still_sees_their_last_sessions():
    """Trimming must never answer "what did I last do?" with silence."""
    today = dt.date.fromisoformat(TODAY)
    dates = [(today - dt.timedelta(days=n)).isoformat() for n in (40, 45, 50, 60)]
    kept = _as_dict(_payload(3, dates), today=TODAY)["recent_activities"]
    assert len(kept) == ACTIVITY_CONTEXT_MIN_KEEP
    assert [a["date"] for a in kept] == dates[:ACTIVITY_CONTEXT_MIN_KEEP]


def test_without_a_date_nothing_is_trimmed_by_age():
    dates = ["2026-09-01", "2026-07-01"]
    kept = _as_dict(_payload(3, dates), today=None)["recent_activities"]
    assert [a["date"] for a in kept] == dates


def test_a_dropped_field_does_not_bust_the_dedup_cache():
    """The mirror of the README pitfall. Every piece of Claude context must be part of the
    key — so anything trimmed OUT of the prompt has to be trimmed out of the key too, or an
    invisible field (a jittering spo2_low, say) pays for a fresh Opus call every morning."""
    base = _payload(3, [TODAY])
    noisy = _payload(3, [TODAY])
    noisy["daily"][0]["extra"]["min_hr"] = 999
    noisy["recent_activities"][0]["start_lat"] = 0.0
    key = _cache_key(_as_dict(base, today=TODAY), "q", "m", today=TODAY)
    assert key == _cache_key(_as_dict(noisy, today=TODAY), "q", "m", today=TODAY)


def test_a_kept_field_still_busts_the_dedup_cache():
    base = _payload(3, [TODAY])
    changed = _payload(3, [TODAY])
    changed["daily"][0]["extra"]["readiness_score"] = 41
    key = _cache_key(_as_dict(base, today=TODAY), "q", "m", today=TODAY)
    assert key != _cache_key(_as_dict(changed, today=TODAY), "q", "m", today=TODAY)


def test_the_prompt_forbids_the_failure_that_started_this():
    """Two rules, both violated by the report this change came from: an activity type
    copied out of previous_report, and a day described only by its auto-detections."""
    src = PROMPTS.read_text(encoding="utf-8")
    assert "ПРІОРИТЕТ:" in src and "auto_activities" in src
    assert "ТИП БЕРИ ДОСЛІВНО" in src
    assert "УВАГА ЗІ ЗМІСТОМ" in src
