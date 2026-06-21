"""
bot.py — Telegram bot that analyzes Garmin data via Claude.

Commands:
  /report    — 7-day report + status/advice
  /deep ...  — deep analysis (Opus), e.g.: /deep how does cycling affect HRV
  /test_on N — test job every N min (for debugging), /test_off — disable

Morning auto-report:
  Every 20 min within the window from MORNING_START_HOUR the bot checks whether
  data has synced. As soon as today's data is available it sends the report once.
  If there's still none by the deadline, it sends with a note (using the last
  available day).

Responds only to the owner (TELEGRAM_CHAT_ID).
"""

import os
import json
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

# morning sync-check window
MORNING_START_HOUR = 7
MORNING_DEADLINE_HOUR = 12
CHECK_INTERVAL_MIN = 20

# Persist whether the morning report was already sent today, so a restart
# mid-morning doesn't fire it again.
STATE_FILE = os.environ.get("STATE_FILE", "state.json")


def _load_sent_date():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get("morning_sent_date")
    except (FileNotFoundError, json.JSONDecodeError):
        return None  # missing or empty/corrupt — start fresh
    except Exception as e:
        logger.warning(f"STATE load failed: {e}")
        return None


def _save_sent_date(date: str) -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"morning_sent_date": date}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.warning(f"STATE save failed: {e}")


# avoid sending the morning report twice a day (survives restarts via STATE_FILE)
_sent_today = {"date": _load_sent_date()}


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


# ---------- COMMANDS ----------

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


# ---------- MORNING AUTO-REPORT ----------

async def morning_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        now = dt.datetime.now(TZ)
        today = now.date().isoformat()

        if not (MORNING_START_HOUR <= now.hour <= MORNING_DEADLINE_HOUR):
            logger.debug(f"MORNING skip: outside window (hour={now.hour})")
            return

        if _sent_today["date"] == today:
            logger.debug("MORNING skip: already sent today")
            return

        payload = garmin_client.build_payload(days=3, activity_limit=20)

        if not payload.get("synced_today"):
            if now.hour < MORNING_DEADLINE_HOUR:
                # no data yet and not past the deadline — wait for the next check
                logger.info(
                    f"MORNING skip: not synced yet, waiting "
                    f"(last_data={payload.get('last_data_date')}, deadline={MORNING_DEADLINE_HOUR}:00)"
                )
                return
            logger.info("MORNING: deadline reached without sync — sending with stale-data note")
            note = "⚠️ Дані за сьогодні ще не синканулись, звіт за останній доступний день.\n\n"
        else:
            logger.info("MORNING: today synced — sending report")
            note = ""

        text = _analyze(
                payload,
                question="Короткий ранковий звіт: відновлення, готовність на сьогодні, найближча пробіжка."
        )
        await ctx.bot.send_message(ALLOWED_CHAT_ID, "Доброго ранку.\n\n" + note + text)
        _sent_today["date"] = today
        _save_sent_date(today)
        logger.info(f"MORNING sent for {today}")

    except Exception:
        logger.exception("MORNING job failed")


# ---------- TEST JOB ----------

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


# ---------- ENTRYPOINT ----------

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
