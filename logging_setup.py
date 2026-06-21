"""logging_setup.py — single logging configuration for the whole project."""
import logging
import os
from logging.handlers import RotatingFileHandler

LOG_FILE = os.environ.get("LOG_FILE", "bot.log")


def setup():
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")

    # rotating file (so it doesn't bloat): 5 files of 1 MB each
    fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(formatter)

    # and to console
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)

    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    root.handlers.clear()
    root.addHandler(fh)
    root.addHandler(ch)

    # silence noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
