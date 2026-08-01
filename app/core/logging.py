"""Single logging configuration for the whole project (web + bot).

Moved from the old flat ``logging_setup.py``; now reads paths/levels from Settings.
Call :func:`setup` once at process start, before any module-level loggers are used.
"""
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

from app.core.alerts import TelegramAlertHandler
from app.core.config import settings

_crash_logger = logging.getLogger("crash")


def _log_uncaught(exc_type, exc_value, exc_tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    _crash_logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _log_uncaught_thread(args: "threading.ExceptHookArgs") -> None:
    _crash_logger.critical(
        f"Uncaught exception in thread {args.thread.name!r}",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def setup() -> None:
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")

    # rotating file (so it doesn't bloat): 5 files of 1 MB each
    fh = RotatingFileHandler(
        settings.LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(formatter)

    # and to console
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)

    level = settings.LOG_LEVEL.upper()
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    root.handlers.clear()
    root.addHandler(fh)
    root.addHandler(ch)

    # mirror WARNING+ from every process to the admin bot's owner chat (best-effort,
    # deduped — see app.core.alerts). Off when no admin bot token is configured.
    if settings.TELEGRAM_ADMIN_BOT_TOKEN:
        root.addHandler(TelegramAlertHandler())

    # rescue: log (and thus forward) exceptions that would otherwise escape unseen —
    # a crash in the main thread or in a bare `threading.Thread` target. Asyncio task
    # exceptions already reach here via asyncio's own default handler logging through
    # the "asyncio" logger, which propagates to root like everything else.
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_uncaught_thread

    # silence noisy libraries
    for noisy in ("httpx", "httpcore", "telegram", "apscheduler", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # DB_ECHO=true → log every SQL statement (reads + writes) through our handlers;
    # otherwise keep SQLAlchemy quiet. (Level-based, so no duplicate echo handler.)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DB_ECHO else logging.WARNING
    )
