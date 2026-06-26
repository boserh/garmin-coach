"""Single logging configuration for the whole project (web + bot).

Moved from the old flat ``logging_setup.py``; now reads paths/levels from Settings.
Call :func:`setup` once at process start, before any module-level loggers are used.
"""
import logging
from logging.handlers import RotatingFileHandler

from app.core.config import settings


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

    # silence noisy libraries
    for noisy in ("httpx", "httpcore", "telegram", "apscheduler", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # DB_ECHO=true → log every SQL statement (reads + writes) through our handlers;
    # otherwise keep SQLAlchemy quiet. (Level-based, so no duplicate echo handler.)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DB_ECHO else logging.WARNING
    )
