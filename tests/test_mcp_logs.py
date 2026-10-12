"""The ops-log tools on the coach MCP server: read-only access to the deployment's own
logs, admin only.

What is ours to test: that a record keeps its traceback, that the filters mean what the
tool description says, that secrets don't leave the Pi, that only whitelisted units reach
an argv, and that a non-admin token is refused on every call.

``mcp`` is an opt-in extra, so the module skips itself when it isn't installed.
"""
import datetime as dt
import subprocess

import anyio
import pytest

pytest.importorskip("mcp")

from app import mcp_logs  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db import users  # noqa: E402

LOG = """\
2026-10-12 06:59:58 | INFO    | bot.jobs | MORNING tick
2026-10-12 07:00:01 | INFO    | bot.jobs | MORNING start user_id=3
2026-10-12 07:00:02 | WARNING | garmin.client | 429, retrying in 2s
2026-10-12 07:00:05 | ERROR   | bot.jobs | MORNING failed user_id=3
Traceback (most recent call last):
  File "bot/jobs.py", line 10, in morning_job
ValueError: boom
2026-10-12 07:00:06 | INFO    | claude | CLAUDE OK sonnet ~$0.02
2026-10-12 08:00:00 | INFO    | api | GET /plan → 200 42ms
"""


@pytest.fixture
def logfile(tmp_path, monkeypatch):
    path = tmp_path / "bot.log"
    (tmp_path / "bot.log.1").write_text(
        "2026-10-11 23:00:00 | INFO    | bot.jobs | yesterday\n", encoding="utf-8"
    )
    path.write_text(LOG, encoding="utf-8")
    monkeypatch.setattr(settings, "LOG_FILE", str(path))
    monkeypatch.setattr(mcp_logs, "get_access_token", lambda: None)
    return path


# --- pure ---------------------------------------------------------------------------


def test_traceback_stays_attached_to_its_record():
    recs = mcp_logs.parse_records(LOG.splitlines(True))
    assert len(recs) == 6
    err = recs[3]
    assert err.level == "ERROR" and err.name == "bot.jobs"
    assert err.text.endswith("ValueError: boom")


def test_rotated_files_are_read_oldest_first(logfile):
    names = [p.name for p in mcp_logs.log_files()]
    assert names == ["bot.log.1", "bot.log"]


def test_parse_when_relative_and_iso():
    now = dt.datetime(2026, 10, 12, 8, 0)
    assert mcp_logs.parse_when("2h", now=now) == dt.datetime(2026, 10, 12, 6, 0)
    assert mcp_logs.parse_when("30m ago", now=now) == dt.datetime(2026, 10, 12, 7, 30)
    assert mcp_logs.parse_when("2026-10-12T07:00") == dt.datetime(2026, 10, 12, 7, 0)
    assert mcp_logs.parse_when("") is None
    with pytest.raises(ValueError, match="ISO"):
        mcp_logs.parse_when("yesterday-ish")


def test_filters_level_logger_and_context():
    recs = mcp_logs.parse_records(LOG.splitlines(True))
    warn = mcp_logs.filter_records(recs, level="WARNING")
    assert [r.level for r in warn] == ["WARNING", "ERROR"]
    # A logger prefix matches the logger and its children, not a name that merely starts
    # with the same letters.
    assert mcp_logs.filter_records(recs, logger_prefix="bo") == []
    assert len(mcp_logs.filter_records(recs, logger_prefix="bot")) == 3
    assert len(mcp_logs.filter_records(recs, logger_prefix="bot.jobs")) == 3
    around = mcp_logs.filter_records(recs, level="ERROR", context=1)
    assert [r.level for r in around] == ["WARNING", "ERROR", "INFO"]


def test_redacts_secrets():
    # Built at runtime: a literal token-shaped string trips GitHub's secret scanner.
    bot_token = "1" * 9 + ":" + "Xy" * 18
    anthropic_key = "sk-" + "ant-" + "fake" * 3
    line = (f"token {bot_token} key {anthropic_key} "
            "Authorization: Bearer abc.def.ghijkl password=hunter22")
    out = mcp_logs.redact(line)
    for secret in ("XyXy", anthropic_key, "abc.def.ghijkl", "hunter22"):
        assert secret not in out


def test_fit_keeps_the_newest():
    text, cut = mcp_logs._fit(["a" * 10, "b" * 10, "c" * 10], 25)
    assert cut and text == "b" * 10 + "\n" + "c" * 10


# --- read_app_log -------------------------------------------------------------------


async def test_read_app_log_filters_and_reports(logfile):
    got = await mcp_logs.read_app_log(since="2026-10-12T07:00", grep="morning")
    assert got["matched"] == 2 and not got["truncated"]
    assert "ValueError: boom" in got["text"]
    assert got["files_read"] == ["bot.log.1", "bot.log"]
    assert got["oldest_record"] == "2026-10-11 23:00:00"


async def test_read_app_log_returns_the_newest_within_limit(logfile):
    got = await mcp_logs.read_app_log(since="", limit=2)
    assert got["returned"] == 2 and got["truncated"]
    assert got["text"].endswith("GET /plan → 200 42ms")


async def test_read_app_log_rejects_bad_arguments(logfile):
    with pytest.raises(ValueError, match="level"):
        await mcp_logs.read_app_log(level="LOUD")
    with pytest.raises(ValueError, match="regex"):
        await mcp_logs.read_app_log(grep="(")


# --- read_journal / service_status --------------------------------------------------


def _fake_run(stdout="", stderr="", rc=0, seen=None):
    def run(argv):
        if seen is not None:
            seen.append(argv)
        return subprocess.CompletedProcess(argv, rc, stdout, stderr)
    return run


async def test_journal_builds_a_safe_argv(monkeypatch):
    monkeypatch.setattr(mcp_logs, "get_access_token", lambda: None)
    seen = []
    monkeypatch.setattr(mcp_logs, "_run", _fake_run("l1\nl2 boom\n", seen=seen))
    got = await mcp_logs.read_journal("garmin-web.service", since="2026-10-12T07:00",
                                      priority="err", lines=50)
    argv = seen[0]
    assert argv[0] == "journalctl" and "--unit=garmin-web.service" in argv
    assert "--since=2026-10-12 07:00:00" in argv and "--priority=err" in argv
    assert "--lines=50" in argv
    assert got["returned"] == 2


async def test_journal_grep_filters_after_the_fetch(monkeypatch):
    monkeypatch.setattr(mcp_logs, "get_access_token", lambda: None)
    monkeypatch.setattr(mcp_logs, "_run", _fake_run("a\nb boom\nc\n"))
    got = await mcp_logs.read_journal("garmin-bot", grep="BOOM")
    assert got["text"] == "b boom"


async def test_journal_refuses_a_unit_off_the_whitelist(monkeypatch):
    monkeypatch.setattr(mcp_logs, "get_access_token", lambda: None)
    monkeypatch.setattr(mcp_logs, "_run", _fake_run())
    for unit in ("sshd", "garmin-bot; rm -rf /", "--all"):
        with pytest.raises(ValueError, match="Unknown unit"):
            await mcp_logs.read_journal(unit)


async def test_journal_permission_problem_names_the_fix(monkeypatch):
    monkeypatch.setattr(mcp_logs, "get_access_token", lambda: None)
    monkeypatch.setattr(mcp_logs, "_run",
                        _fake_run(stderr="Failed: Permission denied", rc=1))
    with pytest.raises(ValueError, match="systemd-journal"):
        await mcp_logs.read_journal("garmin-bot")


async def test_service_status_parses_systemctl_show(monkeypatch):
    monkeypatch.setattr(mcp_logs, "get_access_token", lambda: None)
    monkeypatch.setattr(mcp_logs, "_run", _fake_run(
        "ActiveState=active\nSubState=running\nNRestarts=2\nMemoryCurrent=104857600\n"))
    got = await mcp_logs.service_status("garmin-bot")
    bot = got["services"]["garmin-bot"]
    assert bot["ActiveState"] == "active" and bot["NRestarts"] == "2"
    assert bot["MemoryCurrent"] == "100.0 MiB"


# --- who may call it ----------------------------------------------------------------


class _Token:
    def __init__(self, subject):
        self.subject = str(subject)


class _FakeMaker:
    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def test_tools_refuse_a_non_admin_token(session, logfile, monkeypatch):
    user = await users.create_user(session, email="logs-user@example.com",
                                   password_hash="x", is_admin=False, is_approved=True)
    await session.commit()
    monkeypatch.setattr(mcp_logs, "async_session_maker", _FakeMaker(session))
    monkeypatch.setattr(mcp_logs, "get_access_token", lambda: _Token(user.id))
    monkeypatch.setattr(mcp_logs, "_run", _fake_run())
    for call in (mcp_logs.read_app_log(), mcp_logs.read_journal(),
                 mcp_logs.service_status()):
        with pytest.raises(PermissionError):
            await call


async def test_tools_allow_an_admin_token(session, logfile, monkeypatch):
    admin = await users.create_user(session, email="logs-admin@example.com",
                                    password_hash="x", is_admin=True, is_approved=True)
    await session.commit()
    monkeypatch.setattr(mcp_logs, "async_session_maker", _FakeMaker(session))
    monkeypatch.setattr(mcp_logs, "get_access_token", lambda: _Token(admin.id))
    assert (await mcp_logs.read_app_log(since=""))["matched"] == 7


# --- they live on the existing coach server, not a server of their own ---------------


def test_coach_server_carries_the_log_tools():
    """No new process/origin/connector: the read-only coach endpoint the admin already
    connected serves the log tools, gated per call by the admin check above."""
    from app.mcp_server import build_server

    names = {t.name for t in anyio.run(build_server().list_tools)}
    assert {"read_app_log", "read_journal", "service_status"} <= names
    assert "query_activities" in names


def test_there_is_no_separate_logs_server_or_scope():
    import app.mcp_oauth as oauth_mod

    assert not hasattr(mcp_logs, "main") and not hasattr(mcp_logs, "build_server")
    assert not hasattr(oauth_mod, "LOGS_SCOPE")
