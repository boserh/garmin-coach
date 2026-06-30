"""Telegram command handlers + the user lookup and error handler.

Business logic lives in the shared core (app.garmin.service / app.analysis.service);
handlers only orchestrate fetch → analyze → reply, each within a DB session and the
matched user's runtime context (their Garmin provider + Claude key). The bot is one
global identity; a chat is authorised by mapping its chat_id to a registered user.
"""
import logging
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import ContextTypes

from app.analysis.service import (
    AnalystError,
    run_activity_analysis,
    run_analysis,
    run_ask,
    run_plan_edit,
)
from app.db import users
from app.db.base import async_session_maker
from app.db.models import User
from app.garmin import plan_sync, repository, service
from app.garmin.runtime import user_runtime
from app.garmin.schemas import PlanOp

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
                text = str(e)
    await update.message.reply_text(text)


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


def _ops_hint(ops: list) -> str:
    """A short ' · N км' hint for a button label, from the first op carrying a distance."""
    for o in ops:
        d = o.get("dist_km")
        if d:
            return f" · {d:.0f} км"
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
        try:
            async with user_runtime(session, user):
                await plan_sync.resync_workouts(session, user.id, affected)
        except Exception:
            logger.exception(f"PLAN edit sync failed user={user.id}")
    await q.edit_message_text(f"✅ Застосовано змін: {len(affected)}. /plan — переглянути.")


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


# ---------- ERROR HANDLER ----------

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning(f"TG network: {type(err).__name__}: {err}")
    else:
        logger.exception("Unhandled bot error", exc_info=err)
