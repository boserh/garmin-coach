"""EP-11: web chat with the same run_ask / run_plan_edit engine the bot's /ask and
/plan <text> already use — a single input box, routed to the right engine by a simple
heuristic, with HTML confirm/cancel buttons for a plan-edit proposal.

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
import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.client import AnalystError
from app.analysis.plans import run_plan_edit
from app.analysis.reports import run_ask
from app.core.auth import current_user
from app.db.models import User
from app.dependencies import get_session
from app.garmin import plan_sync, repository
from app.garmin.credentials import load_credentials
from app.garmin.runtime import user_runtime
from app.garmin.schemas import PlanOp

logger = logging.getLogger("api")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["chat"])

CHAT_HISTORY_N = 30

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


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    history = await repository.get_chat_history(session, user.id, n=CHAT_HISTORY_N)
    pending = await repository.get_pending_plan_edit(session, user.id)
    return templates.TemplateResponse(
        request, "chat.html",
        {"user": user, "history": history, "pending": pending,
         "error": request.query_params.get("err")},
    )


@router.post("/chat", response_class=HTMLResponse)
async def chat_send(
    message: str = Form(...),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    text = message.strip()
    if not text:
        return RedirectResponse("/chat", status_code=303)
    creds = load_credentials(user)
    try:
        if _looks_like_plan_edit(text):
            _plan, edit = await run_plan_edit(
                session, user_id=user.id, instruction=text, api_key=creds.anthropic_key,
            )
            if edit.operations:
                ops = [op.model_dump() for op in edit.operations]
                alt = [op.model_dump() for op in (edit.alt_operations or [])]
                await repository.set_pending_plan_edit(
                    session, user.id, ops, alt,
                    summary=edit.summary, alt_summary=edit.alt_summary, risky=edit.risky,
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
    action: str = Form(...),
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Confirm/reject a pending free-text plan edit. Mirrors ``bot.handlers.plan_callback``
    almost exactly — same pending-state helper, same apply + best-effort Garmin resync."""
    pending = await repository.pop_pending_plan_edit(session, user.id)
    if action != "cancel" and pending:
        ops_data = pending.get("alt" if action == "apply_alt" else "ops")
        if ops_data:
            plan_obj = await repository.get_active_plan(session, user.id)
            if plan_obj is not None:
                affected = await repository.apply_plan_ops(
                    session, plan_obj, [PlanOp(**o) for o in ops_data]
                )
                if user.garmin_sync_enabled:
                    try:
                        async with user_runtime(session, user):
                            await plan_sync.resync_workouts(session, user.id, affected)
                    except Exception:
                        logger.exception(f"CHAT plan edit sync failed user={user.id}")
    return RedirectResponse("/chat", status_code=303)
