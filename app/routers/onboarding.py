"""``GET /onboarding`` — the "what do I do now?" page a fresh account lands on.

One checklist, live status per step, and the action for each step right next to it.
Everything it shows is derived: ``app.onboarding`` decides what's done from flags on the
user, this router only maps the ``User`` row (and "is there a plan?") onto those flags.
Pure DB read — no Garmin call, no Claude call.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import onboarding
from app.core.auth import current_user
from app.core.config import settings
from app.core.tglink import bot_link, deep_link
from app.db.models import User
from app.dependencies import get_session
from app.garmin import repository
from app.templating import create_templates

templates = create_templates()

router = APIRouter(tags=["onboarding"])


async def steps_for(session: AsyncSession, user: User) -> list[dict]:
    """This user's checklist. Shared with the routers that only need the counts (the
    dashboard banner) so "done" can never mean two different things on two pages."""
    plan = await repository.get_active_plan(session, user.id)
    return onboarding.build_steps(
        has_garmin=user.has_garmin_setup,
        garmin_connected=bool(user.garth_token_enc),
        garmin_invalid=user.garmin_creds_invalid,
        has_anthropic=bool(user.anthropic_key_enc),
        has_telegram=user.telegram_chat_id is not None,
        has_plan=plan is not None,
        telegram_link=deep_link(user.id),
    )


@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    if user.is_demo:
        # The walkthrough account has no credentials to enter and no way to enter them
        # (every write path short-circuits) — a checklist it can never finish is worse
        # than not offering the page at all.
        return RedirectResponse("/dashboard", status_code=303)
    steps = await steps_for(session, user)
    done, total = onboarding.progress(steps)
    return templates.TemplateResponse(
        request, "onboarding.html",
        {
            "user": user,
            "steps": steps,
            "done": done,
            "total": total,
            "complete": onboarding.is_complete(steps),
            "next_step": onboarding.next_step(steps),
            "bot_url": bot_link(),
            "bot_username": settings.TELEGRAM_BOT_USERNAME,
        },
    )
