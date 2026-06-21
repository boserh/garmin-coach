"""Scheduled morning auto-report.

Every CHECK_INTERVAL_MIN within the Europe/Warsaw window the job checks whether
today's Garmin data has synced; it fires once when it has (or after the deadline
with a stale-data note). The once-a-day guard now lives in the DB (BotState),
replacing the old in-memory flag + state.json.
"""
import datetime as dt
import logging

from telegram.ext import ContextTypes

from app.analysis.service import AnalystError, run_analysis
from app.db.base import async_session_maker
from app.garmin import repository, service
from bot.handlers import ALLOWED_CHAT_ID, TZ

logger = logging.getLogger("bot")

MORNING_START_HOUR = 7
MORNING_DEADLINE_HOUR = 12
CHECK_INTERVAL_MIN = 20
MORNING_STATE_KEY = "morning_sent_date"

_MORNING_Q = "Короткий ранковий звіт: відновлення, готовність на сьогодні, найближча пробіжка."
_MORNING_STALE = "⚠️ Дані за сьогодні ще не синканулись, звіт за останній доступний день.\n\n"


async def morning_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        now = dt.datetime.now(TZ)
        today = now.date().isoformat()

        if not (MORNING_START_HOUR <= now.hour <= MORNING_DEADLINE_HOUR):
            logger.debug(f"MORNING skip: outside window (hour={now.hour})")
            return

        async with async_session_maker() as session:
            if await repository.get_state(session, MORNING_STATE_KEY) == today:
                logger.debug("MORNING skip: already sent today")
                return

            payload = await service.build_payload_cached(session, days=3, activity_limit=20)

            if not payload.synced_today:
                if now.hour < MORNING_DEADLINE_HOUR:
                    # no data yet and not past the deadline — wait for the next check
                    logger.info(
                        f"MORNING skip: not synced yet, waiting "
                        f"(last_data={payload.last_data_date}, deadline={MORNING_DEADLINE_HOUR}:00)"
                    )
                    return
                logger.info("MORNING: deadline reached without sync — sending with stale-data note")
                note = _MORNING_STALE
            else:
                logger.info("MORNING: today synced — sending report")
                note = ""

            try:
                text = await run_analysis(
                    session, payload, question=_MORNING_Q, kind="morning"
                )
            except AnalystError as e:
                logger.error(f"ANALYST {e}")
                text = str(e)

            await ctx.bot.send_message(ALLOWED_CHAT_ID, "Доброго ранку.\n\n" + note + text)
            await repository.set_state(session, MORNING_STATE_KEY, today)
            logger.info(f"MORNING sent for {today}")

    except Exception:
        logger.exception("MORNING job failed")
