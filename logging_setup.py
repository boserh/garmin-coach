"""logging_setup.py — єдине налаштування логів для всього проєкту."""
import logging
import os
from logging.handlers import RotatingFileHandler

LOG_FILE = os.environ.get("LOG_FILE", "bot.log")


def setup():
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")

    # у файл з ротацією (щоб не розпухав): 5 файлів по 1 МБ
    fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(formatter)

    # і в консоль
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(fh)
    root.addHandler(ch)

    # приглушити шумні бібліотеки
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
