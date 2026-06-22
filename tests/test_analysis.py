"""Analysis-layer unit tests that need no API key."""
from app.analysis.service import _ask_cache_key, _cache_key

_DATA = {"daily": [], "recent_activities": [], "planned_runs": []}


_PREV = {"date": "2026-06-21", "text": "учора все ок"}


def test_cache_key_includes_previous_report():
    base = _cache_key(_DATA, "q", "claude-sonnet-4-6")
    with_prev = _cache_key(_DATA, "q", "claude-sonnet-4-6", previous_report=_PREV)
    assert base != with_prev


def test_cache_key_stable_for_same_inputs():
    a = _cache_key(_DATA, "q", "claude-sonnet-4-6", previous_report=_PREV)
    b = _cache_key(_DATA, "q", "claude-sonnet-4-6", previous_report=dict(_PREV))
    assert a == b


_REPORTS = [{"date": "2026-06-22", "text": "звіт B"},
            {"date": "2026-06-21", "text": "звіт A"}]


def test_ask_cache_key_varies_with_question_and_reports():
    base = _ask_cache_key(_REPORTS, "чи бігти?", "claude-sonnet-4-6")
    assert base != _ask_cache_key(_REPORTS, "інше питання", "claude-sonnet-4-6")
    assert base != _ask_cache_key(_REPORTS[:1], "чи бігти?", "claude-sonnet-4-6")
    # stable for equal inputs
    assert base == _ask_cache_key(list(_REPORTS), "чи бігти?", "claude-sonnet-4-6")
