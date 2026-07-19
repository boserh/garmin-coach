"""on_error: an unhandled exception during a callback-query tap (plan/adapt/checkin
buttons) must not leave the user staring at an unchanged button forever — a toast
answer tells them the tap failed and to retry."""
from types import SimpleNamespace

from telegram import Update

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
