"""Telegram bot entrypoint.

Run with::

    ./venv/bin/python -m bot.main

Shares the core (app.garmin / app.analysis) with the web layer — no duplicated
logic. Ensures DB tables exist on startup so BotState works out of the box.
"""
import logging

from telegram.ext import Application, CommandHandler, Defaults

from app.core import logging as app_logging
from app.core.config import settings
from app.db.base import init_db
from bot import handlers
from bot.jobs import CHECK_INTERVAL_MIN, morning_job

app_logging.setup()
logger = logging.getLogger("bot")


async def _post_init(application: Application) -> None:
    # create tables for a zero-config first run (Alembic stays the source of truth)
    await init_db()


def main() -> None:
    defaults = Defaults(tzinfo=handlers.TZ)
    app = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .defaults(defaults)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("report", handlers.report))
    app.add_handler(CommandHandler("deep", handlers.deep))
    app.add_handler(CommandHandler("test_on", handlers.test_on))
    app.add_handler(CommandHandler("test_off", handlers.test_off))
    app.add_error_handler(handlers.on_error)

    # First check runs shortly after startup, then every CHECK_INTERVAL_MIN.
    # morning_job enforces the time window and the once-a-day guard itself,
    # so an early/out-of-window run just returns without doing anything.
    app.job_queue.run_repeating(
        morning_job,
        interval=CHECK_INTERVAL_MIN * 60,
        first=10,
    )

    logger.info("Bot started")
    print("Бот запущено. Ctrl+C для зупинки.")
    app.run_polling()


if __name__ == "__main__":
    main()
