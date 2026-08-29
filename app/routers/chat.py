"""EP-11: web chat with the same run_ask / run_plan_edit engine the bot's /ask and
/plan <text> already use — a single input box, routed to the right engine by a simple
heuristic, with HTML confirm/cancel buttons for a plan-edit proposal.

ST-23 adds a dialogue turn on top of it: the pending-proposal card carries its own input
(``refine=1``) whose message is fed back into ``run_plan_edit`` **with the pending
proposal as context** — a question is answered without touching the proposal, a
correction replaces it with a new one. The dialogue rides inside the same pending blob
(``thread``), so it is shared with Telegram exactly like the proposal itself.

The pending-edit state lives in ``bot_state`` (``repository.set_pending_plan_edit`` /
``pop_pending_plan_edit``), the same DB-backed key/value store EP-02's adaptation
proposals already use — so a proposal shown here can be confirmed from Telegram and
vice versa, and it survives a bot/web restart (EP-11's AC). Chat history is read
straight off ``ReportLog`` (``repository.get_chat_history``): the bot's /ask and
/plan <text>/`/sick` already log every turn there, user-scoped not chat-scoped, so a
question asked in Telegram shows up in the web transcript too, with no new table.

**Deliberate v1 limitation** (documented, not a bug — matches how the rest of this
backlog notes a scoped-down first cut): responses are NOT token-streamed. The ticket's
SSE AC would mean moving the Anthropic client off the dedicated sync threadpool
PERF-04b deliberately chose (see CLAUDE.md) onto ``AsyncAnthropic`` — a much larger,
separate change than this router. Every turn is a plain POST + full-page reload, so the
"no-JS still works" AC holds by construction (there's no JS-only fast path to fall back
from yet).
"""
import datetime as dt
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import away as away_mod
from app.analysis.client import AnalystError
from app.analysis.plans import run_plan_edit
from app.analysis.reports import run_ask
from app.core.auth import current_user
from app.core.demo import DEMO_DISABLED_MSG
from app.core.tz import user_tz as core_user_tz
from app.db import away as away_db
from app.db.models import User
from app.dependencies import get_session
from app.garmin import plan_sync, providers, repository
from app.garmin.credentials import load_credentials
from app.garmin.mfa import MFARequired
from app.garmin.providers import GarminAuthFailed
from app.garmin.runtime import user_runtime
from app.garmin.schemas import PlanOp
from app.templating import create_templates

logger = logging.getLogger("api")

templates = create_templates()

router = APIRouter(tags=["chat"])

CHAT_HISTORY_N = 30       # how many exchanges to show initially / add per "load more"
CHAT_HISTORY_MAX = 500    # hard cap so a crafted ?limit= can't pull the whole history

CONFIRM_NO_ACTION_MSG = (
    "Не зрозумів, що робити з пропозицією — онови сторінку і натисни кнопку ще раз."
)


def _user_tz(user: User) -> ZoneInfo:
    """This user's IANA timezone (ST-14), falling back to Europe/Warsaw on a bad value —
    so chat timestamps read in the user's own local time, like the rest of the app.
    Thin alias for the canonical ``app.core.tz.user_tz``."""
    return core_user_tz(user)


def _with_local_time(history: list, tz: ZoneInfo) -> list:
    """Annotate each chat turn with a ``when`` string (date + time in the user's timezone)
    from its stored UTC ``created_at``."""
    for h in history:
        iso = h.get("created_at")
        when = ""
        if iso:
            try:
                when = dt.datetime.fromisoformat(iso).astimezone(tz).strftime("%d.%m.%Y %H:%M")
            except ValueError:
                when = ""
        h["when"] = when
    return history

# A pragmatic, conservative v1 heuristic (documented limitation, in the spirit of NF-16's
# "no bedtime clock" or NF-15's desk-only recon note): imperative plan-editing verbs route
# to run_plan_edit, everything else — including a QUESTION about the plan, since
# get_training_plan is one of /ask's own EP-09 tools — goes to run_ask. A miss just falls
# through to run_ask, which can still explain itself; never a dead end.
_PLAN_EDIT_VERBS = (
    "перенеси", "перенос", "пересунь", "зсунь", "додай", "додати", "прибери", "прибрати",
    "видали", "скасуй", "скасувати", "заміни", "замінити", "зменш", "збільш", "полегш",
    "ускладни", "постав", "зроби довш", "зроби коротш", "зроби легш", "зроби важч",
)


def _looks_like_plan_edit(text: str) -> bool:
    t = text.lower()
    return any(v in t for v in _PLAN_EDIT_VERBS)


@asynccontextmanager
async def _plan_edit_runtime(session, user: User):
    """Bind this user's Garmin provider for a plan edit — but never let Garmin block one.

    The only Garmin call under ``run_plan_edit`` is the best-effort strength-template read
    (``fetch_workout_full``), so a broken link must degrade to "no exercise detail", not to
    a 409 page in place of the proposal. A gate raised *before* we enter (MFA pending,
    credentials marked invalid) therefore falls back to a context bound to a provider that
    refuses; anything raised *inside* the block is the edit's own failure and propagates
    untouched."""
    entered = False
    try:
        async with user_runtime(session, user) as creds:
            entered = True
            yield creds
    except (GarminAuthFailed, MFARequired) as exc:
        if entered:
            raise
        logger.info(f"CHAT plan edit without Garmin for user={user.id}: {exc!r}")
        # Bind a provider that refuses rather than leaving the context unbound: an unbound
        # one falls through to the legacy .env single-user provider, which on a seeded
        # deployment would answer THIS user's request with the seed account's Garmin data.
        token = providers.set_current_provider(
            providers.build_unavailable_provider(f"Garmin unavailable for user {user.id}")
        )
        try:
            yield load_credentials(user)
        finally:
            providers.reset_current_provider(token)


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    limit: int = Query(CHAT_HISTORY_N, ge=1),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    # "Load more" grows the window (?limit=60, 90, …) backwards in time. The query returns
    # the newest exchanges first because that's the efficient way to take a window off the
    # end; the page then REVERSES it, because a chat reads oldest → newest downward. It
    # used to render the query order straight through, so the thread ran newest-first with
    # the composer above it — a reverse-chronological feed, not a conversation.
    limit = max(CHAT_HISTORY_N, min(limit, CHAT_HISTORY_MAX))
    history = await repository.get_chat_history(session, user.id, n=limit + 1)
    has_more = len(history) > limit
    history = _with_local_time(history[:limit], _user_tz(user))
    history.reverse()
    pending = await repository.get_pending_plan_edit(session, user.id)
    return templates.TemplateResponse(
        request, "chat.html",
        {"user": user, "history": history, "pending": pending,
         "has_more": has_more, "next_limit": limit + CHAT_HISTORY_N,
         # Jump to the newest turn only on the default view — after "load more" the reader
         # is looking at older messages and must not be yanked back to the bottom.
         "jump_to_latest": "limit" not in request.query_params,
         "error": request.query_params.get("err")},
    )


@router.post("/chat", response_class=HTMLResponse)
async def chat_send(
    message: str = Form(...),
    refine: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """``refine=1`` (ST-23) comes from the input inside the pending-proposal card: the
    message is then a follow-up **to that proposal** — a question about it or a
    correction — rather than a message routed by the plan-edit/ask heuristic. Keeping it
    an explicit field (not "pending exists ⇒ everything is a follow-up") means the main
    composer still answers an unrelated «як мій сон?» while a proposal waits."""
    text = message.strip()
    if not text:
        return RedirectResponse("/chat", status_code=303)
    if user.is_demo:
        return RedirectResponse(f"/chat?err={quote(DEMO_DISABLED_MSG)}", status_code=303)
    pending = await repository.get_pending_plan_edit(session, user.id) if refine else None
    creds = load_credentials(user)
    try:
        if pending or _looks_like_plan_edit(text):
            # run_plan_edit reads the plan's strength templates off Garmin, so it needs a
            # bound per-user provider — exactly like the bot's /plan <text>. Without it
            # get_provider() fell through to the legacy .env single-user provider and every
            # template fetch died with `KeyError: 'GARMIN_EMAIL'` (visible as a GARMIN ERR
            # line), leaving the model to propose edits with no exercises in front of it.
            async with _plan_edit_runtime(session, user) as edit_creds:
                _plan, edit = await run_plan_edit(
                    session, user_id=user.id, instruction=text,
                    api_key=edit_creds.anthropic_key, pending=pending,
                )
            # NF-34: a trip mentioned in passing rides with the proposal and is written on
            # the same confirmation (or dropped with it on cancel).
            away = away_mod.from_op(edit.away) or (pending or {}).get("away")
            if edit.operations or away:
                ops = [op.model_dump() for op in edit.operations]
                alt = [op.model_dump() for op in (edit.alt_operations or [])]
                await repository.set_pending_plan_edit(
                    session, user.id, ops, alt,
                    summary=edit.summary, alt_summary=edit.alt_summary, risky=edit.risky,
                    instruction=(pending or {}).get("instruction") or text,
                    thread=repository.append_thread(pending, text, edit.answer) if pending
                    else [],
                    away=away,
                )
            elif pending:
                # a question about the proposal — it stays exactly as it was, only the
                # dialogue thread grows (so the next follow-up keeps the context).
                await repository.set_pending_plan_edit(
                    session, user.id, pending.get("ops") or [], pending.get("alt") or [],
                    summary=pending.get("summary"), alt_summary=pending.get("alt_summary"),
                    risky=bool(pending.get("risky")),
                    instruction=pending.get("instruction"),
                    thread=repository.append_thread(pending, text,
                                                    edit.answer or edit.summary),
                    message=pending.get("message"),
                    away=pending.get("away"),
                )
        else:
            await run_ask(session, text, user_id=user.id, api_key=creds.anthropic_key)
    except AnalystError as e:
        # A failure BEFORE any Claude call (e.g. "no active plan") never reaches
        # ReportLog, so it can't show up as a chat turn on reload — surface it via a
        # query-string flash instead (same pattern as /settings' ``?tz=fail``).
        return RedirectResponse(f"/chat?err={quote(str(e)[:200])}", status_code=303)
    return RedirectResponse("/chat", status_code=303)


@router.post("/chat/confirm", response_class=HTMLResponse)
async def chat_confirm(
    action: str = Form(""),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Confirm/reject a pending free-text plan edit. Mirrors ``bot.handlers.plan_callback``
    almost exactly — same pending-state helper, same apply + best-effort Garmin resync.

    ``action`` is deliberately optional-with-validation rather than ``Form(...)``: a body
    that arrives without it (a stale cached app.js, a client that drops the submitter)
    must not be answered with FastAPI's raw 422 JSON in place of the chat — and must not
    fall into the apply branch either, which is what a plain default would do. An
    unrecognised action leaves the proposal exactly where it is and says so."""
    if action not in ("apply", "apply_alt", "cancel"):
        logger.warning(f"CHAT confirm without a valid action user={user.id} ({action!r})")
        return RedirectResponse(
            f"/chat?err={quote(CONFIRM_NO_ACTION_MSG)}", status_code=303,
        )
    pending = await repository.pop_pending_plan_edit(session, user.id)
    if action != "cancel" and pending:
        # NF-34: a trip declared inside the edit is written on the same confirmation as the
        # plan changes — through the same helper the bot's confirm uses, so the two paths
        # cannot disagree about whether it was recorded.
        await away_db.apply_pending(session, user.id, pending)
        ops_data = pending.get("alt" if action == "apply_alt" else "ops")
        if ops_data:
            plan_obj = await repository.get_active_plan(session, user.id)
            if plan_obj is not None:
                affected = await repository.apply_plan_ops(
                    session, plan_obj, [PlanOp(**o) for o in ops_data]
                )
                if user.garmin_sync_enabled and not user.is_demo:
                    try:
                        async with user_runtime(session, user):
                            await plan_sync.resync_workouts(session, user.id, affected)
                    except Exception:
                        logger.exception(f"CHAT plan edit sync failed user={user.id}")
    return RedirectResponse("/chat", status_code=303)
