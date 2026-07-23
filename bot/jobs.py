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
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.concurrency import run_in_threadpool
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from app import baselines, gear, race, records, sleepnudge, stepmatch, weather
from app.analysis import delivery
from app.analysis.plans import ADAPT_WINDOW_DAYS_DEFAULT, OPEN_ENDED_GOAL
from app.analysis.service import (
    AnalystError,
    build_health_alerts,
    build_injury_assessment,
    run_activity_analysis,
    run_compare,
    run_digest,
    run_health_alert,
    run_injury_check,
    run_insights,
    run_plan_adaptation,
    run_race_plan,
    run_weather_plan_check,
)
from app.core.config import settings
from app.db.base import async_session_maker
from app.db.models import User
from app.db.users import eligible_users
from app.garmin import matching, plan_sync, repository, service
from app.garmin.client import GarminRateLimited
from app.garmin.credentials import load_credentials
from app.garmin.mfa import MFARequired
from app.garmin.runtime import user_runtime
from bot.handlers import (
    CHECKIN_PROMPT,
    GARMIN_RATE_LIMITED_MSG,
    MFA_REQUIRED_MSG,
    PENDING_ADAPT_KEY,
    PLAN_EXTEND_SNOOZE_KEY,
    TZ,
    checkin_keyboard,
)

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
CHECK_INTERVAL_MIN = 5
MORNING_STATE_KEY = "morning_sent_date"
MFA_NOTIFIED_PREFIX = "mfa_notified:"
RATE_LIMIT_NOTIFIED_PREFIX = "garmin_ratelimited:"

# A separate, once-a-day calendar sync (push upcoming plan workouts to Garmin, remove
# stale ones). Kept out of the morning report — different concern. Scheduled via
# run_daily at a fixed hour (Europe/Warsaw), before the morning window.
PLAN_SYNC_HOUR = 5

# EP-02 adaptive plan: weekly review hour/day come from settings (PLAN_ADAPT_HOUR /
# PLAN_ADAPT_WEEKLY_DOW). The morning nudge fires only for these session types, and at
# most once/day per user (guarded via bot_state, key below + today's date).
ADAPT_HEAVY_TYPES = {"tempo", "intervals", "long"}
ADAPT_GUARD_PREFIX = "adapt_suggested:"

# Open-ended plan extend nudge: once-a-day guard keyed by date (bot_state extend_nudge:<date>),
# so the morning "продовжити план?" ✅/❌ prompt is sent at most once per day per user.
EXTEND_NUDGE_PREFIX = "extend_nudge:"

# EP-07 weekly digest: once-a-week guard keyed by ISO week (bot_state key digest:<iso-week>).
DIGEST_GUARD_PREFIX = "digest:"

# NF-06 compare-past-self: a monthly "you vs a year ago" block appended once per calendar
# month, on the first weekly digest of that month (bot_state key compare:<YYYY-MM>).
COMPARE_GUARD_PREFIX = "compare:"
COMPARE_WEEKS = 4   # last 4 weeks vs the same 4 weeks a year ago (matches /compare default)

# NF-02 correlation insights: a monthly "what actually affects you" block, appended once per
# calendar month on the first weekly digest of that month (bot_state key insights:<YYYY-MM>).
INSIGHTS_GUARD_PREFIX = "insights:"

# NF-04 injury radar: bot_state key holding the last date an advisory was sent, so we warn at
# most once per settings.INJURY_GUARD_DAYS (the same signals persist for days — don't nag).
INJURY_WARNED_KEY = "injury_warned"

# EP-08 health alerts: per-rule cooldown in bot_state (key alert:<kind> → last-sent date), so
# the same drifting metric isn't re-flagged daily. Kept per-kind (not one global guard) so a
# new anomaly (e.g. sleep debt) can still fire while an older one (hrv_low) is still cooling.
HEALTH_ALERT_PREFIX = "alert:"

# NF-09 auto-deload: how many days ahead to look for a heavy (tempo/intervals/long) session
# before bothering to turn a risk signal into an adaptation proposal.
DELOAD_HEAVY_WINDOW_DAYS = 5

# ST-11: warn once per threshold as the stored garth token nears its ~1y OAuth1 death date.
# The guard *value* holds the token's issue date, so a fresh re-login (new issue date, after
# a manual reconnect) naturally makes the stored value stop matching and re-arms both
# thresholds — no explicit "clear the guard" step needed.
TOKEN_WARN_THRESHOLDS = (30, 7)   # days-until-expiry, most distant first
TOKEN_WARN_PREFIX = "token_warn:"

_MORNING_Q = "Короткий ранковий звіт: відновлення, готовність на сьогодні, найближча пробіжка."
# Morning keeps its own stale wording ("звіт" not "аналіз", cf. delivery.STALE_NOTE) — a
# deliberate difference: morning decides stale via the stricter _recovery_synced check
# below, not payload.synced_today, so it can't reuse the on-demand note verbatim.
_MORNING_STALE = "⚠️ Дані за сьогодні ще не синканулись, звіт за останній доступний день.\n\n"


def user_tz(user: User) -> ZoneInfo:
    """ST-14: this user's own IANA timezone (validated on save in /settings), falling back
    to the process default (Europe/Warsaw) on a corrupt/missing value so a bad zoneinfo
    string can never break a job. Per-user checks (the morning window, once-a-day/week/
    month bot_state guard dates) read this instead of the hardcoded process TZ; the
    run_daily-scheduled jobs themselves stay on the process TZ in v1 (see CLAUDE.md)."""
    try:
        return ZoneInfo(user.timezone or "Europe/Warsaw")
    except (ZoneInfoNotFoundError, ValueError):
        return TZ


async def for_each_user(worker, *, with_chat: bool, label: str) -> None:
    """Shared scaffold for the per-user scheduled jobs: open a session, select the
    eligible (active + approved [+ chat id]) recipients, and run ``worker(session, user)``
    for each — isolating failures per user so one user's error never aborts the rest.
    The three jobs reduce to a single call; PERF-01 will parallelize only this loop."""
    try:
        async with async_session_maker() as session:
            for user in await eligible_users(session, with_chat=with_chat):
                try:
                    await worker(session, user)
                except Exception:
                    logger.exception(f"{label} failed user={user.id}")
    except Exception:
        logger.exception(f"{label} job failed")


@asynccontextmanager
async def user_garmin_runtime(session, user: User, *, skip_label: Optional[str] = None):
    """Bind the user's Garmin provider and yield decrypted creds only when they actually
    have Garmin credentials; yields ``None`` otherwise (optionally logging a skip line).
    The shared "has creds + runtime" guard for the per-user jobs — callers do
    ``async with user_garmin_runtime(...) as creds: if creds is None: return``."""
    async with user_runtime(session, user) as creds:
        if not creds.has_garmin:
            if skip_label:
                logger.debug(f"{skip_label} skip user={user.id}: no Garmin credentials")
            yield None
        else:
            yield creds


def _recovery_synced(payload, today: str) -> bool:
    """True only when today's recovery data is actually in — both HRV and sleep.
    Garmin can sync stress earlier than HRV/sleep, so ``payload.synced_today`` (any
    field) is too loose for the morning report; we wait for the recovery essentials."""
    row = next((d for d in payload.daily if d.date == today), None)
    return bool(row and row.hrv_avg is not None and row.sleep_score is not None)


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

    wx = await weather.forecast_for_user(user)
    try:
        result = await delivery.build_report(
            session, user, payload, question=_MORNING_Q,
            kind="morning", api_key=creds.anthropic_key, weather=wx,
        )
        text = result.text
    except AnalystError as e:
        logger.error(f"ANALYST {e}")
        text = str(e)

    prefix = "🧪 [тест] " if force else ""
    await ctx.bot.send_message(user.telegram_chat_id, prefix + "Доброго ранку.\n\n" + note + text)
    return True


async def force_morning_for_user(ctx, session, user: User) -> None:
    """Send the morning report on demand, bypassing the time window + once-a-day guard
    (and leaving the guard untouched, so the real morning still fires). For /test_morning."""
    now = dt.datetime.now(user_tz(user))
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


async def _step_match_for_activity(session, user_id: int, act) -> None:
    """NF-14: score a run's actual laps against its planned structured steps, once. Gated
    on the activity being matched (EP-01) to a session WE pushed WITH structure
    (``garmin_workout_id`` set — a manually-run, unpushed workout has no reliable
    lap-to-step correspondence, so it stays silent rather than guessing). Idempotent
    (skips if already scored) and best-effort — a Garmin/parse hiccup never blocks the
    auto-analysis that follows. Mutates ``act.step_match`` in place; caller commits."""
    try:
        if getattr(act, "step_match", None) is not None:
            return
        workout = await repository.get_workout_for_activity(session, user_id, act.id)
        if workout is None or not workout.garmin_workout_id or not workout.steps:
            return
        from app.garmin import client
        laps = await run_in_threadpool(client.fetch_activity_splits, act.activity_id)
        result = stepmatch.match(workout.steps, laps)
        if result is not None:
            act.step_match = result
            logger.info(f"STEPMATCH user={user_id} activity={act.id}: "
                        f"{result['steps_hit']}/{result['steps_total']}")
    except Exception:
        logger.exception(f"STEPMATCH failed user={user_id} activity={act.id}")


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
            # NF-14 first, so the step-level result rides along in the analysis context
            # (activity_payload) instead of arriving a tick later.
            await _step_match_for_activity(session, user.id, act)
            text = await run_activity_analysis(
                session, act, user_id=user.id, api_key=creds.anthropic_key
            )
            badge = stepmatch.badge(getattr(act, "step_match", None))
            head = f"{_activity_head(act)}\n{badge}" if badge else _activity_head(act)
            # Attach the EP-12 post-run check-in (RPE + pain) — one tap, silence is fine.
            await ctx.bot.send_message(
                user.telegram_chat_id,
                f"{head}\n\n{text}\n\n{CHECKIN_PROMPT}",
                reply_markup=checkin_keyboard(act.id),
            )
            logger.info(f"ACTIVITY_WATCH sent user={user.id} activity={act.id}")
        except Exception:
            logger.exception(f"ACTIVITY_WATCH failed user={user.id} activity={act.id}")


async def _records_check_for_user(ctx, session, user: User) -> None:
    """Recompute personal records (EP-14) and DM a 🎉 for any freshly set one. Pure DB work,
    no LLM/network; best-effort so a Telegram hiccup never breaks the tick. Runs after the
    activity watch so the record lands right below the run recap. Commits its own inserts."""
    if not user.telegram_chat_id:
        return
    try:
        new = await records.detect_records(session, user.id)
        if not new:
            return
        fresh = records.announce_worthy(new)
        # Persist first (even the silent backfill rows) so a record never re-announces, then
        # send — a send failure must not re-open the already-recorded PB.
        await session.commit()
        if fresh:
            await ctx.bot.send_message(user.telegram_chat_id, records.celebrate(fresh))
            logger.info(f"RECORDS user={user.id}: {[r.kind for r in fresh]}")
    except Exception:
        logger.exception(f"RECORDS failed user={user.id}")


def _within_guard(last: Optional[str], today: str, days: int) -> bool:
    """True when ``last`` (a stored ISO date) is within ``days`` of ``today`` — i.e. we already
    warned recently and should stay quiet."""
    if not last:
        return False
    try:
        return (dt.date.fromisoformat(today) - dt.date.fromisoformat(last)).days < days
    except ValueError:
        return False


class _RiskCache:
    """Per-tick memo for the two risk detectors (D1). ``_deload``/``_injury``/``_health``
    each need ``build_injury_assessment`` and/or ``build_health_alerts``, and all three can
    run in one 20-min tick — each detector is its own set of ~90-day history reads
    (read_load_history / recent_subjective_runs / read_history / count_daily_metrics). This
    computes each at most once per tick and shares it, WITHOUT changing any guard: the compute
    still happens lazily, the first time a hook actually reaches the point of needing it, so a
    disabled feature or an active per-day guard still costs nothing. A hook called on its own
    (the tests) just gets a fresh single-use cache and behaves exactly as before."""

    def __init__(self, session, user_id: int):
        self._session = session
        self._user_id = user_id
        self._injury = None
        self._health = None

    async def injury(self):
        if self._injury is None:
            self._injury = await build_injury_assessment(self._session, user_id=self._user_id)
        return self._injury

    async def health(self):
        if self._health is None:
            self._health = await build_health_alerts(self._session, user_id=self._user_id)
        return self._health


async def _injury_check_for_user(ctx, session, user: User, creds, today: str,
                                 *, risk: "_RiskCache | None" = None) -> None:
    """Injury-risk radar (NF-04): run the pure detector; if it's an actionable warning and we
    haven't warned in the last INJURY_GUARD_DAYS, narrate + DM one advisory. Best-effort and
    LLM-optional (the detector is zero-LLM; run_injury_check falls back to a deterministic
    text). Silent during calibration or when there's nothing to flag — no false-positive spam."""
    if not settings.INJURY_RADAR or not user.telegram_chat_id or not creds.anthropic_key:
        return
    last = await repository.get_state(session, user.id, INJURY_WARNED_KEY)
    if _within_guard(last, today, settings.INJURY_GUARD_DAYS):
        return
    risk = risk or _RiskCache(session, user.id)
    try:
        assessment = await risk.injury()
        if not assessment.actionable:
            return
        text = await run_injury_check(
            session, user_id=user.id, assessment=assessment, api_key=creds.anthropic_key
        )
        # Set the guard before sending so a send hiccup can't loop into re-warning next tick.
        await repository.set_state(session, user.id, INJURY_WARNED_KEY, today)
        await ctx.bot.send_message(user.telegram_chat_id, text)
        logger.info(f"INJURY user={user.id}: {assessment.level} "
                    f"{[s.kind for s in assessment.signals]}")
    except Exception:
        logger.exception(f"INJURY check failed user={user.id}")


async def _health_check_for_user(ctx, session, user: User, creds, today: str,
                                 *, risk: "_RiskCache | None" = None) -> bool:
    """Proactive health alerts (EP-08): run the pure recovery-anomaly detector; DM one advisory
    for any *newly* actionable alert kind we haven't sent in the last HEALTH_ALERT_COOLDOWN_DAYS.
    Per-rule cooldown (key ``alert:<kind>``) so a persistent drift isn't re-flagged daily, but a
    fresh anomaly still fires. Best-effort and LLM-optional (deterministic ``health.summary``
    fallback). Silent during calibration / when nothing is out of the personal band. Returns True
    if an advisory was sent. Callers pass this only when no injury advisory went out this tick —
    at most one risk DM per morning (the shared 'don't stack risk pings' rule)."""
    if (not settings.HEALTH_ALERTS or not user.alerts_enabled
            or not user.telegram_chat_id or not creds.anthropic_key):
        return False
    risk = risk or _RiskCache(session, user.id)
    try:
        report = await risk.health()
        if not report.actionable:
            return False
        # Fire only for alert kinds not on cooldown; if every kind is still cooling, stay silent.
        fresh = [a for a in report.alerts
                 if not _within_guard(
                     await repository.get_state(session, user.id, HEALTH_ALERT_PREFIX + a.kind),
                     today, settings.HEALTH_ALERT_COOLDOWN_DAYS)]
        if not fresh:
            return False
        text = await run_health_alert(
            session, user_id=user.id, report=report, api_key=creds.anthropic_key
        )
        # Set each fired kind's guard BEFORE sending so a hiccup can't loop into re-warning.
        for a in fresh:
            await repository.set_state(session, user.id, HEALTH_ALERT_PREFIX + a.kind, today)
        await ctx.bot.send_message(user.telegram_chat_id, text)
        logger.info(f"HEALTH user={user.id}: {[a.kind for a in fresh]}")
        return True
    except Exception:
        logger.exception(f"HEALTH check failed user={user.id}")
        return False


async def _deload_check_for_user(ctx, session, user: User, creds, today: str,
                                 *, risk: "_RiskCache | None" = None) -> bool:
    """NF-09: when the injury radar (NF-04) or the health-alert detector (EP-08) already
    has an actionable signal AND a heavy session (tempo/intervals/long) sits within
    DELOAD_HEAVY_WINDOW_DAYS, turn the warning into a concrete ✅/❌ deload proposal via the
    EP-02 adaptation engine (the pre-computed signals ride along as ``risk`` context) instead
    of leaving the user to act on a plain advisory manually. Reuses the same once-per-
    INJURY_GUARD_DAYS guard as the plain injury advisory (INJURY_WARNED_KEY) — a fired
    proposal IS that day's one risk touchpoint, so the caller must skip the plain injury/
    health advisories when this returns True (see _tick_for_user, the EP-13 "don't ping
    twice" pattern extended to a third proposer via _has_pending_proposal). Best-effort:
    any failure here just falls through to the plain advisories."""
    if not user.plan_adapt_enabled or not user.telegram_chat_id or not creds.anthropic_key:
        return False
    last = await repository.get_state(session, user.id, INJURY_WARNED_KEY)
    if _within_guard(last, today, settings.INJURY_GUARD_DAYS):
        return False
    if await _has_pending_proposal(session, user.id):
        return False
    ws = await repository.upcoming_plan_workouts(session, user.id, days=DELOAD_HEAVY_WINDOW_DAYS)
    if not any((w.type or "").lower() in ADAPT_HEAVY_TYPES for w in ws):
        return False

    risk = risk or _RiskCache(session, user.id)
    try:
        assessment = await risk.injury()
        health_report = await risk.health()
        if not assessment.actionable and not health_report.actionable:
            return False

        from app import health as health_mod
        from app import injury as injury_mod

        risk = {}
        if assessment.actionable:
            risk["injury"] = injury_mod.to_context(assessment)
        if health_report.actionable:
            risk["health"] = health_mod.to_context(health_report)["alerts"]

        plan, edit = await run_plan_adaptation(
            session, user_id=user.id, api_key=creds.anthropic_key,
            trigger="deload", window_days=ADAPT_WINDOW_DAYS_DEFAULT, risk=risk,
        )
    except AnalystError:
        logger.exception(f"DELOAD check failed user={user.id}")
        return False
    except Exception:
        logger.exception(f"DELOAD check failed user={user.id}")
        return False
    if plan is None or edit is None or not edit.operations:
        return False

    # Set the guard BEFORE sending — same "can't loop into a re-warn" reasoning as the
    # other risk hooks — and only once we know a real proposal is going out.
    await repository.set_state(session, user.id, INJURY_WARNED_KEY, today)
    await _send_adapt_proposal(ctx, session, user, plan.id, edit)
    logger.info(f"DELOAD proposal sent user={user.id}: injury={assessment.level} "
                f"health={[a['kind'] for a in risk.get('health', [])]}")
    return True


async def _token_expiry_check_for_user(ctx, session, user: User) -> None:
    """ST-11: decode the stored garth token's estimated OAuth1 death date
    (``app.garmin.token_info``, ~1y from issue) and DM a heads-up once the deadline is
    within TOKEN_WARN_THRESHOLDS days — so a re-login happens in /settings before the
    morning job starts hard-failing on it (OPS-01 turned from a fire into a scheduled
    chore). Pure decode, no network call; best-effort like the other risk hooks — a
    missing/undecodable token blob is a silent skip, never a tick failure."""
    if not user.telegram_chat_id or not user.garth_token_enc:
        return
    try:
        from app.core.crypto import decrypt
        from app.garmin.token_info import decode_token_info
        info = decode_token_info(decrypt(user.garth_token_enc))
    except Exception:
        return
    issued = info.get("oauth1_issued")
    expiry = info.get("oauth1_expiry_est")
    if not issued or not expiry:
        return
    days_left = (expiry.date() - dt.date.today()).days
    issued_iso = issued.date().isoformat()
    for threshold in TOKEN_WARN_THRESHOLDS:
        if days_left > threshold:
            continue
        guard_key = TOKEN_WARN_PREFIX + str(threshold)
        if await repository.get_state(session, user.id, guard_key) == issued_iso:
            continue
        await repository.set_state(session, user.id, guard_key, issued_iso)
        await ctx.bot.send_message(
            user.telegram_chat_id,
            f"⏳ Токен Garmin спливає приблизно {expiry.date().isoformat()} "
            "(≈рік від останнього повного логіну). Перелогінься завчасно в /settings, "
            "щоб ранкові звіти не перервались.",
        )
        logger.info(f"TOKEN_EXPIRY warn user={user.id} days_left={days_left} threshold={threshold}")
        return


async def _tick_for_user(ctx, session, user: User) -> None:
    # ST-14: window + "today" are per-user (their own timezone), not the process TZ — a
    # traveling user or a second user outside Europe/Warsaw gets their own morning, and
    # the once-a-day/week/month bot_state guard dates below key off their local date.
    now = dt.datetime.now(user_tz(user))
    today = now.date().isoformat()
    if not (MORNING_START_HOUR <= now.hour <= ACTIVITY_WATCH_END_HOUR):
        logger.debug(f"TICK skip user={user.id}: outside window (hour={now.hour})")
        return
    try:
        async with user_garmin_runtime(session, user, skip_label="TICK") as creds:
            if creds is None:
                return

            payload, new_activities = await service.build_payload_cached(
                session, user.id, days=3, activity_limit=20
            )

            # OPS-01/ST-11: warn well ahead of the ~1y garth token death date — pure decode,
            # no network, so it runs unconditionally alongside the other risk hooks.
            await _token_expiry_check_for_user(ctx, session, user)

            # Match freshly synced activities to planned workouts BEFORE the auto-analysis,
            # so it can compare plan vs actual instead of just narrating the raw activity —
            # best-effort, same pattern as _sync_for_user (a failure here must not block the tick).
            try:
                result = await matching.match_activities(session, user.id)
                if any(result.values()):
                    logger.info(f"MATCH user={user.id}: {result}")
            except Exception:
                logger.exception(f"MATCH failed user={user.id}")

            await _activity_watch_for_user(ctx, session, user, creds, new_activities)

            # Celebrate any new personal record (EP-14) — after the activity recap.
            await _records_check_for_user(ctx, session, user)

            # D1: one memo shared across all three risk hooks — the injury/health detectors
            # (each a set of ~90-day reads) get computed at most once per tick instead of up
            # to 3×, with every guard below unchanged.
            risk = _RiskCache(session, user.id)

            # NF-09 auto-deload: try turning an actionable injury/health signal into a
            # concrete ✅/❌ correction FIRST — that proposal IS the day's one risk
            # touchpoint, so the plain advisories below are skipped when it fires.
            deload_sent = await _deload_check_for_user(ctx, session, user, creds, today, risk=risk)

            if not deload_sent:
                # Injury-risk radar (NF-04) — a rare, guarded advisory when signals stack up.
                await _injury_check_for_user(ctx, session, user, creds, today, risk=risk)

                # Proactive health alerts (EP-08) — recovery anomalies vs the personal
                # baseline. Skip when an injury advisory already went out today: at most one
                # risk DM per day (the detectors share the "don't stack risk pings" rule).
                injury_sent = (
                    await repository.get_state(session, user.id, INJURY_WARNED_KEY) == today)
                if not injury_sent:
                    await _health_check_for_user(ctx, session, user, creds, today, risk=risk)

            if not (MORNING_START_HOUR <= now.hour <= MORNING_DEADLINE_HOUR):
                return
            if await repository.get_state(session, user.id, MORNING_STATE_KEY) == today:
                logger.debug(f"MORNING skip user={user.id}: already sent today")
            elif await _deliver_morning(ctx, session, user, creds, payload, now, today):
                await repository.set_state(session, user.id, MORNING_STATE_KEY, today)
                logger.info(f"MORNING sent for {today} user={user.id}")

            # Independent guard from the morning report above — runs even on a later
            # tick within the same 07-12 window after the report already went out.
            await _adapt_morning_check(ctx, session, user, creds, today)

            # Open-ended plans: ask (✅/❌) whether to add the next block when the plan is
            # about to run out. Confirm-only — generation happens on the ✅ tap, not here.
            await _extend_nudge_for_user(ctx, session, user, today)
    except GarminRateLimited:
        # Garmin kept answering 429 through every backoff retry — it's actively
        # throttling/blocking us, not a one-off blip. DM once/day per user so the
        # tighter CHECK_INTERVAL_MIN cadence can't turn this into a spam loop; the
        # next tick just tries again (no separate un-guard needed, "today" rolls over).
        guard_key = RATE_LIMIT_NOTIFIED_PREFIX + today
        if await repository.get_state(session, user.id, guard_key) != "1":
            await repository.set_state(session, user.id, guard_key, "1")
            if user.telegram_chat_id:
                await ctx.bot.send_message(user.telegram_chat_id, GARMIN_RATE_LIMITED_MSG)
        logger.warning(f"TICK Garmin rate-limited user={user.id}")
    except MFARequired:
        # A different process (this one) can't finish the login Garmin is asking
        # about — just point the user at /settings once/day, don't spam every tick.
        guard_key = MFA_NOTIFIED_PREFIX + today
        if await repository.get_state(session, user.id, guard_key) != "1":
            await repository.set_state(session, user.id, guard_key, "1")
            if user.telegram_chat_id:
                await ctx.bot.send_message(user.telegram_chat_id, MFA_REQUIRED_MSG)
        logger.warning(f"TICK MFA required user={user.id}")
    except Exception:
        logger.exception(f"TICK failed for user={user.id}")


async def _has_pending_proposal(session, user_id: int) -> bool:
    """True when an unanswered plan proposal is already waiting for this user. Every
    automatic proposer (weekly/morning adaptation, weather) checks this before querying
    Claude so we never send a second set of ✅/❌ buttons that would overwrite the first's
    pending ops in bot_state — the EP-13 "don't ping twice" pitfall, enforced across all
    hooks (they share PENDING_ADAPT_KEY + adapt_callback)."""
    return bool(await repository.get_state(session, user_id, PENDING_ADAPT_KEY))


def _adapt_ops_dump(edit) -> str:
    return json.dumps(
        {"ops": [op.model_dump() for op in edit.operations],
         "alt": [op.model_dump() for op in (edit.alt_operations or [])]},
        ensure_ascii=False,
    )


_DOW_UK = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]

_TYPE_UK = {
    "easy": "легкий біг", "long": "довга пробіжка", "tempo": "темпова",
    "intervals": "інтервали", "rest": "відпочинок", "cross": "кросс-тренування",
    "strength": "силова",
}


def _dow_label(iso: str) -> str:
    try:
        d = dt.date.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso or "?"
    return f"{_DOW_UK[d.weekday()]} {iso[5:]}"


def _type_label(t: Optional[str]) -> str:
    return _TYPE_UK.get((t or "").lower(), t or "тренування")


async def _op_change_line(session, plan_id: int, op) -> str:
    """One concrete before→after line for a single proposed PlanOp, so the proposal
    shows what actually changes instead of only the LLM's reasoning (the summary text
    alone left users unable to tell which session moved or by how much)."""
    d = _dow_label(op.date)
    if op.action == "add":
        dist = f" {op.dist_km:.0f} км" if op.dist_km else ""
        return f"• {d}: додається {_type_label(op.type)}{dist}"
    if op.action == "skip":
        return f"• {d}: скасовується"
    old = await repository.workout_on_date(session, plan_id, op.date)
    if op.action == "move" and op.to_date:
        nd = _dow_label(op.to_date)
        what = _type_label(old.type) if old else _type_label(op.type)
        return f"• {d} → {nd}: {what}"
    if op.action == "modify":
        parts = []
        if op.dist_km is not None:
            was = f"{old.dist_km:.0f}" if old and old.dist_km else "?"
            parts.append(f"{was} → {op.dist_km:.0f} км")
        if op.type is not None and (old is None or op.type != old.type):
            parts.append(_type_label(op.type))
        return f"• {d}: {', '.join(parts) if parts else 'деталі сесії'}"
    return f"• {d}: {op.action}"


async def _ops_changes_text(session, plan_id: int, ops: list) -> str:
    lines = [await _op_change_line(session, plan_id, op) for op in ops]
    return "\n".join(lines)


async def _send_adapt_proposal(ctx, session, user: User, plan_id: int, edit) -> None:
    """Store the proposed ops in bot_state (survives a bot restart, unlike
    context.user_data — see EP-02 pitfalls) and send the confirm/reject buttons."""
    await repository.set_state(session, user.id, PENDING_ADAPT_KEY, _adapt_ops_dump(edit))
    changes = await _ops_changes_text(session, plan_id, edit.operations)
    if edit.risky and edit.alt_operations:
        text = "📅 Пропоную скоригувати план.\n\n⚠️ " + edit.summary
        if changes:
            text += "\n\n" + changes
        if edit.alt_summary:
            text += "\n\n🛡 Безпечніше: " + edit.alt_summary
            alt_changes = await _ops_changes_text(session, plan_id, edit.alt_operations)
            if alt_changes:
                text += "\n" + alt_changes
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Як пропоновано", callback_data="adapt_apply")],
            [InlineKeyboardButton("🛡 Безпечніший варіант", callback_data="adapt_apply_alt")],
            [InlineKeyboardButton("❌ Відхилити", callback_data="adapt_cancel")],
        ])
    else:
        text = "📅 Пропоную скоригувати план.\n\n" + edit.summary
        if changes:
            text += "\n\n" + changes
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Прийняти", callback_data="adapt_apply"),
            InlineKeyboardButton("❌ Відхилити", callback_data="adapt_cancel"),
        ]])
    await ctx.bot.send_message(user.telegram_chat_id, text, reply_markup=kb)
    logger.info(f"ADAPT proposal sent user={user.id}: {len(edit.operations)} op(s)")


async def _adapt_morning_check(ctx, session, user: User, creds, today: str) -> None:
    """One-off morning nudge: if today's plan session is heavy (tempo/intervals/long)
    and readiness is low, ask Claude whether to ease/move just that session. Silent
    when the plan is fine or there's nothing heavy today. Guarded to at most once/day
    (the guard is set as soon as the check actually runs, not just when it proposes
    something, so a re-tick with the same low readiness doesn't re-query Claude)."""
    if not user.plan_adapt_enabled or not user.telegram_chat_id:
        return
    if await _has_pending_proposal(session, user.id):
        return
    guard_key = ADAPT_GUARD_PREFIX + today
    if await repository.get_state(session, user.id, guard_key) == "1":
        return
    ws = await repository.upcoming_plan_workouts(session, user.id, days=1)
    if not any((w.type or "").lower() in ADAPT_HEAVY_TYPES for w in ws):
        return
    ex = await repository.get_recent_extra(session, user.id, days=3)
    readiness = ex.get("readiness_score")
    if readiness is None or readiness >= settings.PLAN_ADAPT_READINESS_MIN:
        return

    await repository.set_state(session, user.id, guard_key, "1")
    try:
        plan, edit = await run_plan_adaptation(
            session, user_id=user.id, api_key=creds.anthropic_key,
            trigger="morning", window_days=0,
        )
    except AnalystError:
        logger.exception(f"ADAPT morning check failed user={user.id}")
        return
    if plan is None or edit is None or not edit.operations:
        return
    await _send_adapt_proposal(ctx, session, user, plan.id, edit)


async def _adapt_weekly_for_user(ctx, session, user: User) -> None:
    if not user.plan_adapt_enabled or not user.telegram_chat_id:
        return
    plan = await repository.get_active_plan(session, user.id)
    if plan is None:
        return
    if await _has_pending_proposal(session, user.id):
        return
    try:
        async with user_runtime(session, user) as creds:
            if not creds.anthropic_key:
                return
            _plan, edit = await run_plan_adaptation(
                session, user_id=user.id, api_key=creds.anthropic_key, trigger="weekly",
            )
    except AnalystError:
        logger.exception(f"ADAPT weekly failed user={user.id}")
        return
    if edit is None or not edit.operations:   # None → plan's adjust_level is off
        return
    await _send_adapt_proposal(ctx, session, user, plan.id, edit)


async def plan_adapt_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Weekly (Sunday evening by default) plan-adaptation review: propose a correction
    to the next window when compliance/recovery signals call for it. Silent when the
    plan is fine (no message sent) — see SYSTEM_PLAN_ADAPT."""
    async def worker(session, user):
        await _adapt_weekly_for_user(ctx, session, user)

    await for_each_user(worker, with_chat=True, label="PLAN adapt")


async def _weather_plan_for_user(ctx, session, user: User) -> None:
    """EP-13 daily check: if a key session (tempo/intervals/long) in the next
    WEATHER_DECISION_DAYS lands on an extreme-weather day, ask Claude to propose a
    move/modify with the same ✅/❌ buttons as EP-02. Fully silent (and zero Claude calls)
    when there's no conflict. Gated on a stored location + active plan + plan_adapt_enabled
    (the general auto-adjust switch), and yields to any already-pending proposal so we
    never ping the user twice."""
    if not user.plan_adapt_enabled or not user.telegram_chat_id:
        return
    if user.latitude is None or user.longitude is None:
        return   # no location → feature just doesn't activate (EP-13 AC)
    plan = await repository.get_active_plan(session, user.id)
    if plan is None:
        return
    if await _has_pending_proposal(session, user.id):
        logger.debug(f"WEATHER skip user={user.id}: proposal already pending")
        return

    decision_days = settings.WEATHER_DECISION_DAYS
    forecast = await run_in_threadpool(
        weather.fetch_forecast_week, user.latitude, user.longitude
    )
    if not forecast:
        return
    ws = await repository.upcoming_plan_workouts(session, user.id, days=decision_days + 1)
    conflicts = weather.find_weather_conflicts(
        forecast, [(w.date, w.type) for w in ws],
        today=dt.date.today(), decision_days=decision_days, heavy_types=ADAPT_HEAVY_TYPES,
        heat_feels_c=settings.WEATHER_HEAT_FEELS_C, rain_prob_pct=settings.WEATHER_RAIN_PROB_PCT,
        wind_kmh=settings.WEATHER_WIND_KMH,
    )
    if not conflicts:
        return   # no conflict → silence, no Claude call (EP-13 AC)
    logger.info(f"WEATHER user={user.id}: {len(conflicts)} conflict(s) "
                f"{[c['date'] for c in conflicts]}")
    try:
        async with user_runtime(session, user) as creds:
            if not creds.anthropic_key:
                return
            _plan, edit = await run_weather_plan_check(
                session, user_id=user.id, forecast=forecast, conflicts=conflicts,
                decision_days=decision_days, api_key=creds.anthropic_key,
            )
    except AnalystError:
        logger.exception(f"WEATHER check failed user={user.id}")
        return
    if edit is None or not edit.operations:
        return
    await _send_adapt_proposal(ctx, session, user, plan.id, edit)


async def weather_plan_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Daily weather-aware planning check (EP-13): propose moving a key session off an
    extreme-weather day. Silent when there's no conflict. Scheduled by run_daily."""
    async def worker(session, user):
        await _weather_plan_for_user(ctx, session, user)

    await for_each_user(worker, with_chat=True, label="WEATHER")


# NF-16 evening sleep nudge: once-a-evening guard keyed by the user's own local date
# (bot_state key sleep_nudge:<date>, ST-14) — a re-tick within the same evening stays quiet.
SLEEP_NUDGE_GUARD_PREFIX = "sleep_nudge:"


async def _sleep_nudge_for_user(ctx, session, user: User, today: str) -> None:
    """NF-16: a once-a-evening, zero-LLM heads-up when tomorrow's plan holds a key session
    (tempo/intervals/long) AND recent sleep shows a debt signal (``app.sleepnudge`` — reuses
    NF-01's own personal band, plus Garmin's own sleep_need vs actual gap). Either condition
    alone stays silent (EP-13's "no conflict, no message" rule) — never "before every tempo
    run". Pure DB read, zero Garmin/Claude calls (today's data is already synced by evening).
    Reuses ``User.alerts_enabled`` as the per-user off-switch (same wellness-push class as
    EP-08's health alerts) plus the process-level ``SLEEP_NUDGE`` toggle."""
    if not settings.SLEEP_NUDGE or not user.alerts_enabled or not user.telegram_chat_id:
        return
    guard_key = SLEEP_NUDGE_GUARD_PREFIX + today
    if await repository.get_state(session, user.id, guard_key) == "1":
        return
    tomorrow = (dt.date.fromisoformat(today) + dt.timedelta(days=1)).isoformat()
    ws = await repository.upcoming_plan_workouts(session, user.id, days=2)
    if not sleepnudge.tomorrow_is_heavy([w.type for w in ws if w.date == tomorrow]):
        return
    history = await repository.read_history(session, user.id, days=baselines.WINDOW_DAYS)
    if not sleepnudge.has_sleep_debt(history):
        return
    await repository.set_state(session, user.id, guard_key, "1")
    await ctx.bot.send_message(user.telegram_chat_id, sleepnudge.NUDGE_TEXT)
    logger.info(f"SLEEP_NUDGE sent user={user.id}")


async def sleep_nudge_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Evening check (NF-16): a heads-up before a heavy session on a sleep-debt night.
    Silent when there's no conflict of the two conditions. Scheduled by run_daily at
    ``SLEEP_NUDGE_HOUR`` (process TZ in v1 — see ``_sleep_nudge_for_user``'s per-user guard
    date, ST-14); the job's own firing hour is the one piece that stays global."""
    async def worker(session, user):
        today = dt.datetime.now(user_tz(user)).date().isoformat()
        await _sleep_nudge_for_user(ctx, session, user, today)

    await for_each_user(worker, with_chat=True, label="SLEEP_NUDGE")


async def _deliver_digest(ctx, session, user: User, creds, *, force: bool = False) -> bool:
    """Build + send the weekly digest for one user. Returns True if a message was sent.
    Reads only from the DB (no Garmin fetch). Does NOT touch the once-a-week guard —
    the caller owns it (or bypasses it, for /test_digest)."""
    try:
        text = await run_digest(session, user_id=user.id, api_key=creds.anthropic_key)
    except AnalystError as e:
        logger.error(f"ANALYST {e}")
        text = str(e)
    if not text:   # nothing to report (no history, no plan)
        return False
    prefix = "🧪 [тест] " if force else ""
    await ctx.bot.send_message(user.telegram_chat_id, prefix + "🗓 Тижневий підсумок\n\n" + text)
    return True


async def _monthly_compare_for_user(ctx, session, user: User, creds) -> None:
    """Once a calendar month (on the first weekly digest of the month), append a NF-06
    "you vs a year ago" block. Best-effort: guarded via bot_state so it fires at most once
    a month, and any failure/empty result is silent — it never breaks the digest send. The
    guard is set only after a message actually goes out, so a no-history month retries next
    week rather than burning the month."""
    if not user.telegram_chat_id:
        return
    local_today = dt.datetime.now(user_tz(user)).date()
    guard_key = COMPARE_GUARD_PREFIX + local_today.strftime("%Y-%m")
    if await repository.get_state(session, user.id, guard_key) == "1":
        return
    try:
        text = await run_compare(
            session, user_id=user.id, weeks=COMPARE_WEEKS, api_key=creds.anthropic_key
        )
    except AnalystError:
        logger.exception(f"COMPARE monthly failed user={user.id}")
        return
    if not text:
        return   # not enough history a year back — retry next week, don't set the guard
    from app import compare as compare_mod

    cur_s, cur_e, past_s, past_e = compare_mod.window_pair(local_today, COMPARE_WEEKS)
    header = (f"📅 Ти зараз ({compare_mod.fmt_range(cur_s, cur_e)}) проти себе рік тому "
              f"({compare_mod.fmt_range(past_s, past_e)}):\n\n")
    await ctx.bot.send_message(user.telegram_chat_id, header + text)
    await repository.set_state(session, user.id, guard_key, "1")
    logger.info(f"COMPARE monthly sent user={user.id} month={guard_key}")


async def _monthly_insights_for_user(ctx, session, user: User, creds) -> None:
    """Once a calendar month (on the first weekly digest of the month), append a NF-02
    correlation-insight block ("what actually affects you"). Best-effort and guarded via
    bot_state so it fires at most once a month; a no-findings month leaves the guard unset
    (so it retries next week) and any failure is silent — it never breaks the digest send."""
    if not user.telegram_chat_id:
        return
    guard_key = INSIGHTS_GUARD_PREFIX + dt.datetime.now(user_tz(user)).strftime("%Y-%m")
    if await repository.get_state(session, user.id, guard_key) == "1":
        return
    try:
        text = await run_insights(session, user_id=user.id, api_key=creds.anthropic_key)
    except AnalystError:
        logger.exception(f"INSIGHTS monthly failed user={user.id}")
        return
    if not text:
        return   # no significant correlations yet — retry next week, don't set the guard
    await ctx.bot.send_message(user.telegram_chat_id, "🔎 Що на тебе впливає:\n\n" + text)
    await repository.set_state(session, user.id, guard_key, "1")
    logger.info(f"INSIGHTS monthly sent user={user.id} month={guard_key}")


async def _digest_for_user(ctx, session, user: User) -> None:
    """Scheduled weekly digest for one user, guarded to once per ISO week via bot_state."""
    if not user.telegram_chat_id:
        return
    guard_key = DIGEST_GUARD_PREFIX + dt.datetime.now(user_tz(user)).strftime("%G-W%V")
    if await repository.get_state(session, user.id, guard_key) == "1":
        logger.debug(f"DIGEST skip user={user.id}: already sent this week")
        return
    async with user_runtime(session, user) as creds:
        if not creds.anthropic_key:
            return
        if await _deliver_digest(ctx, session, user, creds):
            await repository.set_state(session, user.id, guard_key, "1")
            logger.info(f"DIGEST sent user={user.id} week={guard_key}")
            # Monthly NF-06 comparison + NF-02 insight blocks, riding on the first digest of
            # the month (each self-guards to once a month via bot_state).
            await _monthly_compare_for_user(ctx, session, user, creds)
            await _monthly_insights_for_user(ctx, session, user, creds)


async def force_digest_for_user(ctx, session, user: User) -> None:
    """Send the weekly digest on demand, bypassing the once-a-week guard (and leaving it
    untouched, so the scheduled one still fires). For the hidden /test_digest command."""
    async with user_runtime(session, user) as creds:
        if not creds.anthropic_key:
            await ctx.bot.send_message(user.telegram_chat_id, "🧪 Немає Anthropic-ключа.")
            return
        await _deliver_digest(ctx, session, user, creds, force=True)


async def weekly_digest_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Weekly (Sunday evening) retrospective digest: this week's volume/compliance vs last
    week, recovery/fitness trends, and an honest progress-to-goal read. One message per
    user with a chat id; guarded once-a-week via bot_state (EP-07). Scheduled by run_daily,
    so no time-window guard is needed."""
    async def worker(session, user):
        await _digest_for_user(ctx, session, user)

    await for_each_user(worker, with_chat=True, label="DIGEST")


async def _sync_for_user(session, user: User) -> None:
    """Reconcile one user's Garmin calendar with their plan. Binds the user's provider;
    the cleanup pass runs even with no active plan (to remove a just-archived plan's
    pushed workouts). Skips users with sync disabled, nothing to do, or no creds."""
    if not user.garmin_sync_enabled:
        return
    plan = await repository.get_active_plan(session, user.id)
    pushed = await repository.list_pushed_workouts(session, user.id)
    if plan is None and not pushed:
        return
    async with user_garmin_runtime(session, user) as creds:
        if creds is None:
            return
        await plan_sync.sync_plan_to_garmin(session, user.id)


RACE_PACK_GUARD_PREFIX = "race_pack_sent:"


async def _race_pack_for_user(ctx, session, user: User) -> None:
    """EP-05: send the race pack exactly once, ``race.TRIGGER_DAYS`` before the active
    plan's target date. Pure DB read + weather + one Opus call (no Garmin fetch needed,
    unlike most other jobs here) — guarded per-plan (not per-date), so a fresh plan/target
    date naturally re-arms it and a missed tick never loses the trigger."""
    if not user.telegram_chat_id:
        return
    plan = await repository.get_active_plan(session, user.id)
    if not race.has_target(plan):
        return
    today = dt.datetime.now(user_tz(user)).date()
    if race.days_to_target(plan.target_date, today) != race.TRIGGER_DAYS:
        return
    guard_key = RACE_PACK_GUARD_PREFIX + str(plan.id)
    if await repository.get_state(session, user.id, guard_key) == "1":
        return
    creds = load_credentials(user)
    if not creds.anthropic_key:
        return
    try:
        text = await run_race_plan(session, user_id=user.id, api_key=creds.anthropic_key)
    except AnalystError:
        logger.exception(f"RACE pack failed user={user.id}")
        return
    await repository.set_state(session, user.id, guard_key, "1")
    if not text:
        return
    await ctx.bot.send_message(
        user.telegram_chat_id,
        f"🏁 Race pack — твій старт за {race.TRIGGER_DAYS} дн.:\n\n" + text,
    )
    logger.info(f"RACE pack sent user={user.id} plan={plan.id}")


async def _sync_gear_roster(session, user: User) -> list:
    """Refresh this user's gear roster + Garmin's own per-gear mileage (NF-15) into a
    ``gear.STATE_KEY`` bot_state JSON blob, so ``/gear`` reads it back without a live
    fetch. Best-effort: an unparseable/missing gear item is just dropped by
    ``gear.parse_item`` (logged once), never a broken sync."""
    from app.garmin import client

    raw_items = await run_in_threadpool(client.fetch_gear)
    pairs = []
    for raw in raw_items:
        item = gear.parse_item(raw)
        if item is None:
            continue
        stats = await run_in_threadpool(client.fetch_gear_stats, item["gear_id"])
        item["mileage_km"] = gear.parse_mileage_km(stats)
        item["last_used"] = gear.parse_last_used(stats)
        pairs.append(item)
    await repository.set_state(session, user.id, gear.STATE_KEY, json.dumps(pairs))
    return pairs


async def _gear_check_for_user(ctx, session, user: User) -> None:
    """NF-15: refresh the gear roster/mileage and warn once (then every
    ``settings.GEAR_REWARN_KM`` further) per pair past ``settings.GEAR_WEAR_KM``. Runs
    from the daily ``plan_sync_job`` rather than every 20-min tick — a live gear fetch
    isn't cheap enough to repeat that often, and mileage barely moves within a day."""
    async with user_garmin_runtime(session, user, skip_label="GEAR") as creds:
        if creds is None:
            return
        try:
            pairs = await _sync_gear_roster(session, user)
        except Exception:
            logger.exception(f"GEAR sync failed user={user.id}")
            return
    if not user.telegram_chat_id:
        return
    for pair in gear.worn(pairs, settings.GEAR_WEAR_KM):
        guard_key = gear.WARN_PREFIX + pair["gear_id"]
        last = await repository.get_state(session, user.id, guard_key)
        last_km = float(last) if last else None
        if not gear.should_rewarn(pair["mileage_km"], last_km, settings.GEAR_REWARN_KM):
            continue
        await repository.set_state(session, user.id, guard_key, str(pair["mileage_km"]))
        await ctx.bot.send_message(user.telegram_chat_id, gear.warn_text(pair))
        logger.info(f"GEAR warn user={user.id} gear={pair['gear_id']} km={pair['mileage_km']}")


async def plan_sync_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Once-a-day per-user Garmin calendar sync (separate from the morning report);
    scheduled by run_daily, so no further time guard is needed. Also carries the EP-05
    race-pack auto-trigger and NF-15's gear-mileage refresh — different daily concerns,
    but this job already runs once/day for every user, the cheapest place to hang more
    once-a-day checks."""
    async def worker(session, user):
        await _sync_for_user(session, user)
        await _race_pack_for_user(ctx, session, user)
        await _gear_check_for_user(ctx, session, user)

    await for_each_user(worker, with_chat=False, label="PLAN sync")


async def _extend_nudge_for_user(ctx, session, user: User, today: str) -> None:
    """Morning nudge for an open-ended (``general``) plan that's about to run out (last
    workout within PLAN_EXTEND_LEAD_DAYS): ask ✅/❌ whether to add the next block. This is
    **confirm-only** — the actual (paid Opus) generation happens in ``plan_extend_callback``
    on a ✅, never here. Guarded once/day (so an in-window re-tick doesn't re-ask); a prior
    ❌ snoozes it for a few days. Cheap: pure DB reads, zero Claude calls."""
    if not user.telegram_chat_id:
        return
    guard_key = EXTEND_NUDGE_PREFIX + today
    if await repository.get_state(session, user.id, guard_key) == "1":
        return
    snooze = await repository.get_state(session, user.id, PLAN_EXTEND_SNOOZE_KEY)
    if snooze and snooze >= today:   # ISO dates compare lexically
        return
    plan = await repository.get_active_plan(session, user.id)
    if plan is None or plan.target_date or plan.goal != OPEN_ENDED_GOAL:
        return
    last = await repository.last_workout_date(session, plan.id)
    if not last:
        return
    try:
        days_left = (dt.date.fromisoformat(last) - dt.date.today()).days
    except ValueError:
        return
    if days_left > settings.PLAN_EXTEND_LEAD_DAYS:
        return   # still plenty of runway — nothing to ask yet

    await repository.set_state(session, user.id, guard_key, "1")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Продовжити", callback_data="planext:yes"),
        InlineKeyboardButton("❌ Не зараз", callback_data="planext:no"),
    ]])
    left = "сьогодні" if days_left <= 0 else f"за ~{days_left} дн."
    await ctx.bot.send_message(
        user.telegram_chat_id,
        f"🗓 Твій план бігу добігає кінця ({left}). Додати наступні "
        f"{settings.PLAN_BLOCK_WEEKS} тижнів?",
        reply_markup=kb,
    )
    logger.info(f"PLAN extend nudge sent user={user.id} plan={plan.id} ({days_left}d left)")


async def morning_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Per-tick entry point: fetch once per user (07:00-23:00 in EACH user's own timezone,
    ST-14), run activity watch, and — within the narrower 07-12 window — the morning
    report. See module docstring. Runs every CHECK_INTERVAL_MIN regardless of the process's
    own clock; ``_tick_for_user`` is what enforces the (per-user) window."""
    async def worker(session, user):
        await _tick_for_user(ctx, session, user)

    await for_each_user(worker, with_chat=True, label="MORNING")
