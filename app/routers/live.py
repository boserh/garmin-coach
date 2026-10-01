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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app import livejobs
from app.core.auth import current_user
from app.db.models import User

router = APIRouter(tags=["live"])


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
