"""app.core.alerts: a page for `logger.exception(...)` must say what broke.

"🛑 [bot] TICK failed for user=1" carried the cause only in exc_info, which the handler
dropped — the owner had to go read the Pi's log to learn anything at all.
"""
import logging

import pytest

from app.core import alerts


class _SyncThread:
    def __init__(self, target, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(alerts.settings, "TELEGRAM_ADMIN_BOT_TOKEN", "t")
    monkeypatch.setattr(alerts, "_resolve_owner_chat_id", lambda: 42)
    monkeypatch.setattr(alerts, "_send", lambda token, chat, text: out.append(text))
    monkeypatch.setattr(alerts.threading, "Thread", _SyncThread)
    alerts._recent.clear()
    yield out
    alerts._recent.clear()


def _emit(msg, exc=None, level=logging.ERROR):
    exc_info = (type(exc), exc, None) if exc is not None else None
    rec = logging.LogRecord("bot", level, __file__, 1, msg, None, exc_info)
    alerts.TelegramAlertHandler().emit(rec)


def test_exception_one_liner_rides_along(sent):
    _emit("TICK failed for user=1", KeyError("plan_id"))
    assert sent == ["🛑 [bot] TICK failed for user=1\nKeyError: 'plan_id'"]


def test_plain_warning_is_unchanged(sent):
    _emit("TICK Garmin rate-limited user=1", level=logging.WARNING)
    assert sent == ["⚠️ [bot] TICK Garmin rate-limited user=1"]


def test_exception_without_message_shows_its_type(sent):
    _emit("Unhandled bot error", RuntimeError())
    assert sent == ["🛑 [bot] Unhandled bot error\nRuntimeError"]


def test_two_different_failures_behind_one_message_both_page(sent):
    _emit("TICK failed for user=1", KeyError("a"))
    _emit("TICK failed for user=1", ValueError("b"))
    _emit("TICK failed for user=1", ValueError("b"))     # true duplicate: deduped
    assert len(sent) == 2
