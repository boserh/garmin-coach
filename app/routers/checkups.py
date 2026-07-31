"""``/checkups`` — the "Аналізи" tab: user-entered medical checkups/lab results.

Data entry (add/edit/delete, list, detail) plus an on-demand Claude interpretation
(``POST /checkups/{id}/analyze`` → ``run_checkup_analysis``, a real but cheap Sonnet
call — only ever triggered by an explicit button tap, never automatically). Reminders
about an upcoming ``next_due_date`` are a separate, bot-side concern
(``app.checkup_reminders`` + ``bot.jobs``), not this router."""
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.service import AnalystError, run_checkup_analysis
from app.core.auth import current_user
from app.core.tz import user_today
from app.db import checkups
from app.db.models import User
from app.dependencies import get_session
from app.garmin.credentials import load_credentials

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["checkups"])


def _parse_results(form) -> list:
    """Repeated ``result_name``/``result_value``/``result_unit``/``result_ref`` inputs
    (one row per lab value) → a compact list, dropping rows with no name."""
    names = form.getlist("result_name")
    values = form.getlist("result_value")
    units = form.getlist("result_unit")
    refs = form.getlist("result_ref")
    out = []
    for i, name in enumerate(names):
        name = (name or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "value": (values[i] if i < len(values) else "").strip(),
            "unit": (units[i] if i < len(units) else "").strip(),
            "ref_range": (refs[i] if i < len(refs) else "").strip(),
        })
    return out


@router.get("/checkups", response_class=HTMLResponse)
async def checkups_list(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    rows = await checkups.list_checkups(session, user.id)
    return templates.TemplateResponse(
        request, "checkups.html",
        {"user": user, "checkups": rows, "today": user_today(user).isoformat()},
    )


@router.post("/checkups")
async def checkups_create(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    date = (form.get("date") or "").strip()
    title = (form.get("title") or "").strip()
    if not (date and title):
        return RedirectResponse("/checkups?err=required", status_code=303)
    await checkups.create_checkup(
        session, user.id,
        date=date,
        title=title,
        category=(form.get("category") or "").strip() or None,
        results=_parse_results(form) or None,
        notes=(form.get("notes") or "").strip() or None,
        next_due_date=(form.get("next_due_date") or "").strip() or None,
    )
    return RedirectResponse("/checkups?saved=1", status_code=303)


@router.get("/checkups/{checkup_id}", response_class=HTMLResponse)
async def checkup_detail(
    checkup_id: int,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    row = await checkups.get_checkup(session, user.id, checkup_id)
    if row is None:
        return RedirectResponse("/checkups", status_code=303)
    return templates.TemplateResponse(
        request, "checkup_detail.html",
        {"user": user, "c": row},
    )


@router.post("/checkups/{checkup_id}")
async def checkup_update(
    checkup_id: int,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    row = await checkups.get_checkup(session, user.id, checkup_id)
    if row is None:
        return RedirectResponse("/checkups", status_code=303)
    form = await request.form()
    date = (form.get("date") or "").strip()
    title = (form.get("title") or "").strip()
    if not (date and title):
        return RedirectResponse(f"/checkups/{checkup_id}?err=required", status_code=303)
    await checkups.update_checkup(
        session, row,
        date=date,
        title=title,
        category=(form.get("category") or "").strip() or None,
        results=_parse_results(form) or None,
        notes=(form.get("notes") or "").strip() or None,
        next_due_date=(form.get("next_due_date") or "").strip() or None,
    )
    return RedirectResponse(f"/checkups/{checkup_id}?saved=1", status_code=303)


@router.post("/checkups/{checkup_id}/analyze")
async def checkup_analyze(
    checkup_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """On-demand Claude interpretation of one checkup's results (a real Sonnet call —
    only from this explicit button tap, never a background job). Dedup-cached, so
    re-tapping without an edit in between is free (``run_checkup_analysis``)."""
    row = await checkups.get_checkup(session, user.id, checkup_id)
    if row is None:
        return RedirectResponse("/checkups", status_code=303)
    creds = load_credentials(user)
    if not creds.anthropic_key:
        return RedirectResponse(f"/checkups/{checkup_id}?err=nokey", status_code=303)
    try:
        await run_checkup_analysis(session, row, user_id=user.id, api_key=creds.anthropic_key)
        await session.commit()
    except AnalystError:
        return RedirectResponse(f"/checkups/{checkup_id}?err=analyze", status_code=303)
    return RedirectResponse(f"/checkups/{checkup_id}?analyzed=1", status_code=303)


@router.post("/checkups/{checkup_id}/delete")
async def checkup_delete(
    checkup_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    row = await checkups.get_checkup(session, user.id, checkup_id)
    if row is not None:
        await checkups.delete_checkup(session, row)
    return RedirectResponse("/checkups", status_code=303)
