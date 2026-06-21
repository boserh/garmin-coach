"""Telegram command handlers + the owner guard and error handler.

Business logic lives in the shared core (app.garmin.service / app.analysis.service);
handlers only orchestrate fetch → analyze → reply, each within a DB session.
"""
import logging
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import ContextTypes

from app.analysis.service import AnalystError, run_analysis
from app.core.config import settings
from app.db.base import async_session_maker
from app.garmin import service

logger = logging.getLogger("bot")

TZ = ZoneInfo("Europe/Warsaw")
ALLOWED_CHAT_ID = settings.TELEGRAM_CHAT_ID

_REPORT_Q = "Оціни відновлення і дай пораду до наступної запланованої пробіжки."
_DEEP_Q = "Глибокий розбір сну, HRV і навантаження за два тижні."
_REPORT_STALE = "⚠️ Дані за сьогодні ще не синканулись, аналіз за останній доступний день.\n\n"


def _guard(update: Update) -> bool:
    ok = update.effective_chat and update.effective_chat.id == ALLOWED_CHAT_ID
    if not ok and update.effective_chat:
        logger.warning(f"DENIED chat_id={update.effective_chat.id}")
    return ok


async def report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update):
        return
    logger.info("CMD /report")
    await update.message.reply_text("Тягну дані з Garmin...")
    async with async_session_maker() as session:
        payload = await service.build_payload_cached(session, days=7, activity_limit=20)
        note = "" if payload.synced_today else _REPORT_STALE
        try:
            text = await run_analysis(session, payload, question=_REPORT_Q, kind="report")
        except AnalystError as e:
            logger.error(f"ANALYST {e}")
            text = str(e)
    await update.message.reply_text(note + text)


async def deep(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update):
        return
    question = " ".join(ctx.args) or _DEEP_Q
    logger.info(f"CMD /deep {question[:60]}")
    await update.message.reply_text("Думаю глибше...")
    async with async_session_maker() as session:
        payload = await service.build_payload_cached(session, days=14, activity_limit=30)
        try:
            text = await run_analysis(
                session, payload, question=question, deep=True, kind="deep"
            )
        except AnalystError as e:
            logger.error(f"ANALYST {e}")
            text = str(e)
    await update.message.reply_text(text)


# ---------- TEST JOB ----------

async def test_job(ctx: ContextTypes.DEFAULT_TYPE):
    async with async_session_maker() as session:
        payload = await service.build_payload_cached(session, days=7, activity_limit=20)
        try:
            text = await run_analysis(session, payload, kind="report")
        except AnalystError as e:
            text = str(e)
    await ctx.bot.send_message(ALLOWED_CHAT_ID, "🧪 [тест]\n\n" + text)


async def test_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update):
        return
    for j in ctx.job_queue.get_jobs_by_name("test"):
        j.schedule_removal()
    minutes = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 2
    ctx.job_queue.run_repeating(test_job, interval=minutes * 60, first=5, name="test")
    logger.info(f"CMD /test_on {minutes}")
    await update.message.reply_text(f"🧪 Тестова джоба: кожні {minutes} хв (перша через 5 сек).")


async def test_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update):
        return
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
