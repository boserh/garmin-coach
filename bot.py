"""
bot.py — Telegram-бот для аналізу Garmin через Claude.

Команди:
  /report    — звіт за 7 днів + статус/порада
  /deep ...  — глибокий аналіз (Opus), напр.: /deep як вело впливає на HRV
  /test_on N — тестова джоба кожні N хв (для відладки), /test_off — вимкнути

Ранковий автозвіт:
  Кожні 20 хв у вікні від MORNING_START_HOUR бот перевіряє, чи синканулись дані.
  Щойно дані за сьогодні є — надсилає звіт один раз. Якщо до дедлайну немає —
  надсилає з приміткою (за останній доступний день).

Відповідає лише власнику (TELEGRAM_CHAT_ID).
"""

import os
import datetime as dt
from dotenv import load_dotenv

load_dotenv()

import logging_setup
logging_setup.setup()
import logging
logger = logging.getLogger("bot")

from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, Defaults

import garmin_client
import claude_analyst

ALLOWED_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
TZ = ZoneInfo("Europe/Warsaw")

# ранкове вікно перевірки синку
MORNING_START_HOUR = 7
MORNING_DEADLINE_HOUR = 12
CHECK_INTERVAL_MIN = 20

# щоб не слати ранковий звіт двічі за день
_sent_today = {"date": None}


def _guard(update: Update) -> bool:
    ok = update.effective_chat and update.effective_chat.id == ALLOWED_CHAT_ID
    if not ok and update.effective_chat:
        logger.warning(f"DENIED chat_id={update.effective_chat.id}")
    return ok


def _analyze(payload: dict, question: str = "", deep: bool = False) -> str:
    try:
        return claude_analyst.analyze(payload, question=question, deep=deep)
    except claude_analyst.AnalystError as e:
        logger.error(f"ANALYST {e}")
        return str(e)


# ---------- КОМАНДИ ----------

async def report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update):
        return
    logger.info("CMD /report")
    await update.message.reply_text("Тягну дані з Garmin...")
    payload = garmin_client.build_payload(days=7, activity_limit=20)
    note = ""
    if not payload.get("synced_today"):
        note = "⚠️ Дані за сьогодні ще не синканулись, аналіз за останній доступний день.\n\n"
    text = _analyze(payload, question="Оціни відновлення і дай пораду до наступної запланованої пробіжки.")
    await update.message.reply_text(note + text)


async def deep(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update):
        return
    question = " ".join(ctx.args) or "Глибокий розбір сну, HRV і навантаження за два тижні."
    logger.info(f"CMD /deep {question[:60]}")
    await update.message.reply_text("Думаю глибше...")
    payload = garmin_client.build_payload(days=14, activity_limit=30)
    text = _analyze(payload, question=question, deep=True)
    await update.message.reply_text(text)


# ---------- РАНКОВИЙ АВТОЗВІТ ----------

async def morning_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        now = dt.datetime.now(TZ)
        today = now.date().isoformat()

        if not (MORNING_START_HOUR <= now.hour <= MORNING_DEADLINE_HOUR):
            return

        if _sent_today["date"] == today:
            return

        payload = garmin_client.build_payload(days=3, activity_limit=20)

        if not payload.get("synced_today"):
            if now.hour < MORNING_DEADLINE_HOUR:
                return  # даних ще нема і ще не дедлайн — чекаємо наступної перевірки
            note = "⚠️ Дані за сьогодні ще не синканулись, звіт за останній доступний день.\n\n"
        else:
            note = ""

        text = _analyze(
                payload,
                question="Короткий ранковий звіт: відновлення, готовність на сьогодні, найближча пробіжка."
        )
        await ctx.bot.send_message(ALLOWED_CHAT_ID, "Доброго ранку.\n\n" + note + text)
        _sent_today["date"] = today
        logger.info(f"MORNING sent for {today}")

    except Exception:
        logger.exception("MORNING job failed")


# ---------- ТЕСТОВА ДЖОБА ----------

async def test_job(ctx: ContextTypes.DEFAULT_TYPE):
    payload = garmin_client.build_payload(days=7, activity_limit=20)
    text = _analyze(payload)
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


# ---------- ЗАПУСК ----------

def main():
    defaults = Defaults(tzinfo=TZ)
    app = (Application.builder()
           .token(os.environ["TELEGRAM_BOT_TOKEN"])
           .defaults(defaults)
           .build())

    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("deep", deep))
    app.add_handler(CommandHandler("test_on", test_on))
    app.add_handler(CommandHandler("test_off", test_off))

    app.job_queue.run_repeating(
        morning_job,
        interval=CHECK_INTERVAL_MIN * 60,
        first=dt.time(hour=MORNING_START_HOUR, minute=0, tzinfo=TZ),
    )

    logger.info("Бот запущено")
    print("Бот запущено. Ctrl+C для зупинки.")
    app.run_polling()


if __name__ == "__main__":
    main()
