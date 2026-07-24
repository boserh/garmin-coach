"""Admin/system Telegram bot entrypoint — a SECOND bot, its own process.

Run with::

    ./venv/bin/python -m bot.admin_main

Carries only the hidden system/admin commands — ``/deploy`` (OPS-03: git pull +
service restart) and the ``/test_*`` debug commands — under a separate bot identity.
This keeps deploy + debug traffic off the main coaching bot (``bot.main``) and lets
deploy restart the whole stack without the main product bot being the thing triggering
it. Shares the exact same handlers (``bot.handlers``), codebase and DB — different
token, different process (``garmin-admin-bot.service``).

Access is locked to the **first user** (lowest ``users.id`` — the bootstrap admin):
every update is gated by ``_owner_only`` before any handler runs, so even another
registered admin can't drive deploy/test from here. The bot token comes from
``TELEGRAM_ADMIN_BOT_TOKEN`` (``.env``); unset → this process refuses to start.

The scheduled jobs (morning report, digest, plan sync, …) stay on ``bot.main`` — this
process runs no JobQueue work of its own beyond the on-demand ``/test_on`` tick.
"""
import logging

from sqlalchemy import select
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    Defaults,
    TypeHandler,
)

from app.core import logging as app_logging
from app.core.config import settings
from app.db.base import async_session_maker, init_db
from app.db.models import User
from bot import handlers

app_logging.setup()
logger = logging.getLogger("bot")

_DENIED_MSG = "⛔ Цей бот лише для власника."


async def _first_user_id_and_chat() -> "tuple[int | None, int | None]":
    """The bootstrap owner: lowest users.id + its telegram_chat_id."""
    async with async_session_maker() as session:
        first = (
            await session.execute(select(User).order_by(User.id.asc()).limit(1))
        ).scalar_one_or_none()
    if first is None:
        return None, None
    return first.id, first.telegram_chat_id


async def _owner_only(update: Update, ctx) -> None:
    """Group -1 gate: block every update whose chat isn't the first user's.

    Raising ApplicationHandlerStop prevents the actual command/callback (group 0) from
    running. An authorised update falls through untouched.
    """
    chat = update.effective_chat
    _, owner_chat = await _first_user_id_and_chat()
    if chat is not None and owner_chat is not None and chat.id == owner_chat:
        return  # authorised — let the real handler run
    logger.warning(
        "ADMIN BOT denied chat=%s (owner_chat=%s)",
        getattr(chat, "id", None),
        owner_chat,
    )
    if update.callback_query is not None:
        await update.callback_query.answer(_DENIED_MSG, show_alert=True)
    elif update.effective_message is not None:
        await update.effective_message.reply_text(_DENIED_MSG)
    raise ApplicationHandlerStop


def register_admin_handlers(app: Application) -> None:
    """Wire the owner gate + the hidden system/admin commands (/deploy + /test_*)."""
    # Runs before every handler below; refuses anyone but the first user.
    app.add_handler(TypeHandler(Update, _owner_only), group=-1)

    app.add_handler(CommandHandler("test_on", handlers.test_on))
    app.add_handler(CommandHandler("test_off", handlers.test_off))
    app.add_handler(CommandHandler("test_morning", handlers.test_morning))
    app.add_handler(CommandHandler("test_digest", handlers.test_digest))
    # OPS-03: admin-only remote deploy (git pull + restart). Re-checks user.is_admin
    # inside the handler + callback (defense in depth); DEPLOY_ENABLED still gates it.
    app.add_handler(CommandHandler("deploy", handlers.deploy))
    app.add_handler(CallbackQueryHandler(handlers.deploy_callback, pattern=r"^deploy:"))
    app.add_error_handler(handlers.on_error)


async def _post_init(application: Application) -> None:
    # Tables exist already (bot.main/web create them), but keep it zero-config.
    await init_db()
    # This is a private admin bot, so — unlike bot.main, which hides these — surface the
    # commands in its own "/" menu for convenience.
    await application.bot.set_my_commands([
        BotCommand("deploy", "git pull + рестарт сервісів (admin)"),
        BotCommand("test_morning", "Форс ранкового звіту зараз"),
        BotCommand("test_digest", "Форс тижневого підсумку зараз"),
        BotCommand("test_on", "Тестова джоба: ранковий звіт кожні N хв"),
        BotCommand("test_off", "Вимкнути тестову джобу"),
    ])


def main() -> None:
    if not settings.TELEGRAM_ADMIN_BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_ADMIN_BOT_TOKEN is not set — add it to .env before running "
            "bot.admin_main (see CLAUDE.md)."
        )
    defaults = Defaults(tzinfo=handlers.TZ)
    app = (
        Application.builder()
        .token(settings.TELEGRAM_ADMIN_BOT_TOKEN)
        .defaults(defaults)
        .post_init(_post_init)
        .build()
    )

    register_admin_handlers(app)

    logger.info("Admin bot started")
    print("Адмін-бот запущено. Ctrl+C для зупинки.")
    app.run_polling()


if __name__ == "__main__":
    main()
