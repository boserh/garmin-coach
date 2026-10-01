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

Each message runs as a background job (``app.livejobs``): ``POST /chat`` returns at once,
the answer streams to the page over ``/live/{id}/events`` as Claude writes it (``/ask``'s
rounds are streamed by ``_complete_tools``; a plan edit reports a status line and ends with
the proposal card's HTML). No JavaScript still works: the post redirects back here, the
page shows the message as in progress and refreshes until it's answered.
"""
import datetime as dt
import logging
from contextlib import asynccontextmanager
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import away as away_mod
from app import livejobs, planschedule
from app.analysis.plans import run_plan_edit, schedule_proposal
from app.analysis.reports import run_ask
from app.core.auth import current_user
from app.core.demo import DEMO_DISABLED_MSG
from app.core.tz import user_tz as core_user_tz
from app.db import away as away_db
from app.db.base import async_session_maker
from app.db.models import User
from app.dependencies import get_session
from app.garmin import plan_sync, providers, repository
from app.garmin.credentials import load_credentials
from app.garmin.mfa import MFARequired
from app.garmin.providers import GarminAuthFailed
from app.garmin.runtime import user_runtime
from app.garmin.schemas import PlanOp
from app.routers import plan as plan_router
from app.templating import create_templates

logger = logging.getLogger("api")

templates = create_templates()

router = APIRouter(tags=["chat"])

CHAT_HISTORY_N = 30       # how many exchanges to show initially / add per "load more"
CHAT_HISTORY_MAX = 500    # hard cap so a crafted ?limit= can't pull the whole history

CONFIRM_NO_ACTION_MSG = (
    "Не зрозумів, що робити з пропозицією — онови сторінку і натисни кнопку ще раз."
)
CHAT_BUSY_MSG = "Ще відповідаю на попереднє повідомлення — зачекай кілька секунд."
REBUILD_BUSY_MSG = (
    "Програма зараз генерується — підтверди перебудову, коли це завершиться."
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
    "замість",
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
    pending = _pending_view(await repository.get_pending_plan_edit(session, user.id))
    live = livejobs.running(user.id, "chat")
    error = request.query_params.get("err")
    last = livejobs.latest(user.id, "chat")
    if not error and last is not None and last.error() and not last.seen:
        # A turn that failed before any Claude call (e.g. "no active plan") never reaches
        # report_logs, so it can't appear in the thread — say it once here instead.
        error, last.seen = last.error(), True
    return templates.TemplateResponse(
        request, "chat.html",
        {"user": user, "history": history, "pending": pending, "live": live,
         "has_more": has_more, "next_limit": limit + CHAT_HISTORY_N,
         # Jump to the newest turn only on the default view — after "load more" the reader
         # is looking at older messages and must not be yanked back to the bottom.
         "jump_to_latest": "limit" not in request.query_params,
         "error": error},
    )


async def _process_message(session, user: User, text: str, refine: bool,
                           on_event=None) -> dict:
    """One chat turn, start to finish: route it (plan edit or question), run the engine,
    store what it produced (the pending proposal; ``report_logs`` is written by the
    engines themselves). Returns what the live page needs to finish the turn:
    ``{"reply": <the bot bubble's text>, "card": <the proposal card's HTML>}``.

    ``refine`` (ST-23) comes from the input inside the pending-proposal card: the message
    is then a follow-up **to that proposal** — a question about it or a correction —
    rather than a message routed by the plan-edit/ask heuristic. Keeping it an explicit
    field (not "pending exists ⇒ everything is a follow-up") means the main composer still
    answers an unrelated «як мій сон?» while a proposal waits."""
    pending = await repository.get_pending_plan_edit(session, user.id) if refine else None
    if not (pending or _looks_like_plan_edit(text)):
        creds = load_credentials(user)
        reply = await run_ask(session, text, user_id=user.id, api_key=creds.anthropic_key,
                              **({"on_event": on_event} if on_event else {}))
        return {"reply": reply}

    if on_event:
        on_event("status", {"text": "думаю над змінами в плані…"})
    # run_plan_edit reads the plan's strength templates off Garmin, so it needs a bound
    # per-user provider — exactly like the bot's /plan <text>. Without it get_provider()
    # fell through to the legacy .env single-user provider and every template fetch died
    # with `KeyError: 'GARMIN_EMAIL'` (visible as a GARMIN ERR line), leaving the model to
    # propose edits with no exercises in front of it.
    async with _plan_edit_runtime(session, user) as edit_creds:
        _plan, edit = await run_plan_edit(
            session, user_id=user.id, instruction=text,
            api_key=edit_creds.anthropic_key, pending=pending,
        )
    # A weekly-schedule change («3 пробіжки замість 2») is a rebuild of the rest of the
    # plan, not operations — its ✅ regenerates (see chat_confirm).
    schedule = schedule_proposal(_plan, edit.schedule)
    # NF-34: a trip mentioned in passing rides with the proposal and is written on the
    # same confirmation (or dropped with it on cancel).
    away = away_mod.from_op(edit.away) or (pending or {}).get("away")
    if schedule or edit.operations or away:
        ops = [] if schedule else [op.model_dump() for op in edit.operations]
        alt = [] if schedule else [op.model_dump() for op in (edit.alt_operations or [])]
        await repository.set_pending_plan_edit(
            session, user.id, ops, alt,
            summary=edit.summary,
            alt_summary=None if schedule else edit.alt_summary, risky=edit.risky,
            instruction=(pending or {}).get("instruction") or text,
            thread=repository.append_thread(pending, text, edit.answer) if pending
            else [],
            away=away, schedule=schedule,
        )
        reply = edit.summary
    elif pending:
        # a question about the proposal — it stays exactly as it was, only the dialogue
        # thread grows (so the next follow-up keeps the context).
        await repository.set_pending_plan_edit(
            session, user.id, pending.get("ops") or [], pending.get("alt") or [],
            summary=pending.get("summary"), alt_summary=pending.get("alt_summary"),
            risky=bool(pending.get("risky")),
            instruction=pending.get("instruction"),
            thread=repository.append_thread(pending, text, edit.answer or edit.summary),
            message=pending.get("message"),
            away=pending.get("away"),
            schedule=pending.get("schedule"),
        )
        reply = edit.answer or edit.summary
    else:
        reply = edit.summary
    return {"reply": reply,
            "card": _card_html(await repository.get_pending_plan_edit(session, user.id))}


def _pending_view(pending):
    """The pending blob plus what only the page needs (the schedule's confirmation
    lines) — one place, for the page and the live card alike."""
    if pending and pending.get("schedule"):
        pending["schedule_lines"] = planschedule.confirmation_lines(pending["schedule"])
    return pending


def _card_html(pending) -> str:
    return templates.get_template("_chat_pending.html").render(pending=_pending_view(pending))


async def _chat_job(job, user_id: int, text: str, refine: bool) -> None:
    """The background half of ``POST /chat``: its own DB session (the request's is gone
    by now), the same ``_process_message`` the turn always ran, progress into ``job``."""
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if user is None:
            return
        result = await _process_message(session, user, text, refine,
                                        on_event=job.emit_threadsafe)
    await livejobs.flush()   # progress queued from the worker thread lands first
    job.emit("done", result)


def _wants_json(request: Request) -> bool:
    return "application/json" in (request.headers.get("accept") or "")


def _refused(request: Request, msg: str, status: int):
    if _wants_json(request):
        return JSONResponse({"error": msg}, status_code=status)
    return RedirectResponse(f"/chat?err={quote(msg)}", status_code=303)


@router.post("/chat", response_class=HTMLResponse)
async def chat_send(
    request: Request,
    message: str = Form(...),
    refine: str = Form(""),
    user: User = Depends(current_user),
):
    """Start answering a chat message and return at once — the answer arrives over
    ``/live/{id}/events`` (``app.livejobs``). The page's script asks for JSON and gets the
    job id; a plain form post (no JavaScript) is redirected back to /chat, which shows the
    message as in progress and refreshes until it's answered. Either way nothing holds a
    request open for the minute a Claude call can take.

    One message at a time per account: a second send while one is still being answered
    is refused, never queued — each one is a paid call."""
    text = message.strip()
    if not text:
        if _wants_json(request):
            return JSONResponse({"error": "Порожнє повідомлення."}, status_code=400)
        return RedirectResponse("/chat", status_code=303)
    if user.is_demo:
        return _refused(request, DEMO_DISABLED_MSG, 403)
    if livejobs.running(user.id, "chat"):
        return _refused(request, CHAT_BUSY_MSG, 409)
    job = livejobs.start(
        user.id, "chat", lambda j: _chat_job(j, user.id, text, bool(refine)),
        meta={"text": text},
    )
    if _wants_json(request):
        return JSONResponse({"job": job.id, "events": f"/live/{job.id}/events"})
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
    peek = await repository.get_pending_plan_edit(session, user.id)
    if action == "apply" and (peek or {}).get("schedule") \
            and await plan_router.generation_running(session, user.id):
        # Leave the proposal where it is: a ✅ while the plan is already being generated
        # would otherwise be consumed and silently do nothing.
        return RedirectResponse(f"/chat?err={quote(REBUILD_BUSY_MSG)}", status_code=303)
    pending = await repository.pop_pending_plan_edit(session, user.id)
    if action != "cancel" and pending:
        # NF-34: a trip declared inside the edit is written on the same confirmation as the
        # plan changes — through the same helper the bot's confirm uses, so the two paths
        # cannot disagree about whether it was recorded.
        await away_db.apply_pending(session, user.id, pending)
        if pending.get("schedule"):
            # A schedule change: regenerate the rest of the plan — a slow Opus call, so in
            # the background, with /plan's waiting page in front of it (as a generation).
            if action == "apply" and not user.is_demo:
                await plan_router.spawn_plan_rebuild(session, user.id, pending["schedule"])
                return RedirectResponse("/plan", status_code=303)
            return RedirectResponse("/chat", status_code=303)
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
