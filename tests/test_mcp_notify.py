"""The monitoring notify MCP server: a write-only Telegram channel, kept apart from the
read-only coach server (NF-08) at every level that matters — OAuth scope, process,
origin, and who may call it.

``mcp`` is an opt-in extra (`pip install -e ".[mcp]"`), so the tools half of this module
skips itself when it isn't installed; ``app.notify`` (the delivery half) has no such
dependency and is tested unconditionally.
"""
import pytest

from app import notify
from app.core.config import settings
from app.core.ratelimit import RateLimiter
from app.db import users


class _FakeBot:
    """Records what would have gone to Telegram. ``fail_on`` makes the Nth send raise —
    ``fail_parse_mode`` only when the send carried one, which is how a malformed-markup
    rejection actually presents."""

    def __init__(self, *, fail_parse_mode=False, fail_always=False):
        self.sent = []
        self.fail_parse_mode = fail_parse_mode
        self.fail_always = fail_always

    async def send_message(self, chat_id, text, parse_mode=None, disable_notification=False):
        if self.fail_always or (self.fail_parse_mode and parse_mode):
            raise RuntimeError("Bad Request: can't parse entities")
        self.sent.append((chat_id, text, parse_mode, disable_notification))


@pytest.fixture
def bot(monkeypatch):
    fake = _FakeBot()
    monkeypatch.setattr(notify, "_get_bot", lambda: fake)
    monkeypatch.setattr(settings, "TELEGRAM_MONITOR_CHAT_ID", -100500)
    return fake


# --- splitting (pure) ---------------------------------------------------------------


def test_split_short_message_is_one_part():
    assert notify.split_message("привіт") == ["привіт"]


def test_split_blank_message_is_no_parts():
    assert notify.split_message("   \n  ") == []


def test_split_prefers_paragraph_boundaries():
    para = "x" * 3000
    parts = notify.split_message(f"{para}\n\n{para}")
    assert len(parts) == 2
    assert parts[0] == para and parts[1] == para


def test_split_falls_back_to_a_hard_cut_on_one_long_line():
    # No separator anywhere: the text still has to leave, in Telegram-sized pieces.
    parts = notify.split_message("y" * 9000)
    assert [len(p) for p in parts] == [4000, 4000, 1000]
    assert "".join(parts) == "y" * 9000


def test_split_respects_the_limit_for_every_part():
    text = ("слово " * 3000).strip()
    assert all(len(p) <= notify.MAX_CHARS for p in notify.split_message(text))


# --- delivery -----------------------------------------------------------------------


async def test_send_delivers_one_message(bot):
    assert await notify.send_monitor_message("тривога") == 1
    assert bot.sent == [(-100500, "тривога", None, False)]


async def test_send_numbers_the_parts_of_a_long_message(bot):
    await notify.send_monitor_message("z" * 9000)
    assert len(bot.sent) == 3
    assert bot.sent[0][1].endswith("(1/3)")
    assert bot.sent[-1][1].endswith("(3/3)")


async def test_send_passes_parse_mode_and_silent_through(bot):
    await notify.send_monitor_message("*жирним*", parse_mode="Markdown", silent=True)
    assert bot.sent[0][2] == "Markdown"
    assert bot.sent[0][3] is True


async def test_malformed_markup_is_resent_as_plain_text(monkeypatch):
    """A brief that arrives unformatted beats one that doesn't arrive."""
    fake = _FakeBot(fail_parse_mode=True)
    monkeypatch.setattr(notify, "_get_bot", lambda: fake)
    monkeypatch.setattr(settings, "TELEGRAM_MONITOR_CHAT_ID", 42)
    assert await notify.send_monitor_message("_bad", parse_mode="Markdown") == 1
    assert fake.sent == [(42, "_bad", None, False)]


async def test_send_failure_raises_with_an_actionable_message(monkeypatch):
    fake = _FakeBot(fail_always=True)
    monkeypatch.setattr(notify, "_get_bot", lambda: fake)
    monkeypatch.setattr(settings, "TELEGRAM_MONITOR_CHAT_ID", 42)
    with pytest.raises(notify.NotifyError) as exc:
        await notify.send_monitor_message("щось")
    assert "press Start" in str(exc.value)


async def test_send_refuses_without_a_bot_token(monkeypatch):
    monkeypatch.setattr(notify, "_get_bot", lambda: None)
    monkeypatch.setattr(settings, "TELEGRAM_MONITOR_CHAT_ID", 42)
    with pytest.raises(notify.NotifyError, match="TELEGRAM_MONITOR_BOT_TOKEN"):
        await notify.send_monitor_message("щось")


async def test_send_refuses_without_a_chat_id(monkeypatch):
    monkeypatch.setattr(notify, "_get_bot", lambda: _FakeBot())
    monkeypatch.setattr(settings, "TELEGRAM_MONITOR_CHAT_ID", None)
    with pytest.raises(notify.NotifyError, match="TELEGRAM_MONITOR_CHAT_ID"):
        await notify.send_monitor_message("щось")


async def test_send_refuses_an_empty_message(bot):
    with pytest.raises(notify.NotifyError, match="empty"):
        await notify.send_monitor_message("   ")
    assert bot.sent == []


async def test_send_refuses_an_oversized_payload(bot):
    # The guard against an upstream client dumping raw source into the channel.
    with pytest.raises(notify.NotifyError, match="limit"):
        await notify.send_monitor_message("q" * (notify.MAX_TOTAL_CHARS + 1))
    assert bot.sent == []


# --- the MCP tool -------------------------------------------------------------------

pytest.importorskip("mcp")

from app import mcp_notify  # noqa: E402


class _FakeMaker:
    """Stand-in for async_session_maker() handing back the test's shared session."""

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


class _Token:
    def __init__(self, subject):
        self.subject = str(subject)


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch):
    """The module-level limiter is process-wide; give each test its own."""
    monkeypatch.setattr(mcp_notify, "_limiter", RateLimiter(20, 3600))


async def test_tool_sends_over_stdio_without_a_token(bot, monkeypatch):
    monkeypatch.setattr(mcp_notify, "get_access_token", lambda: None)
    got = await mcp_notify.send_message("ранковий звіт")
    assert got == {"sent": True, "parts": 1}
    assert bot.sent[0][1] == "ранковий звіт"


async def test_tool_allows_an_admin_token(session, bot, monkeypatch):
    admin = await users.create_user(session, email="mon-admin@example.com",
                                    password_hash="x", is_admin=True, is_approved=True)
    await session.commit()
    monkeypatch.setattr(mcp_notify, "async_session_maker", _FakeMaker(session))
    monkeypatch.setattr(mcp_notify, "get_access_token", lambda: _Token(admin.id))
    assert await mcp_notify.send_message("ok") == {"sent": True, "parts": 1}


async def test_tool_refuses_a_non_admin_token(session, bot, monkeypatch):
    """The destination chat belongs to the deployment, not to an account — so an
    ordinary approved user must not be able to write into the owner's channel."""
    user = await users.create_user(session, email="mon-user@example.com",
                                   password_hash="x", is_admin=False, is_approved=True)
    await session.commit()
    monkeypatch.setattr(mcp_notify, "async_session_maker", _FakeMaker(session))
    monkeypatch.setattr(mcp_notify, "get_access_token", lambda: _Token(user.id))
    with pytest.raises(PermissionError):
        await mcp_notify.send_message("не мало б дійти")
    assert bot.sent == []


async def test_tool_rate_limits(bot, monkeypatch):
    monkeypatch.setattr(mcp_notify, "get_access_token", lambda: None)
    monkeypatch.setattr(mcp_notify, "_limiter", RateLimiter(1, 3600))
    await mcp_notify.send_message("перше")
    with pytest.raises(ValueError, match="Rate limit"):
        await mcp_notify.send_message("друге")
    assert len(bot.sent) == 1


async def test_tool_turns_a_delivery_failure_into_a_client_error(monkeypatch):
    monkeypatch.setattr(mcp_notify, "get_access_token", lambda: None)
    monkeypatch.setattr(notify, "_get_bot", lambda: None)
    monkeypatch.setattr(settings, "TELEGRAM_MONITOR_CHAT_ID", 1)
    with pytest.raises(ValueError, match="TELEGRAM_MONITOR_BOT_TOKEN"):
        await mcp_notify.send_message("щось")


# --- separation from the coach server -----------------------------------------------


def test_the_two_servers_require_disjoint_scopes():
    """The security property the split exists for: a token minted for one endpoint is
    refused at the other, because neither carries the scope the other requires."""
    from app.mcp_http import auth_kwargs
    from app.mcp_oauth import NOTIFY_SCOPE, SCOPE

    coach = auth_kwargs("https://mcp.example.com", SCOPE)["auth"].required_scopes
    monitor = auth_kwargs("https://mon.example.com", NOTIFY_SCOPE)["auth"].required_scopes
    assert coach == [SCOPE] and monitor == [NOTIFY_SCOPE]
    assert not set(coach) & set(monitor)


def test_notify_server_exposes_only_the_send_tool():
    assert [fn.__name__ for fn in mcp_notify._TOOLS] == ["send_message"]


def test_notify_server_refuses_to_start_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_MONITOR_BOT_TOKEN", None)
    with pytest.raises(SystemExit, match="TELEGRAM_MONITOR_BOT_TOKEN"):
        mcp_notify.main([])


def test_notify_http_refuses_to_start_without_a_public_url(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_MONITOR_BOT_TOKEN", "t")
    monkeypatch.setattr(settings, "TELEGRAM_MONITOR_CHAT_ID", 1)
    monkeypatch.setattr(settings, "MCP_NOTIFY_PUBLIC_URL", None)
    with pytest.raises(SystemExit, match="MCP_NOTIFY_PUBLIC_URL"):
        mcp_notify.main(["--transport", "http"])
