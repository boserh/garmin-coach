"""``/checkups`` — the "Аналізи" tab: user-entered medical checkups/lab results.

Data entry (add/edit/delete, list, detail) plus an on-demand Claude interpretation
(``POST /checkups/{id}/analyze`` → ``run_checkup_analysis``, a real but cheap Sonnet
call — only ever triggered by an explicit button tap, never automatically). Reminders
about an upcoming ``next_due_date`` are a separate, bot-side concern
(``app.checkup_reminders`` + ``bot.jobs``), not this router.

``/checkups/supplements`` is a sibling data-entry page (active/stopped supplements) with
its own on-demand advice call (``POST /checkups/supplements/analyze`` →
``run_supplement_advice``) — which lab markers are worth tracking given what's being
taken. Its static routes are registered BEFORE ``/checkups/{checkup_id}`` so
"supplements" is never swallowed as a checkup id (same ordering trick as
``/plan/archive`` vs ``/plan/{plan_id}``)."""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketDisconnect

from app.analysis.service import (
    CHECKUP_UPLOAD_BATCH_MAX,
    AnalystError,
    parse_supplement_advice,
    run_checkup_analysis,
    run_checkup_ocr_batch,
    run_supplement_advice,
    supplement_advice_to_checkup_template,
)
from app.checkup_flags import out_of_range_severity
from app.core.auth import current_user
from app.core.tz import user_today
from app.db import checkups, supplements
from app.db.base import async_session_maker
from app.db.models import User
from app.dependencies import get_session
from app.garmin import repository
from app.garmin.credentials import load_credentials

logger = logging.getLogger("checkups")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["oor"] = out_of_range_severity  # {{ r.value|oor(r.ref) }} -> minor|major|None

router = APIRouter(tags=["checkups"])

# Anthropic's vision/document input accepts these; anything else is rejected up front
# rather than spending a Claude call that would fail anyway.
CHECKUP_UPLOAD_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "application/pdf"}
CHECKUP_UPLOAD_MAX_BYTES = 15 * 1024 * 1024  # comfortably under Anthropic's per-file limits


# ---------- upload jobs: background OCR + live status over a websocket ----------
#
# Each upload is grouped into batches of up to CHECKUP_UPLOAD_BATCH_MAX files, one
# Claude call per batch (app.analysis.reports.run_checkup_ocr_batch) rather than one
# call per file — cheaper (one system prompt instead of N) and lets Claude tell
# whether several files are pages of the SAME report or separate documents. Each batch
# becomes an in-memory job (per-process, same "TTL'd module dict" posture as the MFA
# login bridge in app.garmin.mfa) so POST /checkups/upload returns immediately instead
# of blocking on the vision call, and several batches upload concurrently. GET
# /checkups renders each job's CURRENT state (covers a hard refresh or a JS-disabled
# browser); GET /checkups/ws pushes updates as they land so an open tab doesn't have
# to poll.

@dataclass
class UploadJob:
    id: str
    user_id: int
    filenames: list
    status: str = "queued"  # queued -> processing -> done | error
    checkup_ids: list = field(default_factory=list)
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return {"job_id": self.id, "filenames": self.filenames, "status": self.status,
                "checkup_ids": self.checkup_ids, "error": self.error}


_upload_jobs: dict = {}          # job id -> UploadJob
_ws_by_user: dict = {}           # user id -> set[WebSocket]
_upload_bg_tasks: set = set()    # keeps fire-and-forget tasks from being GC'd mid-flight
UPLOAD_JOB_TTL_S = 3600  # finished jobs older than this are dropped on the next upload


def _prune_upload_jobs() -> None:
    cutoff = time.time() - UPLOAD_JOB_TTL_S
    stale = [jid for jid, job in _upload_jobs.items()
             if job.status in ("done", "error") and job.created_at < cutoff]
    for jid in stale:
        _upload_jobs.pop(jid, None)


async def _broadcast_job(job: "UploadJob") -> None:
    conns = _ws_by_user.get(job.user_id)
    if not conns:
        return
    dead = []
    for ws in conns:
        try:
            await ws.send_json(job.to_json())
        except Exception:
            dead.append(ws)
    for ws in dead:
        conns.discard(ws)


async def _process_checkup_upload_job(
    job_id: str, files: list, fallback_date: str, api_key: Optional[str],
) -> None:
    """Run one batch's OCR off the request path, in its own DB session — same shape as
    ``app.routers.plan._generate_plan_bg``. ``files`` is
    ``[(file_bytes, media_type, filename), ...]``. Never raises; the outcome lands on
    the job (read by a fresh GET /checkups) and goes out over the websocket to any
    open tab."""
    job = _upload_jobs[job_id]
    job.status = "processing"
    await _broadcast_job(job)
    async with async_session_maker() as session:
        try:
            rows = await run_checkup_ocr_batch(
                session, user_id=job.user_id, files=files,
                fallback_date=fallback_date, api_key=api_key,
            )
            job.status, job.checkup_ids = "done", [row.id for row in rows]
        except AnalystError as e:
            job.status, job.error = "error", str(e)
        except Exception:
            logger.exception(f"CHECKUP_OCR background job crashed user={job.user_id}")
            job.status, job.error = "error", "Внутрішня помилка."
    await _broadcast_job(job)


def _spawn_upload_job(
    job: "UploadJob", files: list, fallback_date: str, api_key: Optional[str],
) -> None:
    """Fire-and-forget the background OCR, keeping a reference so it isn't GC'd (same
    pattern as ``app.routers.plan._spawn_plan_generation``)."""
    task = asyncio.create_task(
        _process_checkup_upload_job(job.id, files, fallback_date, api_key))
    _upload_bg_tasks.add(task)
    task.add_done_callback(_upload_bg_tasks.discard)


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
    job_ids = [j for j in (request.query_params.get("jobs") or "").split(",") if j]
    upload_jobs = [
        _upload_jobs[jid].to_json() for jid in job_ids
        if jid in _upload_jobs and _upload_jobs[jid].user_id == user.id
    ]
    return templates.TemplateResponse(
        request, "checkups.html",
        {"user": user, "checkups": rows, "today": user_today(user).isoformat(),
         "upload_jobs": upload_jobs},
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


@router.post("/checkups/merge")
async def checkups_merge(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Merge 2+ selected checkups (checkboxes on the /checkups list) into the newest
    one — see ``app.db.checkups.merge_checkups`` for the field-conflict rule (newest
    wins on date/title/category/next_due_date; results/notes are combined). Registered
    before ``/checkups/{checkup_id}`` so "merge" is never swallowed as a checkup id
    (same ordering trick as ``/checkups/supplements``/``/checkups/upload``)."""
    form = await request.form()
    ids = [int(v) for v in form.getlist("checkup_ids") if v.isdigit()]
    if len(ids) < 2:
        return RedirectResponse("/checkups?err=mergecount", status_code=303)
    survivor = await checkups.merge_checkups(session, user.id, ids)
    if survivor is None:
        return RedirectResponse("/checkups?err=mergecount", status_code=303)
    return RedirectResponse(f"/checkups/{survivor.id}?merged=1", status_code=303)


@router.post("/checkups/upload")
async def checkups_upload(
    file: list[UploadFile] = File(...),
    user: User = Depends(current_user),
):
    """Upload one or several photos/PDFs of lab reports at once. Valid files are
    grouped into batches of up to CHECKUP_UPLOAD_BATCH_MAX and each batch becomes ONE
    background OCR job — one Claude vision call per batch (``run_checkup_ocr_batch``,
    only from this explicit upload, never automatic), not one per file: cheaper, and
    lets Claude tell whether several files are pages of the same report or separate
    documents. The request returns immediately instead of blocking on however many
    slow vision calls that takes. Redirects to /checkups?jobs=... , which shows live
    per-batch status (see the upload-jobs section above) until each lands as normal,
    editable checkup row(s) the user reviews before trusting."""
    if user.is_demo:
        return RedirectResponse("/checkups?err=demo", status_code=303)
    creds = load_credentials(user)
    if not creds.anthropic_key:
        return RedirectResponse("/checkups?err=nokey", status_code=303)
    _prune_upload_jobs()
    today = user_today(user).isoformat()
    job_ids = []

    valid = []  # (filename, file_bytes, media_type)
    for f in file:
        name = f.filename or "файл"
        content_type = (f.content_type or "").lower()
        if content_type not in CHECKUP_UPLOAD_TYPES:
            job = UploadJob(
                id=uuid.uuid4().hex[:12], user_id=user.id, filenames=[name],
                status="error", error="Непідтримуваний формат файлу.",
            )
            _upload_jobs[job.id] = job
            job_ids.append(job.id)
            continue
        data = await f.read()
        if not data or len(data) > CHECKUP_UPLOAD_MAX_BYTES:
            job = UploadJob(
                id=uuid.uuid4().hex[:12], user_id=user.id, filenames=[name],
                status="error", error="Файл завеликий (>15 МБ) або порожній.",
            )
            _upload_jobs[job.id] = job
            job_ids.append(job.id)
            continue
        valid.append((name, data, content_type))

    for i in range(0, len(valid), CHECKUP_UPLOAD_BATCH_MAX):
        chunk = valid[i:i + CHECKUP_UPLOAD_BATCH_MAX]
        job = UploadJob(
            id=uuid.uuid4().hex[:12], user_id=user.id,
            filenames=[name for name, _, _ in chunk],
        )
        _upload_jobs[job.id] = job
        job_ids.append(job.id)
        _spawn_upload_job(
            job, [(data, media_type, name) for name, data, media_type in chunk],
            today, creds.anthropic_key,
        )
    return RedirectResponse(f"/checkups?jobs={','.join(job_ids)}", status_code=303)


@router.websocket("/checkups/ws")
async def checkups_ws(websocket: WebSocket):
    """Live push for upload-job status to an open /checkups tab, so it doesn't have to
    poll while a background OCR job runs. Auth via the same signed session cookie as
    the rest of the app — Starlette's SessionMiddleware populates ``.session`` on a
    websocket scope exactly like it does on a regular ``Request``."""
    uid = websocket.session.get("user_id")
    user = None
    if uid is not None:
        async with async_session_maker() as session:
            user = await session.get(User, uid)
    if user is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    _ws_by_user.setdefault(uid, set()).add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        conns = _ws_by_user.get(uid)
        if conns is not None:
            conns.discard(websocket)
            if not conns:
                _ws_by_user.pop(uid, None)


def _parse_date_or_none(v: str) -> "str | None":
    return v.strip() or None


@router.get("/checkups/supplements", response_class=HTMLResponse)
async def supplements_list(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    rows = await supplements.list_supplements(session, user.id)
    advice = await repository.get_last_report_of_kind(session, user.id, "supplements")
    parsed = parse_supplement_advice(advice[0]) if advice else None
    template_kwargs = supplement_advice_to_checkup_template(parsed) if parsed else None
    return templates.TemplateResponse(
        request, "supplements.html",
        {
            "user": user, "supplements": rows,
            "advice": parsed,
            # a pre-existing prose report (before this JSON format shipped) fails to
            # parse — show it as-is rather than losing it, just without structured
            # items/template button.
            "advice_raw_text": advice[0] if (advice and not parsed) else None,
            "advice_has_template": template_kwargs is not None,
            "today": user_today(user).isoformat(),
        },
    )


@router.post("/checkups/supplements")
async def supplements_create(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return RedirectResponse("/checkups/supplements?err=required", status_code=303)
    await supplements.create_supplement(
        session, user.id,
        name=name,
        dosage=(form.get("dosage") or "").strip() or None,
        frequency=(form.get("frequency") or "").strip() or None,
        started_date=_parse_date_or_none(form.get("started_date") or ""),
        notes=(form.get("notes") or "").strip() or None,
    )
    return RedirectResponse("/checkups/supplements?saved=1", status_code=303)


@router.post("/checkups/supplements/analyze")
async def supplements_analyze(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """On-demand advice on which lab markers to track given the active supplement list
    (a real Sonnet call — only from this explicit button tap). Dedup-cached, so
    re-tapping an unchanged list is free unless the form carries ``force=1`` (the
    "спробуй ще раз" regenerate button shown once advice already exists)."""
    if user.is_demo:
        return RedirectResponse("/checkups/supplements?err=demo", status_code=303)
    form = await request.form()
    force = (form.get("force") or "") == "1"
    creds = load_credentials(user)
    if not creds.anthropic_key:
        return RedirectResponse("/checkups/supplements?err=nokey", status_code=303)
    try:
        text = await run_supplement_advice(
            session, user_id=user.id, api_key=creds.anthropic_key, force=force)
        await session.commit()
    except AnalystError:
        return RedirectResponse("/checkups/supplements?err=analyze", status_code=303)
    if text is None:
        return RedirectResponse("/checkups/supplements?err=none", status_code=303)
    return RedirectResponse("/checkups/supplements?analyzed=1", status_code=303)


@router.post("/checkups/supplements/apply-template")
async def supplements_apply_template(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Turn the last generated supplement advice into a ready-to-fill checkup: one empty
    result row per distinct recommended lab marker, so the user just types in the real
    values once the lab report is back (via the normal /checkups/{id} edit form — no new
    UI needed for that part). Pure DB read/write, zero Claude calls."""
    advice = await repository.get_last_report_of_kind(session, user.id, "supplements")
    parsed = parse_supplement_advice(advice[0]) if advice else None
    kwargs = supplement_advice_to_checkup_template(parsed) if parsed else None
    if kwargs is None:
        return RedirectResponse("/checkups/supplements?err=notemplate", status_code=303)
    row = await checkups.create_checkup(
        session, user.id, date=user_today(user).isoformat(), **kwargs)
    return RedirectResponse(f"/checkups/{row.id}?saved=1", status_code=303)


@router.post("/checkups/supplements/{supplement_id}")
async def supplement_update(
    supplement_id: int,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    row = await supplements.get_supplement(session, user.id, supplement_id)
    if row is None:
        return RedirectResponse("/checkups/supplements", status_code=303)
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return RedirectResponse("/checkups/supplements?err=required", status_code=303)
    await supplements.update_supplement(
        session, row,
        name=name,
        dosage=(form.get("dosage") or "").strip() or None,
        frequency=(form.get("frequency") or "").strip() or None,
        started_date=_parse_date_or_none(form.get("started_date") or ""),
        notes=(form.get("notes") or "").strip() or None,
        is_active=bool(form.get("is_active")),
    )
    return RedirectResponse("/checkups/supplements?saved=1", status_code=303)


@router.post("/checkups/supplements/{supplement_id}/delete")
async def supplement_delete(
    supplement_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    row = await supplements.get_supplement(session, user.id, supplement_id)
    if row is not None:
        await supplements.delete_supplement(session, row)
    return RedirectResponse("/checkups/supplements", status_code=303)


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
    attachments = await checkups.list_attachments(session, row.id)
    return templates.TemplateResponse(
        request, "checkup_detail.html",
        {"user": user, "c": row, "attachments": attachments},
    )


@router.get("/checkups/{checkup_id}/attachments/{attachment_id}")
async def checkup_attachment(
    checkup_id: int,
    attachment_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """Serve the original uploaded photo/PDF a checkup was parsed from, so the user
    can double-check a value Claude may have misread. Ownership is checked via the
    parent checkup (get_checkup is user-scoped); the attachment lookup is then scoped
    to that checkup id, same "can't guess another owner's id" posture as everywhere
    else in this router."""
    row = await checkups.get_checkup(session, user.id, checkup_id)
    if row is None:
        return RedirectResponse("/checkups", status_code=303)
    attachment = await checkups.get_attachment(session, row.id, attachment_id)
    if attachment is None:
        return RedirectResponse(f"/checkups/{checkup_id}", status_code=303)
    return Response(
        content=attachment.data, media_type=attachment.media_type,
        headers={"Content-Disposition": f'inline; filename="{attachment.filename}"'},
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
    if user.is_demo:
        return RedirectResponse(f"/checkups/{checkup_id}?err=demo", status_code=303)
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
