"""Analysis-layer unit tests that need no API key."""
import pytest

from app.analysis import service
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


def test_cache_key_varies_with_weather():
    base = _cache_key(_DATA, "q", "claude-sonnet-4-6")
    wx = {"t_max_c": 28, "summary": "ясно"}
    with_wx = _cache_key(_DATA, "q", "claude-sonnet-4-6", weather=wx)
    assert base != with_wx
    # a different forecast → a different key (so a fresh report, not a stale cache hit)
    assert with_wx != _cache_key(_DATA, "q", "claude-sonnet-4-6", weather={"t_max_c": 15})


_REPORTS = [{"date": "2026-06-22", "text": "звіт B"},
            {"date": "2026-06-21", "text": "звіт A"}]


def test_ask_cache_key_varies_with_question_and_reports():
    base = _ask_cache_key(_REPORTS, "чи бігти?", "claude-sonnet-4-6", [])
    assert base != _ask_cache_key(_REPORTS, "інше питання", "claude-sonnet-4-6", [])
    assert base != _ask_cache_key(_REPORTS[:1], "чи бігти?", "claude-sonnet-4-6", [])
    # the recent-ask thread is part of the key
    asks = [{"question": "а вчора?", "answer": "ок"}]
    assert base != _ask_cache_key(_REPORTS, "чи бігти?", "claude-sonnet-4-6", asks)
    # stable for equal inputs
    assert base == _ask_cache_key(list(_REPORTS), "чи бігти?", "claude-sonnet-4-6", [])


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
