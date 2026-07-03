"""Scheduled per-user tick: Garmin fetch, activity auto-analysis, morning report.

Every CHECK_INTERVAL_MIN within the wider Europe/Warsaw window (07:00-23:00) the job
loops over every registered user with a Telegram chat id and Garmin credentials,
fetching their data once via ``build_payload_cached``. That single fetch feeds two
concerns from the same tick (no duplicate Garmin calls):
* activity watch — any freshly synced running activity (within ACTIVITY_FRESH_DAYS)
  gets auto-analyzed and DMed, all day;
* the morning report — sent once, only within the narrower 07-12 window, guarded by
  BotState so a re-run of the job doesn't resend it.
"""
import datetime as dt
import logging

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from telegram.ext import ContextTypes

from app import weather
from app.analysis.service import AnalystError, run_activity_analysis, run_analysis
from app.db.base import async_session_maker
from app.db.models import User
from app.garmin import matching, plan_sync, repository, service
from app.garmin.runtime import user_runtime
from bot.handlers import TZ

logger = logging.getLogger("bot")

MORNING_START_HOUR = 7
MORNING_DEADLINE_HOUR = 12
# Activity watch runs across a wider daily window than the morning report itself, so
# an evening run still gets an automatic recap the same day.
ACTIVITY_WATCH_END_HOUR = 23
# Only auto-analyze activities synced in the last N days — never a GDPR-export
# backfill (which doesn't go through build_payload_cached anyway) and never a huge
# first-ever fetch for a long-idle user.
ACTIVITY_FRESH_DAYS = 2
CHECK_INTERVAL_MIN = 20
MORNING_STATE_KEY = "morning_sent_date"

# A separate, once-a-day calendar sync (push upcoming plan workouts to Garmin, remove
# stale ones). Kept out of the morning report — different concern. Scheduled via
# run_daily at a fixed hour (Europe/Warsaw), before the morning window.
PLAN_SYNC_HOUR = 5

_MORNING_Q = "Короткий ранковий звіт: відновлення, готовність на сьогодні, найближча пробіжка."
_MORNING_STALE = "⚠️ Дані за сьогодні ще не синканулись, звіт за останній доступний день.\n\n"


def _recovery_synced(payload, today: str) -> bool:
    """True only when today's recovery data is actually in — both HRV and sleep.
    Garmin can sync stress earlier than HRV/sleep, so ``payload.synced_today`` (any
    field) is too loose for the morning report; we wait for the recovery essentials."""
    row = next((d for d in payload.daily if d.date == today), None)
    return bool(row and row.hrv_avg is not None and row.sleep_score is not None)


async def _fetch_user_weather(user: User):
    """Today's forecast for the user's stored location, or None if unset/on error."""
    if user.latitude is None or user.longitude is None:
        return None
    wx = await run_in_threadpool(weather.fetch_forecast, user.latitude, user.longitude)
    if wx:
        logger.info(f"MORNING user={user.id}: weather {wx.get('summary')} "
                    f"{wx.get('t_min_c')}–{wx.get('t_max_c')}°C")
    return wx


async def _deliver_morning(ctx, session, user: User, creds, payload, now: dt.datetime,
                           today: str, *, force: bool = False) -> bool:
    """Run the morning analysis on an already-fetched ``payload`` and send it. Returns
    True if a report was sent. With ``force`` the not-yet-synced wait is skipped (send
    with the stale note) — used by the on-demand /test_morning trigger. Does NOT touch
    the once-a-day guard; the caller owns that."""
    if not _recovery_synced(payload, today):
        if not force and now.hour < MORNING_DEADLINE_HOUR:
            logger.info(f"MORNING skip user={user.id}: recovery data not synced yet "
                        f"(last_data={payload.last_data_date})")
            return False
        logger.info(f"MORNING user={user.id}: sending with stale note "
                    f"({'forced' if force else 'deadline reached'})")
        note = _MORNING_STALE
    else:
        logger.info(f"MORNING user={user.id}: today synced — sending")
        note = ""

    wx = await _fetch_user_weather(user)
    try:
        text = await run_analysis(
            session, payload, user_id=user.id, question=_MORNING_Q,
            kind="morning", api_key=creds.anthropic_key, weather=wx,
        )
    except AnalystError as e:
        logger.error(f"ANALYST {e}")
        text = str(e)

    prefix = "🧪 [тест] " if force else ""
    await ctx.bot.send_message(user.telegram_chat_id, prefix + "Доброго ранку.\n\n" + note + text)
    return True


async def force_morning_for_user(ctx, session, user: User) -> None:
    """Send the morning report on demand, bypassing the time window + once-a-day guard
    (and leaving the guard untouched, so the real morning still fires). For /test_morning."""
    now = dt.datetime.now(TZ)
    today = now.date().isoformat()
    async with user_runtime(session, user) as creds:
        if not creds.has_garmin:
            await ctx.bot.send_message(user.telegram_chat_id, "🧪 Немає Garmin-кредів.")
            return
        payload, _ = await service.build_payload_cached(
            session, user.id, days=3, activity_limit=20
        )
        await _deliver_morning(ctx, session, user, creds, payload, now, today, force=True)


def _activity_head(act) -> str:
    parts = [act.type or "активність"]
    if act.dist_km:
        parts.append(f"{act.dist_km:.1f} км")
    return f"🏃 Нова активність: {' · '.join(parts)} ({act.date})"


async def _activity_watch_for_user(ctx, session, user: User, creds, new_activities) -> None:
    """Auto-analyze freshly synced running activities and DM each result. Best-effort
    per activity — a Claude/Telegram failure here must not break the tick or block
    the remaining activities."""
    if not new_activities:
        return
    cutoff = (dt.date.today() - dt.timedelta(days=ACTIVITY_FRESH_DAYS)).isoformat()
    for act in new_activities:
        if not act.date or act.date < cutoff or "run" not in (act.type or ""):
            continue
        try:
            text = await run_activity_analysis(
                session, act, user_id=user.id, api_key=creds.anthropic_key
            )
            await ctx.bot.send_message(user.telegram_chat_id, f"{_activity_head(act)}\n\n{text}")
            logger.info(f"ACTIVITY_WATCH sent user={user.id} activity={act.id}")
        except Exception:
            logger.exception(f"ACTIVITY_WATCH failed user={user.id} activity={act.id}")


async def _tick_for_user(ctx, session, user: User, now: dt.datetime, today: str) -> None:
    try:
        async with user_runtime(session, user) as creds:
            if not creds.has_garmin:
                logger.debug(f"TICK skip user={user.id}: no Garmin credentials")
                return

            payload, new_activities = await service.build_payload_cached(
                session, user.id, days=3, activity_limit=20
            )
            await _activity_watch_for_user(ctx, session, user, creds, new_activities)

            # Match freshly synced activities to planned workouts — best-effort,
            # same pattern as _sync_for_user (a failure here must not block the tick).
            try:
                result = await matching.match_activities(session, user.id)
                if any(result.values()):
                    logger.info(f"MATCH user={user.id}: {result}")
            except Exception:
                logger.exception(f"MATCH failed user={user.id}")

            if not (MORNING_START_HOUR <= now.hour <= MORNING_DEADLINE_HOUR):
                return
            if await repository.get_state(session, user.id, MORNING_STATE_KEY) == today:
                logger.debug(f"MORNING skip user={user.id}: already sent today")
                return
            if await _deliver_morning(ctx, session, user, creds, payload, now, today):
                await repository.set_state(session, user.id, MORNING_STATE_KEY, today)
                logger.info(f"MORNING sent for {today} user={user.id}")
    except Exception:
        logger.exception(f"TICK failed for user={user.id}")


async def _sync_for_user(session, user: User) -> None:
    """Reconcile one user's Garmin calendar with their plan. Binds the user's provider;
    the cleanup pass runs even with no active plan (to remove a just-archived plan's
    pushed workouts). Skips users with sync disabled, nothing to do, or no creds."""
    try:
        if not user.garmin_sync_enabled:
            return
        plan = await repository.get_active_plan(session, user.id)
        pushed = await repository.list_pushed_workouts(session, user.id)
        if plan is None and not pushed:
            return
        async with user_runtime(session, user) as creds:
            if not creds.has_garmin:
                return
            await plan_sync.sync_plan_to_garmin(session, user.id)
    except Exception:
        logger.exception(f"PLAN sync failed user={user.id}")


async def plan_sync_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Once-a-day per-user Garmin calendar sync (separate from the morning report);
    scheduled by run_daily, so no further time guard is needed."""
    try:
        async with async_session_maker() as session:
            recipients = (
                await session.execute(
                    select(User).where(
                        User.is_active.is_(True), User.is_approved.is_(True)
                    )
                )
            ).scalars().all()
            for user in recipients:
                await _sync_for_user(session, user)
    except Exception:
        logger.exception("PLAN sync job failed")


async def morning_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Per-tick entry point: fetch once per user (07:00-23:00), run activity watch,
    and — within the narrower 07-12 window — the morning report. See module docstring."""
    try:
        now = dt.datetime.now(TZ)
        today = now.date().isoformat()

        if not (MORNING_START_HOUR <= now.hour <= ACTIVITY_WATCH_END_HOUR):
            logger.debug(f"TICK skip: outside window (hour={now.hour})")
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
                await _tick_for_user(ctx, session, user, now, today)

    except Exception:
        logger.exception("MORNING job failed")
