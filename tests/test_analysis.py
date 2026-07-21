"""Analysis-layer unit tests that need no API key."""
import types

import pytest

from app.analysis import service
from app.analysis.reports import _strength_exercises
from app.analysis.service import _ask_cache_key, _cache_key

_DATA = {"daily": [], "recent_activities": [], "planned_runs": []}


def _wo(**kw):
    kw.setdefault("type", "strength")
    kw.setdefault("strength_plan", None)
    kw.setdefault("strength_snapshot", None)
    return types.SimpleNamespace(**kw)


def test_strength_exercises_from_snapshot():
    # ST-09: a clone day's exercises come from the build-time snapshot (display-only).
    w = _wo(strength_snapshot={"name": "Day 2",
                               "exercises": [{"category": "BENCH_PRESS", "exercise": "Incline",
                                              "reps": 12},
                                             {"category": "PLANK"}]})
    got = _strength_exercises(w)
    assert got["name"] == "Day 2"
    assert got["exercises"] == [
        {"category": "BENCH_PRESS", "exercise": "Incline", "reps": 12},
        {"category": "PLANK"},
    ]


def test_strength_exercises_from_plan_blocks():
    w = _wo(strength_plan={"name": "Все тіло",
                           "blocks": [{"reps": 3, "exercises": [{"category": "SQUAT", "reps": 10}]},
                                      {"exercises": [{"category": "ROW", "reps": 8}]}]})
    got = _strength_exercises(w)
    assert got == {"name": "Все тіло",
                   "exercises": [{"category": "SQUAT", "reps": 10},
                                 {"category": "ROW", "reps": 8}]}


def test_strength_exercises_empty_snapshot_is_none():
    # The JSON-null gotcha: an empty snapshot deserialises to Python None → no exercises,
    # so the report says "силова за планом" instead of inventing a muscle group.
    assert _strength_exercises(_wo(strength_snapshot=None)) is None
    assert _strength_exercises(_wo(strength_snapshot={"exercises": []})) is None
    non_strength = _wo(type="easy", strength_snapshot={"exercises": [{"category": "X"}]})
    assert _strength_exercises(non_strength) is None


_PREV = {"date": "2026-06-21", "text": "учора все ок"}


def test_cache_key_includes_previous_report():
    base = _cache_key(_DATA, "q", "claude-sonnet-4-6")
    with_prev = _cache_key(_DATA, "q", "claude-sonnet-4-6", previous_report=_PREV)
    assert base != with_prev


def test_cache_key_stable_for_same_inputs():
    a = _cache_key(_DATA, "q", "claude-sonnet-4-6", previous_report=_PREV)
    b = _cache_key(_DATA, "q", "claude-sonnet-4-6", previous_report=dict(_PREV))
    assert a == b


def test_cache_key_varies_with_weather():
    base = _cache_key(_DATA, "q", "claude-sonnet-4-6")
    wx = {"t_max_c": 28, "summary": "ясно"}
    with_wx = _cache_key(_DATA, "q", "claude-sonnet-4-6", weather=wx)
    assert base != with_wx
    # a different forecast → a different key (so a fresh report, not a stale cache hit)
    assert with_wx != _cache_key(_DATA, "q", "claude-sonnet-4-6", weather={"t_max_c": 15})


_REPORTS = [{"date": "2026-06-22", "text": "звіт B"},
            {"date": "2026-06-21", "text": "звіт A"}]


def test_cache_key_varies_with_plan_today():
    base = _cache_key(_DATA, "q", "claude-sonnet-4-6")
    plan = [{"date": "2026-07-03", "type": "tempo", "dist_km": 8.0, "description": "темп"}]
    with_plan = _cache_key(_DATA, "q", "claude-sonnet-4-6", plan_today=plan)
    assert base != with_plan


def test_cache_key_stable_without_plan_changes():
    plan = [{"date": "2026-07-03", "type": "easy", "dist_km": 5.0}]
    a = _cache_key(_DATA, "q", "claude-sonnet-4-6", plan_today=plan)
    b = _cache_key(_DATA, "q", "claude-sonnet-4-6", plan_today=list(plan))
    assert a == b


def test_cache_key_changes_when_plan_changes():
    plan_a = [{"date": "2026-07-03", "type": "easy", "dist_km": 5.0}]
    plan_b = [{"date": "2026-07-03", "type": "tempo", "dist_km": 8.0}]
    assert _cache_key(_DATA, "q", "claude-sonnet-4-6", plan_today=plan_a) != \
           _cache_key(_DATA, "q", "claude-sonnet-4-6", plan_today=plan_b)


def test_cache_key_varies_with_subjective():
    # EP-12: felt-effort context must key the cache (the README наскрізна pitfall).
    base = _cache_key(_DATA, "q", "claude-sonnet-4-6")
    subj = {"n": 2, "rpe_rising": True, "recurring_pain": {"part": "коліно", "count": 2}}
    assert base != _cache_key(_DATA, "q", "claude-sonnet-4-6", subjective=subj)


def test_cache_key_varies_with_health_alerts():
    # ST-10: an actionable health-alert report must key the cache (the README
    # наскрізна pitfall — every piece of Claude context enters the dedup key).
    base = _cache_key(_DATA, "q", "claude-sonnet-4-6")
    alerts = {"level": "alert", "alerts": [{"kind": "hrv_low", "severity": 1,
                                            "detail": "HRV нижче норми 3 дні"}]}
    assert base != _cache_key(_DATA, "q", "claude-sonnet-4-6", health_alerts=alerts)


def test_ask_cache_key_varies_with_question_and_reports():
    base = _ask_cache_key(_REPORTS, "чи бігти?", "claude-sonnet-4-6", [])
    assert base != _ask_cache_key(_REPORTS, "інше питання", "claude-sonnet-4-6", [])
    assert base != _ask_cache_key(_REPORTS[:1], "чи бігти?", "claude-sonnet-4-6", [])
    # the recent-ask thread is part of the key
    asks = [{"question": "а вчора?", "answer": "ок"}]
    assert base != _ask_cache_key(_REPORTS, "чи бігти?", "claude-sonnet-4-6", asks)
    # stable for equal inputs
    assert base == _ask_cache_key(list(_REPORTS), "чи бігти?", "claude-sonnet-4-6", [])


def test_ask_cache_key_varies_with_last_data_date():
    # EP-09: keyed on the coarse daily-data slice, not just the calendar date — a repeat
    # question before today's data has synced should still be a cache hit.
    a = _ask_cache_key(_REPORTS, "чи бігти?", "claude-sonnet-4-6", [], "2026-07-17")
    b = _ask_cache_key(_REPORTS, "чи бігти?", "claude-sonnet-4-6", [], "2026-07-18")
    assert a != b
    # stable for the same last_data_date
    assert a == _ask_cache_key(list(_REPORTS), "чи бігти?", "claude-sonnet-4-6", [], "2026-07-17")


_FITNESS = {"acwr_pct": 95.0, "readiness_score": 72, "resting_hr": 52}


def test_cache_key_includes_fitness():
    base = _cache_key(_DATA, "q", "claude-sonnet-4-6")
    with_fitness = _cache_key(_DATA, "q", "claude-sonnet-4-6", fitness=_FITNESS)
    assert base != with_fitness


def test_cache_key_stable_with_same_fitness():
    a = _cache_key(_DATA, "q", "claude-sonnet-4-6", fitness=_FITNESS)
    b = _cache_key(_DATA, "q", "claude-sonnet-4-6", fitness=dict(_FITNESS))
    assert a == b


def test_cache_key_none_fitness_equals_no_fitness():
    """None fitness (no history) produces the same key as omitting the arg."""
    assert _cache_key(_DATA, "q", "claude-sonnet-4-6", fitness=None) == \
           _cache_key(_DATA, "q", "claude-sonnet-4-6")


def test_build_fitness_snapshot_empty_returns_none():
    from app.analysis.service import _build_fitness_snapshot
    assert _build_fitness_snapshot({}) is None
    assert _build_fitness_snapshot({"unknown_key": 42}) is None


def test_build_fitness_snapshot_filters_nulls_and_unknown():
    from app.analysis.service import _build_fitness_snapshot
    snap = _build_fitness_snapshot({
        "vo2max": 46.5,
        "readiness_score": None,
        "unknown_key": 99,
    })
    assert snap == {"vo2max": 46.5}


def test_build_fitness_snapshot_known_keys_pass_through():
    from app.analysis.service import _build_fitness_snapshot
    ex = {"acwr_pct": 110.0, "recovery_time_h": 18, "resting_hr": 50, "hrv_baseline_low": 42}
    snap = _build_fitness_snapshot(ex)
    assert snap == ex


def test_get_client_caches_per_key(monkeypatch):
    import anthropic

    created = []

    class FakeAnthropic:
        def __init__(self, api_key):
            created.append(api_key)

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    service._clients.clear()

    c1 = service._get_client("key-1")
    c2 = service._get_client("key-1")   # cached
    c3 = service._get_client("key-2")   # different user → different client
    assert c1 is c2 and c1 is not c3
    assert created == ["key-1", "key-2"]


def test_get_client_without_key_raises(monkeypatch):
    monkeypatch.setattr(service.settings, "ANTHROPIC_API_KEY", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(service.AnalystError):
        service._get_client(None)
