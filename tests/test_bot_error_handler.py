"""on_error: an unhandled exception during a callback-query tap (plan/adapt/checkin
buttons) must not leave the user staring at an unchanged button forever — a toast
answer tells them the tap failed and to retry."""
import logging
from types import SimpleNamespace

from telegram import Update
from telegram.error import NetworkError

from bot import handlers
from bot.handlers import on_error


class _FakeCBQ:
    def __init__(self):
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


async def test_unhandled_error_on_callback_answers_with_toast():
    cbq = _FakeCBQ()
    update = Update(update_id=1, callback_query=cbq)
    ctx = SimpleNamespace(error=RuntimeError("boom"))
    await on_error(update, ctx)
    assert len(cbq.answers) == 1
    text, show_alert = cbq.answers[0]
    assert text
    assert show_alert is False


async def test_error_on_plain_message_update_does_not_touch_callback():
    update = Update(update_id=1)
    ctx = SimpleNamespace(error=RuntimeError("boom"))
    await on_error(update, ctx)  # must not raise


# ---------- network errors: a dropped long-poll must not page the owner ----------

class _Job:
    pass


def _reset_streak():
    handlers._net_streak.update(first=None, last=None)


async def test_a_single_dropped_poll_connection_is_not_a_warning(caplog):
    """`httpx.ReadError` with no message = the long-poll socket died. PTB reconnects a
    second later, so warning about it only wakes the owner (app.core.alerts) for nothing."""
    _reset_streak()
    with caplog.at_level(logging.INFO, logger="bot"):
        await on_error(None, SimpleNamespace(error=NetworkError("")))
    rec = [r for r in caplog.records if r.name == "bot"]
    assert len(rec) == 1 and rec[0].levelno == logging.INFO


async def test_polling_that_keeps_failing_does_warn(caplog, monkeypatch):
    """An outage is a streak, not a blip — once it has run past the threshold, say so."""
    _reset_streak()
    clock = {"t": 1000.0}
    monkeypatch.setattr(handlers.time, "monotonic", lambda: clock["t"])
    with caplog.at_level(logging.INFO, logger="bot"):
        await on_error(None, SimpleNamespace(error=NetworkError("")))
        clock["t"] += handlers._NET_WARN_AFTER_S + 1
        await on_error(None, SimpleNamespace(error=NetworkError("")))
        clock["t"] += 30                       # still down: the text must stay identical,
        await on_error(None, SimpleNamespace(error=NetworkError("")))   # so alerts dedupe it
    rec = [r for r in caplog.records if r.name == "bot"]
    assert [r.levelno for r in rec] == [logging.INFO, logging.WARNING, logging.WARNING]
    assert rec[1].getMessage() == rec[2].getMessage()


async def test_a_gap_means_it_recovered_and_the_streak_starts_over(caplog, monkeypatch):
    _reset_streak()
    clock = {"t": 1000.0}
    monkeypatch.setattr(handlers.time, "monotonic", lambda: clock["t"])
    with caplog.at_level(logging.INFO, logger="bot"):
        await on_error(None, SimpleNamespace(error=NetworkError("")))
        clock["t"] += handlers._NET_STREAK_GAP_S + 1       # hours of quiet polling
        await on_error(None, SimpleNamespace(error=NetworkError("")))
    assert all(r.levelno == logging.INFO for r in caplog.records if r.name == "bot")


async def test_a_network_error_that_lost_a_reply_or_a_report_always_warns(caplog):
    """Not the retry loop: this one cost the user something, however brief the blip."""
    _reset_streak()
    with caplog.at_level(logging.INFO, logger="bot"):
        await on_error(Update(update_id=1), SimpleNamespace(error=NetworkError("")))
        await on_error(None, SimpleNamespace(error=NetworkError(""), job=_Job()))
    rec = [r for r in caplog.records if r.name == "bot"]
    assert [r.levelno for r in rec] == [logging.WARNING, logging.WARNING]
