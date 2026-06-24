"""Scheduled morning auto-report, per user.

Every CHECK_INTERVAL_MIN within the Europe/Warsaw window the job loops over every
registered user with a Telegram chat id and Garmin credentials, sending each their
report once today's data has synced (or after the deadline with a stale-data note).
The once-a-day guard lives in the DB (BotState), keyed per user.
"""
import datetime as dt
import logging

from sqlalchemy import select
from telegram.ext import ContextTypes

from app.analysis.service import AnalystError, run_analysis
from app.db.base import async_session_maker
from app.db.models import User
from app.garmin import repository, service
from app.garmin.runtime import user_runtime
from bot.handlers import TZ

logger = logging.getLogger("bot")

MORNING_START_HOUR = 7
MORNING_DEADLINE_HOUR = 12
CHECK_INTERVAL_MIN = 20
MORNING_STATE_KEY = "morning_sent_date"

_MORNING_Q = "Короткий ранковий звіт: відновлення, готовність на сьогодні, найближча пробіжка."
_MORNING_STALE = "⚠️ Дані за сьогодні ще не синканулись, звіт за останній доступний день.\n\n"


def _recovery_synced(payload, today: str) -> bool:
    """True only when today's recovery data is actually in — both HRV and sleep.
    Garmin can sync stress earlier than HRV/sleep, so ``payload.synced_today`` (any
    field) is too loose for the morning report; we wait for the recovery essentials."""
    row = next((d for d in payload.daily if d.date == today), None)
    return bool(row and row.hrv_avg is not None and row.sleep_score is not None)


async def _morning_for_user(ctx, session, user: User, now: dt.datetime, today: str) -> None:
    try:
        if await repository.get_state(session, user.id, MORNING_STATE_KEY) == today:
            logger.debug(f"MORNING skip user={user.id}: already sent today")
            return

        async with user_runtime(session, user) as creds:
            if not creds.has_garmin:
                logger.debug(f"MORNING skip user={user.id}: no Garmin credentials")
                return

            payload = await service.build_payload_cached(
                session, user.id, days=3, activity_limit=20
            )

            if not _recovery_synced(payload, today):
                if now.hour < MORNING_DEADLINE_HOUR:
                    logger.info(
                        f"MORNING skip user={user.id}: recovery data not synced yet "
                        f"(last_data={payload.last_data_date})"
                    )
                    return
                logger.info(f"MORNING user={user.id}: deadline reached — sending with stale note")
                note = _MORNING_STALE
            else:
                logger.info(f"MORNING user={user.id}: today synced — sending")
                note = ""

            try:
                text = await run_analysis(
                    session, payload, user_id=user.id, question=_MORNING_Q,
                    kind="morning", api_key=creds.anthropic_key,
                )
            except AnalystError as e:
                logger.error(f"ANALYST {e}")
                text = str(e)

            await ctx.bot.send_message(user.telegram_chat_id, "Доброго ранку.\n\n" + note + text)
            await repository.set_state(session, user.id, MORNING_STATE_KEY, today)
            logger.info(f"MORNING sent for {today} user={user.id}")
    except Exception:
        logger.exception(f"MORNING failed for user={user.id}")


async def morning_job(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        now = dt.datetime.now(TZ)
        today = now.date().isoformat()

        if not (MORNING_START_HOUR <= now.hour <= MORNING_DEADLINE_HOUR):
            logger.debug(f"MORNING skip: outside window (hour={now.hour})")
            return

        async with async_session_maker() as session:
            recipients = (
                await session.execute(
                    select(User).where(
                        User.telegram_chat_id.is_not(None),
                        User.is_active.is_(True),
                        User.is_approved.is_(True),
                    )
                )
            ).scalars().all()
            for user in recipients:
                await _morning_for_user(ctx, session, user, now, today)

    except Exception:
        logger.exception("MORNING job failed")
