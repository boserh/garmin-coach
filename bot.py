"""
bot.py — Telegram-бот для аналізу Garmin через Claude.

Команди:
  /report   — звіт за 7 днів + порада до наступної пробіжки
  /deep ... — глибокий аналіз (Opus), напр.: /deep як вело впливає на HRV

Ранковий автозвіт:
  Кожні 20 хв у вікні 7:00–12:00 бот перевіряє, чи синканулись дані за сьогодні.
  Щойно дані з'явились — надсилає звіт ОДИН раз. Якщо до 12:00 даних нема —
  надсилає з приміткою (за останній доступний день).

Відповідає лише власнику (TELEGRAM_CHAT_ID).
"""

import os
import datetime as dt
from dotenv import load_dotenv

load_dotenv()  # підвантажує .env у змінні оточення

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import garmin_client
import claude_analyst

ALLOWED_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])

# межі ранкового вікна перевірки
MORNING_START_HOUR = 5
MORNING_DEADLINE_HOUR = 10
CHECK_INTERVAL_MIN = 10

# щоб не слати ранковий звіт двічі за день
_sent_today = {"date": None}


def _guard(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.id == ALLOWED_CHAT_ID


def _analyze(payload: dict, question: str = "", deep: bool = False) -> str:
    try:
        return claude_analyst.analyze(payload, question=question, deep=deep)
    except claude_analyst.AnalystError as e:
        return str(e)


# ---------- КОМАНДИ ----------

async def report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update):
        return
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
    await update.message.reply_text("Думаю глибше...")
    payload = garmin_client.build_payload(days=14, activity_limit=30)
    text = _analyze(payload, question=question, deep=True)
    await update.message.reply_text(text)


# ---------- РАНКОВИЙ АВТОЗВІТ ----------

async def morning_job(ctx: ContextTypes.DEFAULT_TYPE):
    today = dt.date.today().isoformat()
    if _sent_today["date"] == today:
        return  # вже відправили сьогодні

    now_hour = dt.datetime.now().hour
    payload = garmin_client.build_payload(days=3, activity_limit=20)

    if not payload.get("synced_today"):
        # дані ще не синканулись
        if now_hour < MORNING_DEADLINE_HOUR:
            return  # ще рано, чекаємо наступної перевірки
        note = "⚠️ Дані за сьогодні ще не синканулись, звіт за останній доступний день.\n\n"
    else:
        note = ""

    text = _analyze(
        payload,
        question="Короткий ранковий звіт: відновлення, готовність на сьогодні, найближча пробіжка."
    )
    await ctx.bot.send_message(ALLOWED_CHAT_ID, "Доброго ранку.\n\n" + note + text)
    _sent_today["date"] = today


async def test_job(ctx: ContextTypes.DEFAULT_TYPE):
    payload = garmin_client.build_payload(days=7, activity_limit=20)
    text = _analyze(payload, question="Щоденний статус. Детальну пораду до пробіжки — лише якщо вона сьогодні/завтра.")
    await ctx.bot.send_message(ALLOWED_CHAT_ID, "🧪 [тест]\n\n" + text)

async def test_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update):
        return
    # знімаємо попередні тестові джоби, якщо були
    for j in ctx.job_queue.get_jobs_by_name("test"):
        j.schedule_removal()
    minutes = 2
    if ctx.args and ctx.args[0].isdigit():
        minutes = int(ctx.args[0])
    ctx.job_queue.run_repeating(test_job, interval=minutes * 60, first=5, name="test")
    await update.message.reply_text(f"🧪 Тестова джоба увімкнена: кожні {minutes} хв (перша через 5 сек).")


async def test_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _guard(update):
        return
    removed = 0
    for j in ctx.job_queue.get_jobs_by_name("test"):
        j.schedule_removal()
        removed += 1
    await update.message.reply_text(f"🧪 Тестову джобу вимкнено (знято {removed}).")


# ---------- ЗАПУСК ----------

def main():
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CommandHandler("deep", deep))
    app.add_handler(CommandHandler("test_on", test_on))
    app.add_handler(CommandHandler("test_off", test_off))

    # перевіряти кожні CHECK_INTERVAL_MIN хв, починаючи з ранку;
    # morning_job сам вирішує, чи вже час слати
    app.job_queue.run_repeating(
        morning_job,
        interval=CHECK_INTERVAL_MIN * 60,
        first=dt.time(hour=MORNING_START_HOUR, minute=0),
    )

    print("Бот запущено. Ctrl+C для зупинки.")
    app.run_polling()


if __name__ == "__main__":
    main()
