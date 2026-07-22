"""OPS-03 remote deploy: the pure subprocess wrappers (app.deploy) and the bot's
admin-only /deploy command + confirm-button flow (bot.handlers)."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app import deploy
from app.db.models import User

U1 = 1


# --- app.deploy: pure subprocess wrappers -------------------------------------

class _FakeProc:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self._stdout = stdout

    async def communicate(self):
        return self._stdout, b""


async def test_git_pull_ok(monkeypatch):
    async def fake_exec(*args, **kwargs):
        assert args[:2] == ("git", "pull")
        return _FakeProc(0, b"Already up to date.")
    monkeypatch.setattr(deploy.asyncio, "create_subprocess_exec", fake_exec)

    result = await deploy.git_pull()
    assert result.ok is True
    assert "Already up to date" in result.output


async def test_git_pull_failure(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _FakeProc(1, b"fatal: not a git repository")
    monkeypatch.setattr(deploy.asyncio, "create_subprocess_exec", fake_exec)

    result = await deploy.git_pull()
    assert result.ok is False
    assert "fatal" in result.output


async def test_restart_services_uses_sudo_and_fixed_script(monkeypatch):
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["args"] = args
        return _FakeProc(0, b"")
    monkeypatch.setattr(deploy.asyncio, "create_subprocess_exec", fake_exec)

    result = await deploy.restart_services()
    assert result.ok is True
    assert seen["args"] == ("sudo", str(deploy.RESTART_SCRIPT))


# --- bot handlers: /deploy + deploy:yes/no callback ---------------------------

class _FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


class _FakeQuery:
    def __init__(self, data, chat_id):
        self.data = data
        self.message = _FakeMessage()
        self.message.chat = SimpleNamespace(id=chat_id)
        self.edited = None

    async def answer(self):
        pass

    async def edit_message_text(self, text, reply_markup=None):
        self.edited = (text, reply_markup)


async def _mk_user(session, chat_id=555, is_admin=False):
    u = User(email=f"{chat_id}@e.com", password_hash="h", is_approved=True,
              is_active=True, telegram_chat_id=chat_id, is_admin=is_admin)
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


@pytest.fixture
def _single_session(session, monkeypatch):
    from bot import handlers as h

    @asynccontextmanager
    async def maker():
        yield session

    monkeypatch.setattr(h, "async_session_maker", maker)
    return session


async def test_deploy_command_denies_non_admin(_single_session, monkeypatch):
    from bot import handlers as h

    monkeypatch.setattr(h.settings, "DEPLOY_ENABLED", True)
    user = await _mk_user(_single_session, is_admin=False)
    msg = _FakeMessage()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=user.telegram_chat_id), message=msg)

    await h.deploy(update, SimpleNamespace(args=[]))

    assert "адмін" in msg.replies[-1][0].lower()


async def test_deploy_command_disabled_by_setting(_single_session, monkeypatch):
    from bot import handlers as h

    monkeypatch.setattr(h.settings, "DEPLOY_ENABLED", False)
    user = await _mk_user(_single_session, is_admin=True)
    msg = _FakeMessage()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=user.telegram_chat_id), message=msg)

    await h.deploy(update, SimpleNamespace(args=[]))

    assert "вимкнен" in msg.replies[-1][0].lower()


async def test_deploy_command_admin_gets_confirm_buttons(_single_session, monkeypatch):
    from bot import handlers as h

    monkeypatch.setattr(h.settings, "DEPLOY_ENABLED", True)
    user = await _mk_user(_single_session, is_admin=True)
    msg = _FakeMessage()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=user.telegram_chat_id), message=msg)

    await h.deploy(update, SimpleNamespace(args=[]))

    text, kb = msg.replies[-1]
    assert "Задеплоїти" in text
    assert kb is not None


async def test_deploy_callback_no_cancels(_single_session):
    from bot import handlers as h

    user = await _mk_user(_single_session, is_admin=True)
    q = _FakeQuery("deploy:no", user.telegram_chat_id)

    await h.deploy_callback(SimpleNamespace(callback_query=q), None)

    assert "Скасовано" in q.edited[0]


async def test_deploy_callback_yes_denies_non_admin(_single_session):
    from bot import handlers as h

    user = await _mk_user(_single_session, is_admin=False)
    q = _FakeQuery("deploy:yes", user.telegram_chat_id)

    await h.deploy_callback(SimpleNamespace(callback_query=q), None)

    assert "адмін" in q.edited[0].lower()


async def test_deploy_callback_yes_runs_pull_then_restart(_single_session, monkeypatch):
    from bot import handlers as h

    user = await _mk_user(_single_session, is_admin=True)
    q = _FakeQuery("deploy:yes", user.telegram_chat_id)

    calls = []

    async def fake_pull():
        calls.append("pull")
        return deploy.CommandResult(ok=True, output="Updating abc..def")

    async def fake_restart():
        calls.append("restart")
        return deploy.CommandResult(ok=True, output="")

    monkeypatch.setattr(h.deploy_ops, "git_pull", fake_pull)
    monkeypatch.setattr(h.deploy_ops, "restart_services", fake_restart)

    await h.deploy_callback(SimpleNamespace(callback_query=q), None)

    assert calls == ["pull", "restart"]
    assert q.edited[0] == "⏳ git pull…"
    joined = "\n".join(r[0] for r in q.message.replies)
    assert "Updating abc..def" in joined
    assert "Перезапускаю" in joined


async def test_deploy_callback_yes_stops_on_pull_failure(_single_session, monkeypatch):
    from bot import handlers as h

    user = await _mk_user(_single_session, is_admin=True)
    q = _FakeQuery("deploy:yes", user.telegram_chat_id)

    calls = []

    async def fake_pull():
        calls.append("pull")
        return deploy.CommandResult(ok=False, output="fatal: conflict")

    async def fake_restart():
        calls.append("restart")
        return deploy.CommandResult(ok=True, output="")

    monkeypatch.setattr(h.deploy_ops, "git_pull", fake_pull)
    monkeypatch.setattr(h.deploy_ops, "restart_services", fake_restart)

    await h.deploy_callback(SimpleNamespace(callback_query=q), None)

    assert calls == ["pull"]                # restart never runs after a failed pull
    assert "провалився" in q.message.replies[-1][0]


async def test_deploy_callback_restart_failure_with_empty_output_shows_code(
    _single_session, monkeypatch,
):
    """A denied sudo call can come back with an empty pipe (the rejection lands in the
    syslog auth log, not this process' stdout/stderr) — the reply must still say
    something diagnostic (the return code), never just the bare header."""
    from bot import handlers as h

    user = await _mk_user(_single_session, is_admin=True)
    q = _FakeQuery("deploy:yes", user.telegram_chat_id)

    async def fake_pull():
        return deploy.CommandResult(ok=True, output="Already up to date.")

    async def fake_restart():
        return deploy.CommandResult(ok=False, output="", returncode=1)

    monkeypatch.setattr(h.deploy_ops, "git_pull", fake_pull)
    monkeypatch.setattr(h.deploy_ops, "restart_services", fake_restart)

    await h.deploy_callback(SimpleNamespace(callback_query=q), None)

    last = q.message.replies[-1][0]
    assert "не вдався" in last
    assert "код 1" in last
    assert last.strip().endswith(":") is False   # never a bare, content-less header
