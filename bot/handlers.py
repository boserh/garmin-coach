"""Telegram command handlers + the user lookup and error handler.

Business logic lives in the shared core (app.garmin.service / app.analysis.service);
handlers only orchestrate fetch → analyze → reply, each within a DB session and the
matched user's runtime context (their Garmin provider + Claude key). The bot is one
global identity; a chat is authorised by mapping its chat_id to a registered user.
"""
import datetime as dt
import json
import logging
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import ContextTypes

from app import deploy as deploy_ops
from app import records, weather
from app.analysis import delivery
from app.analysis.service import (
    AnalystError,
    run_activity_analysis,
    run_analysis,
    run_ask,
    run_plan_edit,
    run_plan_extension,
)
from app.core.config import settings
from app.db import users
from app.db.base import async_session_maker
from app.db.models import User
from app.garmin import plan_sync, repository, service
from app.garmin.mfa import MFARequired
from app.garmin.runtime import user_runtime
from app.garmin.schemas import PlanOp

logger = logging.getLogger("bot")

TZ = ZoneInfo("Europe/Warsaw")

# bot_state key for a pending adaptive-plan proposal (EP-02) — stored per-user, not
# context.user_data, because the proposal can originate from a job (no chat context).
PENDING_ADAPT_KEY = "pending_adapt"

# bot_state key: date (ISO) until which the open-ended-plan extend nudge stays quiet after
# an explicit ❌ (set by plan_extend_callback, read by the morning nudge). "" = not snoozed.
PLAN_EXTEND_SNOOZE_KEY = "extend_snooze"

_REPORT_Q = "Оціни відновлення і дай пораду до наступної запланованої пробіжки."
_DEEP_Q = "Глибокий розбір сну, HRV і навантаження за два тижні."
_NOT_REGISTERED = (
    "Тебе не зареєстровано. Додай цей chat_id у налаштуваннях веб-кабінету, "
    "щоб бот працював з твоїми даними."
)
MFA_REQUIRED_MSG = (
    "🔐 Garmin просить код підтвердження (MFA). Заверши вхід у Налаштуваннях "
    "веб-кабінету — там з'явиться поле для коду."
)
GARMIN_RATE_LIMITED_MSG = (
    "🚦 Garmin тимчасово заблокував запити (забагато звернень). Пробую рідше — "
    "нічого робити не потрібно, дані підтягнуться пізніше."
)

HELP_TEXT = (
    "🤖 Команди бота:\n\n"
    "📋 Звіти та аналіз\n"
    "/report — звіт відновлення за 7 днів (Sonnet)\n"
    "/deep <питання> — глибокий аналіз сну/HRV/навантаження (Opus), "
    "напр. /deep вплив вело на HRV\n"
    "/ask <питання> — питання по всій твоїй історії тренувань і відновлення, "
    "напр. /ask коли я востаннє біг швидше 5:00/км\n"
    "/compare [тижнів] — порівняння з собою рік тому\n"
    "/wrapped [рік|квартал] — святковий підсумок сезону (Opus)\n"
    "/insights — що на тебе насправді впливає (кореляції сну/HRV/стресу)\n\n"
    "🏃 Активності\n"
    "/activities — останні активності\n"
    "/activity <id> — розбір конкретної активності\n"
    "/checkin [rpe] [нотатка] — оцінити останнє тренування (RPE + чи боліло)\n"
    "/records — особисті рекорди\n"
    "/costs [YYYY-MM] — витрати на Claude за місяць\n"
    "/gear — спорядження (кросівки) з пробігом\n\n"
    "🩺 Здоров'я\n"
    "/risk — травматичний радар (сигнали перевантаження)\n"
    "/health — алерти відновлення (HRV, сон, стрес)\n\n"
    "🗓 План\n"
    "/plan — переглянути програму\n"
    "/plan <текст> — змінити програму, напр. /plan додай біг сьогодні\n"
    "/sick [днів] — захворів/у подорожі: перебудувати найближчий блок плану\n"
    "/goal — кількісний прогрес до цілі (прогноз Garmin + тренд)\n"
    "/race — race pack: пейсинг/харчування/чекліст до цільового старту (Opus)\n\n"
    "/help — цей список"
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


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/help — static list of commands. No DB/user lookup: useful even before registration."""
    logger.info("CMD /help")
    await update.message.reply_text(HELP_TEXT)


async def report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    logger.info("CMD /report")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        await update.message.reply_text("Тягну дані з Garmin...")
        async with user_runtime(session, user) as creds:
            payload, _ = await service.build_payload_cached(
                session, user.id, days=7, activity_limit=20
            )
            try:
                result = await delivery.build_report(
                    session, user, payload, question=_REPORT_Q,
                    kind="report", api_key=creds.anthropic_key,
                    weather=await weather.forecast_for_user(user),
                )
                note = "" if result.synced_today else delivery.STALE_NOTE + "\n\n"
                text = result.text
            except AnalystError as e:
                logger.error(f"ANALYST {e}")
                note, text = "", str(e)
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
            payload, _ = await service.build_payload_cached(
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
    """/ask <питання> — EP-09: a bounded tool-use agent over the full stored history.
    Pure DB read + Claude calls; no Garmin fetch, so load_credentials (not user_runtime)
    is enough, like /compare."""
    from app.garmin.credentials import load_credentials

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
        await update.message.reply_text("Шукаю у твоїй історії...")
        creds = load_credentials(user)
        try:
            text = await run_ask(
                session, question, user_id=user.id, api_key=creds.anthropic_key
            )
        except AnalystError as e:
            logger.error(f"ANALYST {e}")
            text = str(e)
    await update.message.reply_text(text)


async def activities(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    logger.info("CMD /activities")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        rows = await repository.list_activities(session, user.id, n=5)
    if not rows:
        await update.message.reply_text(
            "Немає збережених активностей. Зроби /report, щоб синканути дані."
        )
        return
    lines = ["Останні активності:"]
    for a in rows:
        parts = [a["type"] or "активність"]
        if a["dist_km"]:
            parts.append(f"{a['dist_km']:.1f} км")
        if a["dur_min"]:
            parts.append(f"{a['dur_min']:.0f} хв")
        if a["avg_hr"]:
            parts.append(f"♥{a['avg_hr']}")
        lines.append(f"#{a['id']}  {a['date']}  {' · '.join(parts)}")
    lines.append("\nРозбір: /activity <id>")
    await update.message.reply_text("\n".join(lines))


async def records_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/records — the user's current personal bests (EP-14). Pure DB read, no LLM/Garmin."""
    logger.info("CMD /records")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        rows = await repository.current_records(session, user.id)
    if not rows:
        await update.message.reply_text(
            "Поки без рекордів 🏅 Вони зʼявляться, коли назбирається історія пробіжок — "
            "найшвидші 5/10 км, найдовший біг, найоб'ємніший тиждень, новий VO2max."
        )
        return
    order = {k: i for i, k in enumerate(records.DISPLAY_ORDER)}
    rows.sort(key=lambda r: order.get(r.kind, 99))
    lines = ["🏅 Твої особисті рекорди:"]
    lines += [records.format_record_line(r, with_prev=False) + f"  ({r.date})" for r in rows]
    lines.append("\nРахуємо по цілих пробіжках (не відрізках всередині довшого бігу).")
    await update.message.reply_text("\n".join(lines))


_COST_KIND_UK = {
    "report": "щоденний звіт", "morning": "ранковий звіт", "deep": "глибокий аналіз",
    "ask": "/ask", "activity": "активність", "plan": "план", "digest": "дайджест",
    "compare": "порівняння", "wrapped": "wrapped", "insights": "інсайти",
    "injury": "травма-радар", "health": "health-алерт", "weather": "погода",
    "sick": "sick", "strength": "силова",
}


def _parse_month_arg(arg: Optional[str], tz) -> "tuple | None":
    """``arg`` is ``YYYY-MM`` or empty (→ the current month in ``tz``). ``None`` on
    garbage input, so the caller can show a format hint instead of guessing."""
    if not arg:
        now = dt.datetime.now(tz)
        return now.year, now.month
    try:
        d = dt.datetime.strptime(arg, "%Y-%m")
    except ValueError:
        return None
    return d.year, d.month


def _month_bounds_utc(year: int, month: int, tz) -> "tuple[dt.datetime, dt.datetime]":
    """[start, end) of the calendar month in ``tz``, converted to UTC for the query."""
    start_local = dt.datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        end_local = dt.datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end_local = dt.datetime(year, month + 1, 1, tzinfo=tz)
    return start_local.astimezone(dt.timezone.utc), end_local.astimezone(dt.timezone.utc)


def _format_costs(agg: dict, year: int, month: int) -> str:
    label = f"{year}-{month:02d}"
    if agg["calls"] == 0:
        return f"💰 Витрати за {label}: викликів не було."
    lines = [f"💰 Витрати за {label}: ${agg['total_usd']:.2f}",
             f"Викликів: {agg['calls']} (з кешу: {agg['cached']})"]
    if agg["by_kind"]:
        lines.append("")
        lines.append("По типах:")
        for kind, b in sorted(agg["by_kind"].items(), key=lambda kv: kv[1]["cost"], reverse=True):
            lines.append(f"• {_COST_KIND_UK.get(kind, kind)}: ${b['cost']:.2f} ({b['calls']})")
    if agg["top3"]:
        lines.append("")
        lines.append("Найдорожчі виклики:")
        for t in agg["top3"]:
            label = _COST_KIND_UK.get(t["kind"], t["kind"])
            lines.append(f"• {t['date']} {label}: ${t['cost']:.4f}")
    return "\n".join(lines)


async def costs_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/costs [YYYY-MM] — this month's (or a named month's) Claude spend (ST-12): total $,
    breakdown by kind, call count, cache-hit share, top-3 priciest calls. Pure DB read
    (`repository.costs_for_month`), no Garmin/Claude — like /records and /compare."""
    from bot.jobs import user_tz

    arg = ctx.args[0] if ctx.args else None
    logger.info(f"CMD /costs {arg or ''}")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        tz = user_tz(user)
        parsed = _parse_month_arg(arg, tz)
        if parsed is None:
            await update.message.reply_text(
                "Невірний формат місяця. Приклад: /costs 2026-06 (без аргументу — поточний)."
            )
            return
        year, month = parsed
        start, end = _month_bounds_utc(year, month, tz)
        agg = await repository.costs_for_month(session, user.id, start, end)
    await update.message.reply_text(_format_costs(agg, year, month))


async def goal_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/goal — quantified progress toward the active plan's target (NF-10): Garmin's own
    race-time-prediction trend (or VO2max for the open-ended goal), projected forward.
    Pure DB read, zero Claude calls in the minimal version — no Garmin fetch, no MFA risk."""
    from app import goal as goal_mod

    logger.info("CMD /goal")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        plan = await repository.get_active_plan(session, user.id)
        if plan is None:
            await update.message.reply_text(
                "Спершу створи план на /plan — /goal рахує прогрес відносно нього."
            )
            return
        metric_key, label, higher_better = goal_mod.metric_for_goal(plan.goal)
        history = await repository.read_fitness_history(session, user.id)
        proj = goal_mod.project(
            history, metric_key=metric_key, higher_better=higher_better,
            target_date=plan.target_date,
        )
    if proj is None:
        await update.message.reply_text(
            "Замало даних для тренду — потрібно кілька тижнів історії з прогнозами "
            "Garmin (race predictions / VO2max). Повернись пізніше."
        )
        return
    await update.message.reply_text(goal_mod.summary(proj, label=label))


async def race_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/race — a pre-race pack: pacing/fueling/checklist synthesis for the active plan's
    target race (EP-05). Pure DB read + one Opus call; no Garmin fetch, so no MFA risk."""
    from app import race as race_mod
    from app.analysis.service import run_race_plan
    from app.garmin.credentials import load_credentials

    logger.info("CMD /race")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        plan = await repository.get_active_plan(session, user.id)
        if not race_mod.has_target(plan):
            await update.message.reply_text(
                "Немає активного плану з цільовим стартом (дата + дистанція) — "
                "race pack рахується лише під конкретний забіг. Постав ціль на /plan."
            )
            return
        await update.message.reply_text("Складаю race pack…")
        creds = load_credentials(user)
        try:
            text = await run_race_plan(session, user_id=user.id, api_key=creds.anthropic_key)
        except AnalystError as e:
            logger.error(f"ANALYST {e}")
            await update.message.reply_text(str(e))
            return
    if not text:
        await update.message.reply_text(
            "Немає активного плану з цільовим стартом (дата + дистанція)."
        )
        return
    days_left = race_mod.days_to_target(plan.target_date)
    left = f"за {days_left} дн." if days_left and days_left > 0 else "уже скоро"
    header = f"🏁 Race pack — {plan.goal_label or plan.goal} ({left}):\n\n"
    await update.message.reply_text(header + text)


async def gear_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/gear — this user's tracked gear (shoes/equipment) with mileage + last-used date
    (NF-15). Pure DB read of the roster the daily plan_sync_job last refreshed — no live
    Garmin fetch here, so it's instant."""
    from app import gear as gear_mod

    logger.info("CMD /gear")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        raw = await repository.get_state(session, user.id, gear_mod.STATE_KEY)
    pairs = json.loads(raw) if raw else []
    if not pairs:
        await update.message.reply_text(
            "Ще немає даних про спорядження — з'являться після наступного щоденного "
            "синку (або в Garmin Connect не ведеться gear на пробіжки)."
        )
        return
    lines = [gear_mod.summary_line(p) for p in pairs]
    text = "👟 Твоє спорядження:\n\n" + "\n".join(lines)
    note = gear_mod.dominance_note(pairs)
    if note:
        text += "\n\n" + note
    await update.message.reply_text(text)


async def compare(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/compare [тижнів] — compare current fitness with the same span a year ago (NF-06).
    Pure DB read + one Sonnet call; no Garmin fetch, so no MFA risk."""
    from app import compare as compare_mod
    from app.analysis.service import run_compare
    from app.garmin.credentials import load_credentials

    weeks = compare_mod.parse_period(ctx.args)
    logger.info(f"CMD /compare {weeks}w")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        await update.message.reply_text("Порівнюю тебе з тобою рік тому…")
        creds = load_credentials(user)
        try:
            text = await run_compare(
                session, user_id=user.id, weeks=weeks, api_key=creds.anthropic_key
            )
        except AnalystError as e:
            logger.error(f"ANALYST {e}")
            await update.message.reply_text(str(e))
            return
    if not text:
        await update.message.reply_text(
            "Замало історії для порівняння — треба дані і за цей період, і за той самий "
            "період рік тому. Повернись, коли назбирається історія "
            "(або зроби бекфіл GDPR-експорту)."
        )
        return
    cur_start, cur_end, past_start, past_end = compare_mod.window_pair(dt.date.today(), weeks)
    header = (f"📅 Ти зараз ({compare_mod.fmt_range(cur_start, cur_end)}) "
              f"проти себе рік тому ({compare_mod.fmt_range(past_start, past_end)}):\n\n")
    await update.message.reply_text(header + text)


async def wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wrapped [рік|квартал] — a celebratory season recap (NF-07). Pure DB read + one Opus
    call; no Garmin fetch, so no MFA risk."""
    from app import wrapped as wrapped_mod
    from app.analysis.service import run_wrapped
    from app.garmin.credentials import load_credentials

    period = wrapped_mod.parse_period(ctx.args)
    logger.info(f"CMD /wrapped {period}")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        await update.message.reply_text(
            f"Збираю твій підсумок ({wrapped_mod.label(period)})… це трохи коштує, тому раз "
            "на сезон 🙂")
        creds = load_credentials(user)
        try:
            text = await run_wrapped(
                session, user_id=user.id, period=period, api_key=creds.anthropic_key
            )
        except AnalystError as e:
            logger.error(f"ANALYST {e}")
            await update.message.reply_text(str(e))
            return
    if not text:
        await update.message.reply_text(
            "Замало історії для підсумку — назбирай кілька пробіжок (або зроби бекфіл "
            "GDPR-експорту) і повертайся."
        )
        return
    start, end = wrapped_mod.period_window(dt.date.today(), period)
    header = f"✨ Твій {wrapped_mod.label(period)}: {wrapped_mod.fmt_range(start, end)}\n\n"
    await update.message.reply_text(header + text)


async def insights(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/insights — personal correlation findings (NF-02): "what actually affects you". Pure
    DB read + one Sonnet call only when there's a significant pattern; no Garmin, no MFA."""
    from app.analysis.service import run_insights
    from app.garmin.credentials import load_credentials

    logger.info("CMD /insights")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        await update.message.reply_text("Шукаю закономірності у твоїх даних…")
        creds = load_credentials(user)
        try:
            text = await run_insights(session, user_id=user.id, api_key=creds.anthropic_key)
        except AnalystError as e:
            logger.error(f"ANALYST {e}")
            await update.message.reply_text(str(e))
            return
    if not text:
        await update.message.reply_text(
            "Поки що не бачу статистично надійних закономірностей — треба більше історії "
            "відновлення (сон/HRV/стрес за кілька тижнів). Повернись пізніше 🙂"
        )
        return
    await update.message.reply_text("🔎 Що на тебе впливає:\n\n" + text)


async def risk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/risk — the injury-radar signals right now (NF-04). Pure DB read, no LLM/Garmin: the
    detector is zero-LLM, so this is instant and free."""
    from app import injury
    from app.analysis.service import build_injury_assessment

    logger.info("CMD /risk")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        a = await build_injury_assessment(session, user_id=user.id)
    if a.level == "calibrating":
        await update.message.reply_text(
            f"🩺 Травматичний радар ще калібрується — треба ≥{settings.INJURY_MIN_HISTORY_DAYS} "
            f"днів історії (зараз {a.history_days}). Збираю дані, попереджу, якщо щось насторожить."
        )
        return
    if not a.actionable:
        await update.message.reply_text(
            "🟢 Тривожних сигналів немає. Навантаження, відновлення й самопочуття в нормі — "
            "тримай так."
        )
        return
    await update.message.reply_text(injury.summary(a))


async def health(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/health — proactive recovery alerts right now (EP-08). Pure DB read, no LLM/Garmin:
    the detector is zero-LLM (personal-baseline anomalies), so this is instant and free."""
    from app import health as health_mod
    from app.analysis.service import build_health_alerts

    logger.info("CMD /health")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        report = await build_health_alerts(session, user_id=user.id)
    if report.level == "calibrating":
        await update.message.reply_text(
            f"🩺 Алерти відновлення ще калібруються — треба ≥{settings.HEALTH_MIN_HISTORY_DAYS} "
            f"днів історії (зараз {report.history_days}). Збираю базлайн, попереджу, якщо метрики "
            f"поповзуть у поганий бік."
        )
        return
    if not report.actionable:
        await update.message.reply_text(
            "🟢 Відновлення в нормі — HRV, пульс спокою, сон і стрес у межах твого коридору."
        )
        return
    await update.message.reply_text(health_mod.summary(report))


async def activity(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text(
            "Вкажи id активності, напр.: /activity 5  (список — /activities)"
        )
        return
    row_id = int(ctx.args[0])
    logger.info(f"CMD /activity {row_id}")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        act = await repository.get_activity(session, user.id, row_id)
        if act is None:
            await update.message.reply_text(f"Активність #{row_id} не знайдено.")
            return
        await update.message.reply_text("Аналізую активність...")
        async with user_runtime(session, user) as creds:
            try:
                text = await run_activity_analysis(
                    session, act, user_id=user.id, api_key=creds.anthropic_key
                )
            except AnalystError as e:
                logger.error(f"ANALYST {e}")
                await update.message.reply_text(str(e))
                return
    # Offer the post-run check-in (EP-12) unless it's already been answered.
    if act.subjective:
        await update.message.reply_text(text)
    else:
        await update.message.reply_text(
            f"{text}\n\n{CHECKIN_PROMPT}", reply_markup=checkin_keyboard(act.id)
        )


# ---------- POST-RUN CHECK-IN (EP-12) ----------

# The check-in status footer is appended below the activity analysis; we split it off
# the current message text on each edit so re-taps rewrite (never stack) the footer.
_CI_SEP = "\n— — —\n"
# Common running niggles → (callback slug, Ukrainian label). Kept to buttons (not free
# text) so the state stays entirely in callback_data — see the EP-12 pitfall.
_PAIN_PARTS = [
    ("knee", "коліно"), ("shin", "гомілка"), ("foot", "стопа"),
    ("thigh", "стегно"), ("calf", "литка"), ("back", "спина"), ("other", "інше"),
]
_PART_LABELS = dict(_PAIN_PARTS)


# Shown above the RPE 1-10 buttons so the numbers are self-explanatory without a legend
# tap — "1" alone next to a run recap reads as noise, not a question.
CHECKIN_PROMPT = "Наскільки важко відчувалось (RPE)? 1 — дуже легко, 10 — межа можливостей:"


def checkin_keyboard(aid: int) -> InlineKeyboardMarkup:
    """RPE 1–10 (one tap) + a «щось боліло» opener. ``aid`` is the DB activity id, so the
    callback is fully stateless."""
    rpe = [InlineKeyboardButton(str(n), callback_data=f"ci:rpe:{aid}:{n}") for n in range(1, 11)]
    return InlineKeyboardMarkup([
        rpe[:5], rpe[5:],
        [InlineKeyboardButton("🩹 Щось боліло", callback_data=f"ci:pain:{aid}")],
    ])


def _pain_keyboard(aid: int) -> InlineKeyboardMarkup:
    """Body-part buttons + a «без болю» dismiss — the second (optional) tap."""
    parts = [InlineKeyboardButton(lbl, callback_data=f"ci:part:{aid}:{slug}")
             for slug, lbl in _PAIN_PARTS]
    return InlineKeyboardMarkup([
        parts[:4], parts[4:],
        [InlineKeyboardButton("🆗 Без болю", callback_data=f"ci:ok:{aid}")],
    ])


def _ci_render(current_text: str, status: str) -> str:
    """Rebuild the message: the original analysis (everything before the footer marker)
    plus the fresh check-in status line."""
    base = current_text.split(_CI_SEP)[0].rstrip()
    return f"{base}{_CI_SEP}{status}"


async def checkin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle the RPE / pain buttons. Callback data carries the activity id, so no chat
    state is kept. Re-tapping overwrites the stored value (repository.set_subjective)."""
    q = update.callback_query
    await q.answer()
    # ci:rpe:<aid>:<n> | ci:pain:<aid> | ci:part:<aid>:<slug> | ci:ok:<aid>
    parts = q.data.split(":")
    action, aid = parts[1], int(parts[2])
    async with async_session_maker() as session:
        user = await users.get_by_chat_id(session, q.message.chat.id)
        if user is None or not (user.is_active and user.is_approved):
            await q.edit_message_text(_NOT_REGISTERED)
            return

        if action == "pain":
            # Open the body-part picker without touching stored data yet.
            await q.edit_message_text(
                _ci_render(q.message.text, "🩹 Що саме боліло?"),
                reply_markup=_pain_keyboard(aid),
            )
            return

        if action == "rpe":
            act = await repository.set_subjective(session, user.id, aid, rpe=int(parts[3]))
            status, kb = (f"✅ RPE {parts[3]}/10. Щось боліло?", _pain_keyboard(aid))
        elif action == "part":
            note = _PART_LABELS.get(parts[3], parts[3])
            act = await repository.set_subjective(session, user.id, aid, note=note)
            status, kb = (f"✅ Записав: 🩹 {note}.", None)
        else:  # ok — no pain
            act = await repository.set_subjective(session, user.id, aid, pain=False)
            status, kb = ("✅ Записав: болю немає.", None)

        if act is None:
            await q.edit_message_text("Активність не знайдено.")
            return
        await session.commit()
    await q.edit_message_text(_ci_render(q.message.text, status), reply_markup=kb)


async def checkin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manual post-run check-in for the last activity (if the buttons were ignored).
    ``/checkin`` → show the RPE keyboard; ``/checkin 7`` → set RPE; ``/checkin 7 коліно``
    or ``/checkin коліно`` → also record a niggle note."""
    logger.info("CMD /checkin")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        act = await repository.get_last_activity(session, user.id)
        if act is None:
            await update.message.reply_text(
                "Немає активностей для оцінки. Зроби /report, щоб синканути дані."
            )
            return

        args = ctx.args or []
        rpe, note = None, None
        if args and args[0].isdigit() and 1 <= int(args[0]) <= 10:
            rpe = int(args[0])
            note = " ".join(args[1:]).strip() or None
        elif args:
            note = " ".join(args).strip()

        if rpe is None and note is None:   # no args → offer the buttons
            head = f"{act.type or 'активність'}"
            if act.dist_km:
                head += f" · {act.dist_km:.1f} км"
            await update.message.reply_text(
                f"Як пройшло? {head} ({act.date})\n\n{CHECKIN_PROMPT}",
                reply_markup=checkin_keyboard(act.id),
            )
            return

        await repository.set_subjective(session, user.id, act.id, rpe=rpe, note=note)
        await session.commit()
    bits = []
    if rpe is not None:
        bits.append(f"RPE {rpe}/10")
    if note:
        bits.append(f"🩹 {note}")
    await update.message.reply_text("✅ Записав: " + ", ".join(bits) + ".")


async def plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    instruction = " ".join(ctx.args).strip()
    if instruction:
        await _plan_edit(update, ctx, instruction)
        return
    logger.info("CMD /plan")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        p = await repository.get_active_plan(session, user.id)
        if p is None:
            await update.message.reply_text(
                "Немає активної програми. Створи її на сторінці /plan у вебі."
            )
            return
        ws = await repository.list_workouts(session, p.id, upcoming_only=True)
    if not ws:
        await update.message.reply_text(
            f"🎯 {p.goal_label or p.goal}: майбутніх тренувань немає."
        )
        return
    lines = [f"🎯 {p.goal_label or p.goal}", ""]
    for w in ws[:10]:
        dist = f" · {w.dist_km:.1f} км" if w.dist_km else ""
        lines.append(f"{w.date}  {w.type}{dist}\n  {w.description}")
    lines.append("\nКоригувати: /plan <що змінити>")
    await update.message.reply_text("\n".join(lines))


async def _plan_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE, instruction: str):
    """Propose a free-text plan change and offer confirm/cancel buttons."""
    logger.info(f"CMD /plan edit: {instruction[:60]}")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        await update.message.reply_text("Думаю над змінами...")
        async with user_runtime(session, user) as creds:
            try:
                _plan, edit = await run_plan_edit(
                    session, user_id=user.id, instruction=instruction,
                    api_key=creds.anthropic_key,
                )
            except AnalystError as e:
                logger.error(f"ANALYST {e}")
                await update.message.reply_text(str(e))
                return
    if not edit.operations:
        await update.message.reply_text(edit.summary or "Не зрозумів, що змінити.")
        return
    ops = [op.model_dump() for op in edit.operations]
    alt = [op.model_dump() for op in (edit.alt_operations or [])]
    ctx.user_data["pending_plan"] = {"ops": ops, "alt": alt}

    if edit.risky and alt:
        # risky request → keep what the user asked AND offer the coach's safer version,
        # so the user explicitly chooses (apply-as-asked / take-suggestion / cancel).
        text = "⚠️ " + edit.summary
        if edit.alt_summary:
            text += "\n\n🛡 Безпечніше: " + edit.alt_summary
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Як просив{_ops_hint(ops)}", callback_data="plan_apply")],
            [InlineKeyboardButton(f"🛡 Пропоноване{_ops_hint(alt)}",
                                  callback_data="plan_apply_alt")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="plan_cancel")],
        ])
    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Застосувати", callback_data="plan_apply"),
            InlineKeyboardButton("❌ Скасувати", callback_data="plan_cancel"),
        ]])
        text = "Пропоную:\n\n" + edit.summary
    await update.message.reply_text(text, reply_markup=kb)


async def sick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/sick [днів] — NF-03: ремонт плану після хвороби/подорожі одним тапом. Опційний
    аргумент — скільки днів уже пропущено/буде пропущено (напр. "/sick 3"); без нього —
    консервативний дефолт (сьогодні-завтра легко). Пропонує перебудову блоку через той
    самий confirm-флоу, що /plan <текст> (плюс skip у палітрі дій)."""
    from app.analysis.service import run_sick_check
    from app.garmin.credentials import load_credentials

    days_missed = 0
    if ctx.args and ctx.args[0].isdigit():
        days_missed = int(ctx.args[0])
    logger.info(f"CMD /sick days_missed={days_missed}")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        await update.message.reply_text("Перебудовую план під хворобу/подорож…")
        creds = load_credentials(user)
        try:
            _plan, edit = await run_sick_check(
                session, user_id=user.id, days_missed=days_missed,
                api_key=creds.anthropic_key,
            )
        except AnalystError as e:
            logger.error(f"ANALYST {e}")
            await update.message.reply_text(str(e))
            return
    if _plan is None:
        await update.message.reply_text(
            "Немає активної програми. Створи її на сторінці /plan у вебі."
        )
        return
    if not edit.operations:
        await update.message.reply_text(edit.summary or "Перебудовувати нічого.")
        return
    ops = [op.model_dump() for op in edit.operations]
    ctx.user_data["pending_plan"] = {"ops": ops, "alt": []}
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Застосувати", callback_data="plan_apply"),
        InlineKeyboardButton("❌ Скасувати", callback_data="plan_cancel"),
    ]])
    await update.message.reply_text(
        "🤒 Пропоную перебудову:\n\n" + edit.summary, reply_markup=kb
    )


def _ops_hint(ops: list) -> str:
    """A short hint for a button label — the first op's distance, else an exercise swap."""
    for o in ops:
        d = o.get("dist_km")
        if d:
            return f" · {d:.0f} км"
    for o in ops:
        if o.get("action") == "add" and o.get("type") == "strength":
            nm = (o.get("strength") or {}).get("name") or o.get("description")
            return f" · 🏋️ {nm}" if nm else " · 🏋️ силова"
    for o in ops:
        if o.get("action") == "swap_exercise" and o.get("to_category"):
            from app.garmin import exercises
            return f" · {exercises.label(o['to_category'])}"
    return ""


async def plan_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pending = ctx.user_data.pop("pending_plan", None)
    if q.data == "plan_cancel":
        await q.edit_message_text("Скасовано. План без змін.")
        return
    # plan_apply → the literal request; plan_apply_alt → the safer counter-proposal.
    ops_data = (pending or {}).get("alt" if q.data == "plan_apply_alt" else "ops")
    if not ops_data:
        await q.edit_message_text("Немає змін для застосування.")
        return
    async with async_session_maker() as session:
        user = await users.get_by_chat_id(session, q.message.chat.id)
        if user is None or not (user.is_active and user.is_approved):
            await q.edit_message_text(_NOT_REGISTERED)
            return
        plan_obj = await repository.get_active_plan(session, user.id)
        if plan_obj is None:
            await q.edit_message_text("Немає активної програми.")
            return
        affected = await repository.apply_plan_ops(
            session, plan_obj, [PlanOp(**o) for o in ops_data]
        )
        # Mirror just the edited sessions onto the Garmin calendar (best-effort — the
        # daily job reconciles anything missed; a Garmin outage never blocks the edit).
        if user.garmin_sync_enabled:
            try:
                async with user_runtime(session, user):
                    await plan_sync.resync_workouts(session, user.id, affected)
            except Exception:
                logger.exception(f"PLAN edit sync failed user={user.id}")
    await q.edit_message_text(f"✅ Застосовано змін: {len(affected)}. /plan — переглянути.")


async def adapt_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Confirm/reject a proposal from the adaptive-plan hooks (EP-02: weekly review or
    the morning nudge). Mirrors ``plan_callback`` but reads pending ops from bot_state
    (``PENDING_ADAPT_KEY``) since the proposal may have been sent by a background job,
    not a live chat turn."""
    q = update.callback_query
    await q.answer()
    async with async_session_maker() as session:
        user = await users.get_by_chat_id(session, q.message.chat.id)
        if user is None or not (user.is_active and user.is_approved):
            await q.edit_message_text(_NOT_REGISTERED)
            return
        pending_raw = await repository.get_state(session, user.id, PENDING_ADAPT_KEY)
        await repository.set_state(session, user.id, PENDING_ADAPT_KEY, "")  # single-use

        if q.data == "adapt_cancel":
            await q.edit_message_text("Відхилено. План без змін.")
            return
        if not pending_raw:
            await q.edit_message_text("Пропозиція вже неактуальна.")
            return
        pending = json.loads(pending_raw)
        ops_data = pending.get("alt" if q.data == "adapt_apply_alt" else "ops")
        if not ops_data:
            await q.edit_message_text("Немає змін для застосування.")
            return
        plan_obj = await repository.get_active_plan(session, user.id)
        if plan_obj is None:
            await q.edit_message_text("Немає активної програми.")
            return
        affected = await repository.apply_plan_ops(
            session, plan_obj, [PlanOp(**o) for o in ops_data]
        )
        if user.garmin_sync_enabled:
            try:
                async with user_runtime(session, user):
                    await plan_sync.resync_workouts(session, user.id, affected)
            except Exception:
                logger.exception(f"ADAPT sync failed user={user.id}")
    await q.edit_message_text(f"✅ Застосовано змін: {len(affected)}. /plan — переглянути.")


async def plan_extend_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Answer the morning "продовжити безстроковий план?" nudge (open-ended plans).
    ``planext:yes`` generates the next block on demand (a real Opus call — only ever after
    this explicit tap); ``planext:no`` snoozes the nudge for a few days. Idempotent against
    a stale button: ✅ re-checks the plan still needs extending before spending anything."""
    q = update.callback_query
    await q.answer()
    async with async_session_maker() as session:
        user = await users.get_by_chat_id(session, q.message.chat.id)
        if user is None or not (user.is_active and user.is_approved):
            await q.edit_message_text(_NOT_REGISTERED)
            return

        if q.data == "planext:no":
            until = (dt.date.today() + dt.timedelta(days=settings.PLAN_EXTEND_SNOOZE_DAYS))
            await repository.set_state(
                session, user.id, PLAN_EXTEND_SNOOZE_KEY, until.isoformat())
            await q.edit_message_text("Ок, поки що не чіпаю план. Нагадаю пізніше.")
            return

        # ✅ — re-verify the plan is still an open-ended one about to run out (guards against
        # a double tap / a stale button from a previous day after it was already extended).
        plan = await repository.get_active_plan(session, user.id)
        if plan is None or plan.target_date:
            await q.edit_message_text("Немає безстрокового плану для продовження.")
            return
        last = await repository.last_workout_date(session, plan.id)
        days_left = None
        if last:
            try:
                days_left = (dt.date.fromisoformat(last) - dt.date.today()).days
            except ValueError:
                days_left = None
        if days_left is not None and days_left > settings.PLAN_EXTEND_LEAD_DAYS:
            await q.edit_message_text("План уже продовжено — усе актуально. /plan.")
            return

        await q.edit_message_text("⏳ Генерую наступні тижні, це може зайняти хвилину…")
        try:
            async with user_runtime(session, user) as creds:
                if not creds.anthropic_key:
                    await q.edit_message_text("Немає Anthropic-ключа — не можу згенерувати.")
                    return
                extended = await run_plan_extension(
                    session, user_id=user.id, api_key=creds.anthropic_key)
                if extended is not None and user.garmin_sync_enabled:
                    try:
                        await plan_sync.sync_plan_to_garmin(session, user.id)
                    except Exception:
                        logger.exception(f"PLAN extend sync failed user={user.id}")
        except AnalystError as e:
            await q.edit_message_text(f"Не вдалось продовжити план: {e}")
            return
        except Exception:
            logger.exception(f"PLAN extend failed user={user.id}")
            await q.edit_message_text("Не вдалось продовжити план. Спробуй пізніше.")
            return
    await q.edit_message_text("✅ Додав наступні тижні до плану. /plan — переглянути.")


# ---------- TEST JOB ----------

async def test_job(ctx: ContextTypes.DEFAULT_TYPE):
    # Runs the exact morning-report path (weather included), same as the scheduled job.
    from bot.jobs import force_morning_for_user

    user_id = ctx.job.data["user_id"]
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user is None:
            return
        await force_morning_for_user(ctx, session, user)


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


async def test_morning(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Force the real morning report now (weather included), bypassing the time window
    and once-a-day guard — without consuming today's guard, so the scheduled one still fires."""
    from bot.jobs import force_morning_for_user

    logger.info("CMD /test_morning")
    await update.message.reply_text("🧪 Генерую ранковий звіт…")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        await force_morning_for_user(ctx, session, user)


async def test_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Force the weekly digest now (no once-a-week guard), so a test exercises the exact
    digest path without consuming the week's guard. Hidden debug command (EP-07)."""
    from bot.jobs import force_digest_for_user

    logger.info("CMD /test_digest")
    await update.message.reply_text("🧪 Генерую тижневий підсумок…")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        await force_digest_for_user(ctx, session, user)


async def deploy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only (OPS-03): propose a git pull + service restart, confirmed via inline
    buttons since the restart kills the very process handling this update."""
    logger.info("CMD /deploy")
    async with async_session_maker() as session:
        user = await _resolve_user(update, session)
        if user is None:
            return
        if not user.is_admin:
            await update.message.reply_text("Ця команда лише для адмінів.")
            return
    if not settings.DEPLOY_ENABLED:
        await update.message.reply_text(
            "Деплой з бота вимкнений (DEPLOY_ENABLED=false у .env)."
        )
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Так, деплой", callback_data="deploy:yes"),
        InlineKeyboardButton("❌ Скасувати", callback_data="deploy:no"),
    ]])
    await update.message.reply_text(
        "🚀 Задеплоїти зараз? git pull + перезапуск garmin-bot і garmin-web.",
        reply_markup=kb,
    )


def _fmt_deploy_failure(label: str, result: "deploy_ops.CommandResult") -> str:
    # A denied sudo call can produce an empty pipe (the rejection goes to the syslog
    # auth log, not this process' stdout/stderr) — always show the return code so the
    # message never looks silently truncated. See journalctl -t sudo on the host.
    body = result.output[-1500:] or "(порожній вивід — див. `journalctl -t sudo` на хості)"
    return f"❌ {label} (код {result.returncode}):\n{body}"


async def deploy_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "deploy:no":
        await q.edit_message_text("Скасовано.")
        return
    async with async_session_maker() as session:
        user = await users.get_by_chat_id(session, q.message.chat.id)
        if user is None or not (user.is_active and user.is_approved):
            await q.edit_message_text(_NOT_REGISTERED)
            return
        if not user.is_admin:
            await q.edit_message_text("Ця команда лише для адмінів.")
            return
    await q.edit_message_text("⏳ git pull…")
    pull = await deploy_ops.git_pull()
    if not pull.ok:
        await q.message.reply_text(_fmt_deploy_failure("git pull провалився", pull))
        return
    await q.message.reply_text(f"📥 {pull.output[-1500:] or '(без змін)'}")
    await q.message.reply_text("🔄 Перезапускаю сервіси…")
    restart = await deploy_ops.restart_services()
    if not restart.ok:
        await q.message.reply_text(_fmt_deploy_failure("Перезапуск не вдався", restart))
        return
    # restart_services runs the actual systemctl call inside its own transient systemd
    # unit (app.deploy docstring), so — unlike a direct child of this process — this
    # confirmation is NOT racing garmin-bot's own restart: it reliably means the job was
    # queued, not a guess made before this process might get killed.
    await q.message.reply_text("✅ Рестарт запущено.")


# ---------- ERROR HANDLER ----------

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning(f"TG network: {type(err).__name__}: {err}")
    elif isinstance(err, MFARequired):
        logger.warning(f"MFA required user={err.user_id}")
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(MFA_REQUIRED_MSG)
    else:
        logger.exception("Unhandled bot error", exc_info=err)
    # A failed inline-button tap (plan/adapt/checkin callbacks) otherwise leaves the
    # button visibly stuck — the user taps and, from their side, nothing happens. Best
    # effort: pop a toast so they know the tap failed and to retry, instead of silence.
    cbq = getattr(update, "callback_query", None) if isinstance(update, Update) else None
    if cbq is not None:
        try:
            await cbq.answer("Сталася помилка, спробуй ще раз.", show_alert=False)
        except Exception:
            pass
