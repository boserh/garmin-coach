"""Telegram command handlers + the user lookup and error handler.

Business logic lives in the shared core (app.garmin.service / app.analysis.service);
handlers only orchestrate fetch → analyze → reply, each within a DB session and the
matched user's runtime context (their Garmin provider + Claude key). The bot is one
global identity; a chat is authorised by mapping its chat_id to a registered user.
"""
import logging
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import ContextTypes

from app.analysis.service import AnalystError, run_analysis, run_ask
from app.db import users
from app.db.base import async_session_maker
from app.db.models import User
from app.garmin import service
from app.garmin.runtime import user_runtime

logger = logging.getLogger("bot")

TZ = ZoneInfo("Europe/Warsaw")

_REPORT_Q = "Оціни відновлення і дай пораду до наступної запланованої пробіжки."
_DEEP_Q = "Глибокий розбір сну, HRV і навантаження за два тижні."
_REPORT_STALE = "⚠️ Дані за сьогодні ще не синканулись, аналіз за останній доступний день.\n\n"
_NOT_REGISTERED = (
    "Тебе не зареєстровано. Додай цей chat_id у налаштуваннях веб-кабінету, "
    "щоб бот працював з твоїми даними."
)


async def _resolve_user(update: Update, session) -> "User | None":
    """Map the incoming chat to a registered user, or reply and return None."""
    chat = update.effective_chat
    if chat is None:
        return None
    user = await users.get_by_chat_id(session, chat.id)
    if user is None or not (user.is_active and user.is_approved):
        logger.warning(f"DENIED chat_id={chat.id}")
        if update.message:
            await update.message.reply_text(_NOT_REGISTERED)
        return None
    return user


async def report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    logger.info("CMD /report")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        await update.message.reply_text("Тягну дані з Garmin...")
        async with user_runtime(session, user) as creds:
            payload = await service.build_payload_cached(
                session, user.id, days=7, activity_limit=20
            )
            note = "" if payload.synced_today else _REPORT_STALE
            try:
                text = await run_analysis(
                    session, payload, user_id=user.id, question=_REPORT_Q,
                    kind="report", api_key=creds.anthropic_key,
                )
            except AnalystError as e:
                logger.error(f"ANALYST {e}")
                text = str(e)
    await update.message.reply_text(note + text)


async def deep(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    question = " ".join(ctx.args) or _DEEP_Q
    logger.info(f"CMD /deep {question[:60]}")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        await update.message.reply_text("Думаю глибше...")
        async with user_runtime(session, user) as creds:
            payload = await service.build_payload_cached(
                session, user.id, days=14, activity_limit=30
            )
            try:
                text = await run_analysis(
                    session, payload, user_id=user.id, question=question,
                    deep=True, kind="deep", api_key=creds.anthropic_key,
                )
            except AnalystError as e:
                logger.error(f"ANALYST {e}")
                text = str(e)
    await update.message.reply_text(text)


async def ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    question = " ".join(ctx.args).strip()
    if not question:
        await update.message.reply_text(
            "Напиши питання після команди, напр.:\n/ask чи варто завтра бігти інтервали?"
        )
        return
    logger.info(f"CMD /ask {question[:60]}")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        await update.message.reply_text("Дивлюсь у твої останні звіти...")
        async with user_runtime(session, user) as creds:
            try:
                text = await run_ask(
                    session, question, user_id=user.id, api_key=creds.anthropic_key
                )
            except AnalystError as e:
                logger.error(f"ANALYST {e}")
                text = str(e)
    await update.message.reply_text(text)


# ---------- TEST JOB ----------

async def test_job(ctx: ContextTypes.DEFAULT_TYPE):
    user_id = ctx.job.data["user_id"]
    chat_id = ctx.job.data["chat_id"]
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user is None:
            return
        async with user_runtime(session, user) as creds:
            payload = await service.build_payload_cached(
                session, user.id, days=7, activity_limit=20
            )
            try:
                text = await run_analysis(
                    session, payload, user_id=user.id, kind="report",
                    api_key=creds.anthropic_key,
                )
            except AnalystError as e:
                text = str(e)
    await ctx.bot.send_message(chat_id, "🧪 [тест]\n\n" + text)


async def test_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        data = {"user_id": user.id, "chat_id": update.effective_chat.id}
    for j in ctx.job_queue.get_jobs_by_name("test"):
        j.schedule_removal()
    minutes = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 2
    ctx.job_queue.run_repeating(test_job, interval=minutes * 60, first=5, name="test", data=data)
    logger.info(f"CMD /test_on {minutes}")
    await update.message.reply_text(f"🧪 Тестова джоба: кожні {minutes} хв (перша через 5 сек).")


async def test_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    removed = 0
    for j in ctx.job_queue.get_jobs_by_name("test"):
        j.schedule_removal()
        removed += 1
    logger.info(f"CMD /test_off removed={removed}")
    await update.message.reply_text(f"🧪 Тестову джобу вимкнено (знято {removed}).")


# ---------- ERROR HANDLER ----------

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning(f"TG network: {type(err).__name__}: {err}")
    else:
        logger.exception("Unhandled bot error", exc_info=err)
