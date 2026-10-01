"""``GET /live/{id}/events`` — the live feed of one background job (``app.livejobs``) as
Server-Sent Events.

SSE rather than a WebSocket: the page only ever listens, so a one-way stream over plain
HTTP is all it needs. It passes the Cloudflare proxy in front of the app unchanged, the
browser's ``EventSource`` reconnects by itself (sending ``Last-Event-ID``, which resumes
the feed instead of replaying it), and it needs no new dependency.

The feed is user-scoped (another account's job id is a 404) and ends with the job's
terminal ``done``/``failed`` event. A job already pruned from the registry is a 404 too —
the page then simply reloads, and the result is wherever the job wrote it.
"""
import json
from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from app import livejobs
from app.core.auth import current_user
from app.db.base import async_session_maker
from app.db.models import User
from app.templating import create_templates

router = APIRouter(tags=["live"])
templates = create_templates()

# What the page's script sends to say "give me the job id, I'll follow it myself".
JSON_ACCEPT = "application/json"
BUSY_MSG = "Це вже генерується — зачекай, поки закінчиться."


def wants_json(request: Request) -> bool:
    return JSON_ACCEPT in (request.headers.get("accept") or "")


def refuse(request: Request, redirect: str):
    """A button refused before any work started (demo, no key, a cool-down): the page it
    came from already knows how to say why — the URL carries the reason, as it always
    did. The script just navigates there."""
    if wants_json(request):
        return JSONResponse({"redirect": redirect})
    return RedirectResponse(redirect, status_code=303)


def busy(request: Request, user: User, kind: str):
    """The answer to a tap while that same button's job is still running — the running
    job (its id for the script, its page without one), never a refusal or a second paid
    call. Handlers check this FIRST: their own refusals (a cool-down) would otherwise
    send the page away from the answer being written. None when nothing is running."""
    running = livejobs.running(user.id, kind)
    if running is None:
        return None
    if wants_json(request):
        return JSONResponse({"error": BUSY_MSG, "job": running.id}, status_code=409)
    return RedirectResponse(f"/live/{running.id}", status_code=303)


def start_button(
    request: Request, user: User, kind: str, *,
    work: Callable[..., Awaitable[str]], back: str, label: str,
):
    """Run one paid button as a background job and answer at once.

    ``work(session, user, on_text)`` does what the handler used to do inline — in its own
    DB session, since the request's is gone by the time it runs — and returns the URL the
    old handler redirected to (``?regen=ok``, ``?err=analyze``, …): that is still how a
    result is shown, so every page keeps its own banners. ``on_text`` streams the model's
    text to whoever is watching.

    The page's script gets ``{"job": id}`` and follows ``/live/{id}/events``; a plain form
    post is sent to ``/live/{id}``, a page that refreshes itself until the job is done and
    then goes to the same URL. ``kind`` is one-at-a-time per user: a second tap while the
    first runs is answered with the running job, never a second paid call."""
    running = busy(request, user, kind)
    if running is not None:
        return running

    async def run(job):
        async with async_session_maker() as session:
            fresh = await session.get(User, user.id)
            url = await work(session, fresh, livejobs.text_sink(job))
        await livejobs.flush()
        job.emit("done", {"redirect": url})

    job = livejobs.start(user.id, kind, run, meta={"back": back, "label": label})
    if wants_json(request):
        return JSONResponse({"job": job.id, "events": f"/live/{job.id}/events"})
    return RedirectResponse(f"/live/{job.id}", status_code=303)


@router.get("/live/{job_id}", response_class=HTMLResponse)
async def job_page(job_id: str, request: Request, user: User = Depends(current_user)):
    """Where a button's plain form post lands (no JavaScript, or a script that lost the
    stream): the job's progress so far, refreshing itself until it's done — then off to
    the page the result belongs on. A failure is shown here, with the way back."""
    job = livejobs.get(job_id, user.id)
    if job is None:
        return RedirectResponse("/dashboard", status_code=303)
    if job.done and not job.error():
        return RedirectResponse(job.events[-1][1].get("redirect") or job.meta.get("back")
                                or "/dashboard", status_code=303)
    return templates.TemplateResponse(request, "live_wait.html", {"user": user, "job": job})


def _frame(idx: int, name: str, data: dict) -> str:
    return f"id: {idx}\nevent: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/live/{job_id}/events")
async def job_events(job_id: str, request: Request, user: User = Depends(current_user)):
    job = livejobs.get(job_id, user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")
    # Resume point: the browser's own Last-Event-ID on a reconnect; else ``?after=`` — a
    # page rendered mid-job already shows the events up to there.
    raw = request.headers.get("last-event-id") or request.query_params.get("after") or "-1"
    try:
        after = int(raw)
    except ValueError:
        after = -1

    async def stream():
        # A short reconnect delay: a dropped mobile connection should pick the answer
        # back up in seconds, not after the browser's default.
        yield "retry: 2000\n\n"
        async for item in livejobs.follow(job, after):
            if item is None:
                yield ": keepalive\n\n"
                continue
            idx, name, data = item
            if name in livejobs.TERMINAL:
                job.seen = True   # shown live — the page needn't repeat it on reload
            yield _frame(idx, name, data)

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        # no-store: a proxy or the service worker must never cache a live feed;
        # X-Accel-Buffering: an nginx in front would otherwise hold the stream back.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
