"""Infrastructure alerts must speak with the SYSTEM bot's voice, not the coach's.

The morning tick runs inside the product-bot process, so `ctx.bot` is right there and
"⚠️ Off-SD копія бекапу (rsync) не вдалась" used to land in the athlete's coaching
thread, between a sleep score and tomorrow's intervals. It belongs on the admin bot,
next to /deploy.
"""
from types import SimpleNamespace

import pytest

from app.core.config import settings
from bot import opsalert


class _Recorder:
    def __init__(self, boom: Exception | None = None):
        self.sent = []
        self.boom = boom

    async def send_message(self, chat_id, text, **kwargs):
        if self.boom is not None:
            raise self.boom
        self.sent.append((chat_id, text))


def _ctx():
    return SimpleNamespace(bot=_Recorder())


@pytest.fixture(autouse=True)
def _clear_cached_bot(monkeypatch):
    monkeypatch.setattr(opsalert, "_admin_bot", None)


async def test_alert_goes_out_over_the_admin_bot(monkeypatch):
    admin = _Recorder()
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", "1:admin", raising=False)
    monkeypatch.setattr(opsalert, "Bot", lambda token: admin)

    ctx = _ctx()
    await opsalert.send_ops_alert(ctx, 555, "⚠️ бекап")
    assert admin.sent == [(555, "⚠️ бекап")]
    assert ctx.bot.sent == []          # the coaching bot stays out of it


async def test_single_bot_install_still_gets_the_alert(monkeypatch):
    """No admin token configured — the alert is worth more than the channel it uses."""
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None, raising=False)
    ctx = _ctx()
    await opsalert.send_ops_alert(ctx, 555, "⚠️ бекап")
    assert ctx.bot.sent == [(555, "⚠️ бекап")]


async def test_unreachable_admin_bot_falls_back_to_the_product_bot(monkeypatch, caplog):
    """An admin who never pressed Start on the system bot can't be written to — Telegram
    forbids a bot opening a conversation. Losing the alert there would be the worst of
    both worlds."""
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", "1:admin", raising=False)
    monkeypatch.setattr(
        opsalert, "Bot", lambda token: _Recorder(boom=RuntimeError("chat not found")))

    ctx = _ctx()
    with caplog.at_level("WARNING", logger="bot"):
        await opsalert.send_ops_alert(ctx, 555, "⚠️ бекап")
    assert ctx.bot.sent == [(555, "⚠️ бекап")]
    assert "chat not found" in caplog.text


async def test_the_admin_bot_is_built_once(monkeypatch):
    built = []

    def _factory(token):
        built.append(token)
        return _Recorder()

    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", "1:admin", raising=False)
    monkeypatch.setattr(opsalert, "Bot", _factory)
    for _ in range(3):
        await opsalert.send_ops_alert(_ctx(), 555, "x")
    assert built == ["1:admin"]
