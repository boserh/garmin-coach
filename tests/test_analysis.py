"""Analysis-layer unit tests that need no API key."""
from app.analysis.service import _cache_key

_DATA = {"daily": [], "recent_activities": [], "planned_runs": []}


def test_cache_key_includes_previous_report():
    base = _cache_key(_DATA, "q", "claude-sonnet-4-6")
    with_prev = _cache_key(_DATA, "q", "claude-sonnet-4-6", previous_report="yesterday")
    assert base != with_prev


def test_cache_key_stable_for_same_inputs():
    a = _cache_key(_DATA, "q", "claude-sonnet-4-6", previous_report="x")
    b = _cache_key(_DATA, "q", "claude-sonnet-4-6", previous_report="x")
    assert a == b
