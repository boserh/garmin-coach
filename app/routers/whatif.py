"""NF-29: ``GET /whatif`` — "а що, якщо…", answered by arithmetic instead of an LLM.

Four controls (sessions per week, volume, race date, an absence) and a "зараз → сценарій"
table, server-rendered from :func:`app.whatif.simulate`. A GET form on purpose: the page
changes nothing, so the scenario lives in the URL (shareable, back-button safe, and it works
under the read-only "переглянути як" session).

Same two hard rules as ``/insights``, enforced by tests: **0 Claude calls, 0 Garmin
requests**, and nothing written — a moved race date is simulated in memory, never stored.
The one way out is "Застосувати": a link that pre-fills ``/chat`` with the plan-edit request
the scenario stands for, so the existing paid, confirm-first edit path takes it from there
and nothing is spent until the athlete sends it.
"""
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import whatif
from app.core.auth import current_user
from app.core.config import settings
from app.core.tz import user_today
from app.db.models import User
from app.dependencies import get_session
from app.garmin import repository
from app.templating import create_templates

templates = create_templates()
router = APIRouter(tags=["whatif"])


@router.get("/whatif", response_class=HTMLResponse)
async def whatif_page(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    scenario = whatif.scenario_from_query(request.query_params)
    inputs = await repository.whatif_inputs(session, user.id, today=user_today(user))
    result = whatif.simulate(
        inputs, scenario,
        warn_acwr=settings.FORECAST_ACWR_WARN, high_acwr=settings.FORECAST_ACWR_HIGH,
    )
    apply_url = "/chat?" + urlencode({"draft": result["apply"]}) if result["apply"] else None
    return templates.TemplateResponse(
        request, "whatif.html",
        {"user": user, "r": result, "sc": result["scenario"], "apply_url": apply_url,
         "describe": whatif.describe(result["scenario"]),
         "opts": {"sessions": whatif.SESSIONS_OPTIONS, "volume": whatif.VOLUME_OPTIONS,
                  "shift": whatif.SHIFT_OPTIONS, "skip": whatif.SKIP_OPTIONS}},
    )
