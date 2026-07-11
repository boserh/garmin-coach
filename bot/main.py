"""Telegram bot entrypoint.

Run with::

    ./venv/bin/python -m bot.main

Shares the core (app.garmin / app.analysis) with the web layer — no duplicated
logic. Ensures DB tables exist on startup so BotState works out of the box.
"""
import logging
from datetime import time

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, Defaults

from app.core import logging as app_logging
from app.core.config import settings
from app.db.base import init_db
from bot import handlers
from bot.jobs import (
    CHECK_INTERVAL_MIN,
    PLAN_SYNC_HOUR,
    morning_job,
    plan_adapt_job,
    plan_sync_job,
    weather_plan_job,
    weekly_digest_job,
)

app_logging.setup()
logger = logging.getLogger("bot")


async def _post_init(application: Application) -> None:
    # create tables for a zero-config first run (Alembic stays the source of truth)
    await init_db()
    # populate the Telegram "/" command menu (user-facing commands only; the
    # test_* debug commands stay hidden)
    await application.bot.set_my_commands([
        BotCommand("report", "Звіт відновлення за 7 днів"),
        BotCommand("ask", "Питання за останніми звітами, напр. /ask чи бігти завтра"),
        BotCommand("deep", "Глибокий аналіз (Opus), напр. /deep вплив вело на HRV"),
        BotCommand("activities", "Останні активності"),
        BotCommand("activity", "Розбір активності, напр. /activity 5"),
        BotCommand("checkin", "Оцінити останнє тренування (RPE + чи боліло)"),
        BotCommand("plan", "Програма; /plan <текст> щоб змінити, напр. додай біг сьогодні"),
    ])


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
    app.add_handler(CommandHandler("ask", handlers.ask))
    app.add_handler(CommandHandler("deep", handlers.deep))
    app.add_handler(CommandHandler("activities", handlers.activities))
    app.add_handler(CommandHandler("activity", handlers.activity))
    app.add_handler(CommandHandler("checkin", handlers.checkin))
    app.add_handler(CommandHandler("records", handlers.records_cmd))
    app.add_handler(CommandHandler("compare", handlers.compare))
    app.add_handler(CommandHandler("risk", handlers.risk))
    app.add_handler(CommandHandler("plan", handlers.plan))
    app.add_handler(CallbackQueryHandler(handlers.plan_callback, pattern=r"^plan_"))
    app.add_handler(CallbackQueryHandler(handlers.adapt_callback, pattern=r"^adapt_"))
    app.add_handler(CallbackQueryHandler(handlers.checkin_callback, pattern=r"^ci:"))
    app.add_handler(CommandHandler("test_on", handlers.test_on))
    app.add_handler(CommandHandler("test_off", handlers.test_off))
    app.add_handler(CommandHandler("test_morning", handlers.test_morning))
    app.add_handler(CommandHandler("test_digest", handlers.test_digest))
    app.add_error_handler(handlers.on_error)

    # First check runs shortly after startup, then every CHECK_INTERVAL_MIN.
    # morning_job enforces the time window and the once-a-day guard itself,
    # so an early/out-of-window run just returns without doing anything.
    app.job_queue.run_repeating(
        morning_job,
        interval=CHECK_INTERVAL_MIN * 60,
        first=10,
    )
    # Separate once-a-day Garmin calendar sync (push upcoming plan workouts / remove
    # stale ones) at a fixed hour, before the morning window.
    app.job_queue.run_daily(
        plan_sync_job,
        time=time(hour=PLAN_SYNC_HOUR, tzinfo=handlers.TZ),
    )
    # EP-02 adaptive plan: weekly review. days follow PTB's 0=Sunday..6=Saturday.
    app.job_queue.run_daily(
        plan_adapt_job,
        time=time(hour=settings.PLAN_ADAPT_HOUR, tzinfo=handlers.TZ),
        days=(settings.PLAN_ADAPT_WEEKLY_DOW,),
    )
    # EP-07 weekly digest: Sunday-evening retrospective (before the adaptation review).
    app.job_queue.run_daily(
        weekly_digest_job,
        time=time(hour=settings.DIGEST_HOUR, tzinfo=handlers.TZ),
        days=(settings.DIGEST_WEEKLY_DOW,),
    )
    # EP-13 weather-aware planning: daily check that proposes moving a key session off an
    # extreme-weather day. Runs every morning (silent when there's no conflict), before
    # the morning report window.
    app.job_queue.run_daily(
        weather_plan_job,
        time=time(hour=settings.WEATHER_PLAN_HOUR, tzinfo=handlers.TZ),
    )

    logger.info("Bot started")
    print("Бот запущено. Ctrl+C для зупинки.")
    app.run_polling()


if __name__ == "__main__":
    main()
