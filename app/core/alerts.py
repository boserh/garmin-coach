"""Mirrors WARNING+ log records to the admin bot's owner chat (Telegram).

Attached to the root logger by ``app.core.logging.setup`` whenever
``TELEGRAM_ADMIN_BOT_TOKEN`` is set, so it works from every process (bot, admin bot,
web) without each one wiring its own alerting. Best-effort and fire-and-forget: a
broken alert path must never block or crash the thing it's alerting about, so network
calls run on a throwaway daemon thread and every failure is swallowed silently.
"""
import json
import logging
import threading
import time
import urllib.request
from urllib.error import URLError

from app.core.config import settings

_OWNER_CHAT_TTL_S = 60
_DEDUP_TTL_S = 300
_MAX_LEN = 3900  # Telegram's sendMessage cap is 4096; leave room for the prefix
_MAX_DEDUP_ENTRIES = 500

_owner_chat_state = {"chat_id": None, "resolved_at": 0.0}
_recent: dict[str, float] = {}
_lock = threading.Lock()


def _fetch_owner_chat_id() -> "int | None":
    """Blocking DB fetch, always run on a fresh thread with its own event loop.

    ``emit()`` runs synchronously inside whichever thread logged the record — in the
    bot/web processes that's almost always a thread with an asyncio loop already
    running, and ``asyncio.run()`` raises immediately (without awaiting its coroutine)
    when called there. That exception used to be swallowed by the bare ``except
    Exception: pass`` below, silently breaking every alert — hence a brand new thread
    here instead, where starting a fresh loop is always safe."""
    import asyncio

    from sqlalchemy import select

    from app.db.base import async_session_maker
    from app.db.models import User

    async def _fetch() -> "int | None":
        async with async_session_maker() as session:
            user = (
                await session.execute(select(User).order_by(User.id.asc()).limit(1))
            ).scalar_one_or_none()
            return user.telegram_chat_id if user else None

    result: dict = {}

    def _run() -> None:
        try:
            result["chat_id"] = asyncio.run(_fetch())
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=5)
    return result.get("chat_id")


def _resolve_owner_chat_id() -> "int | None":
    """Same owner as bot.admin_main: lowest users.id's telegram_chat_id, cached."""
    now = time.monotonic()
    if now - _owner_chat_state["resolved_at"] < _OWNER_CHAT_TTL_S:
        return _owner_chat_state["chat_id"]
    chat_id = _owner_chat_state["chat_id"]  # keep the stale value on a transient failure
    try:
        chat_id = _fetch_owner_chat_id()
    except Exception:
        pass
    _owner_chat_state["chat_id"] = chat_id
    _owner_chat_state["resolved_at"] = now
    return chat_id


def _send(token: str, chat_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text[:_MAX_LEN]}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=5).close()
    except (URLError, OSError):
        pass  # best-effort — never re-raise into logging


def _exc_summary(record: logging.LogRecord) -> str:
    """``KeyError: 'plan_id'`` for a record logged with exc_info, else ''."""
    exc = record.exc_info[1] if record.exc_info else None
    if exc is None:
        return ""
    try:
        detail = str(exc)
    except Exception:
        detail = ""
    name = type(exc).__name__
    return f"{name}: {detail}"[:500] if detail else name


class TelegramAlertHandler(logging.Handler):
    """Root-logger handler: forwards WARNING+ records to the admin bot owner chat.

    Dedupes identical (logger, level, message) within ``_DEDUP_TTL_S`` so a repeating
    warning (e.g. a Garmin retry loop) can't turn into a message flood.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        # Recursion guard: never forward this module's own logging.
        if record.name == __name__:
            return
        try:
            text = record.getMessage()
        except Exception:
            return
        # ``logger.exception("TICK failed for user=1")`` carries the cause only in exc_info,
        # and the page used to drop it — the owner got "failed" with no clue what failed and
        # had to go dig in the Pi's log. The exception's own one-liner rides along instead
        # (not the traceback: that stays in bot.log / job_runs). It is part of the dedup key
        # too, so two different failures behind the same message both get through.
        summary = _exc_summary(record)
        if summary:
            text = f"{text}\n{summary}"
        key = f"{record.name}:{record.levelno}:{text}"
        now = time.monotonic()
        with _lock:
            last = _recent.get(key)
            if last is not None and now - last < _DEDUP_TTL_S:
                return
            if len(_recent) > _MAX_DEDUP_ENTRIES:
                _recent.clear()
            _recent[key] = now
        token = settings.TELEGRAM_ADMIN_BOT_TOKEN
        if not token:
            return
        chat_id = _resolve_owner_chat_id()
        if not chat_id:
            return
        prefix = "🛑" if record.levelno >= logging.ERROR else "⚠️"
        threading.Thread(
            target=_send,
            args=(token, chat_id, f"{prefix} [{record.name}] {text}"),
            daemon=True,
        ).start()
