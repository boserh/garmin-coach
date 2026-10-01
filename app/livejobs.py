"""Paid web buttons as background jobs with a live event feed (SSE).

A Claude call takes from a few seconds to a minute or two. Every web button that made one
used to hold its HTTP request open for the whole of it, so the chat "sent" a message by
freezing for a minute and then reloading. Now the request only starts a ``Job`` and
returns; the job runs on the web process's own event loop and appends events (``status``
lines, streamed ``delta`` text, a final ``done`` or ``failed``) that any number of
``/live/{id}/events`` streams replay and follow.

Deliberately in-process and in-memory, like the login rate limiter and the MFA bridge:
the Pi runs ONE web process, and a job's real result is written where it always was
(``report_logs``, the pending-proposal state, an activity's ``analysis``) — the job only
carries progress. A restart mid-job loses the progress and the answer (the user asks
again); it never charges twice, because nothing here retries.

The registry is user-scoped: ``get`` never returns another account's job, so a guessed
id is a 404, not someone else's coaching.
"""
import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable, Optional

from app.analysis.client import AnalystError

logger = logging.getLogger("api")

# A finished job is kept this long so a page reload / a reconnecting stream can still
# replay how it ended, and the no-JavaScript page can show an error once.
FINISHED_TTL_S = 600
# The stream sends a comment line this often while nothing happens: Cloudflare closes a
# proxied connection that stays silent for ~100 s, and a Claude round can take longer.
KEEPALIVE_S = 15.0

TERMINAL = ("done", "failed")
GENERIC_ERROR = "Щось пішло не так — спробуй ще раз."


@dataclass
class Job:
    id: str
    user_id: int
    kind: str
    meta: dict = field(default_factory=dict)
    events: list = field(default_factory=list)       # [(name, data)], append-only
    finished_at: Optional[float] = None
    seen: bool = False                                # its end was shown on a page
    _subscribers: list = field(default_factory=list)  # asyncio.Queue per open stream
    _loop: Optional[asyncio.AbstractEventLoop] = None
    task: Optional[asyncio.Task] = None

    @property
    def done(self) -> bool:
        return self.finished_at is not None

    def emit(self, name: str, data: Optional[dict] = None) -> None:
        """Append an event and wake every open stream. Event-loop thread only — use
        ``emit_threadsafe`` from a Claude worker thread."""
        if self.done:
            return   # nothing after the terminal event; a late delta is noise
        idx = len(self.events)
        self.events.append((name, data or {}))
        if name in TERMINAL:
            self.finished_at = time.monotonic()
        for q in list(self._subscribers):
            q.put_nowait(idx)

    def emit_threadsafe(self, name: str, data: Optional[dict] = None) -> None:
        """``emit`` from any thread. Everything goes through the loop's queue, so events
        keep their order whichever thread produced them."""
        self._loop.call_soon_threadsafe(self.emit, name, data)

    def text(self) -> str:
        """The streamed text so far (since the last ``reset``) — what a page rendered
        mid-job shows in the reply bubble."""
        out: list = []
        for name, data in self.events:
            if name == "reset":
                out = []
            elif name == "delta":
                out.append(data.get("text") or "")
        return "".join(out)

    def status(self) -> Optional[str]:
        """The latest status line, or None."""
        for name, data in reversed(self.events):
            if name == "status":
                return data.get("text")
        return None

    def error(self) -> Optional[str]:
        if self.events and self.events[-1][0] == "failed":
            return self.events[-1][1].get("message") or GENERIC_ERROR
        return None


_jobs: dict = {}


def _prune() -> None:
    now = time.monotonic()
    for jid in [j.id for j in _jobs.values()
                if j.done and now - j.finished_at > FINISHED_TTL_S]:
        _jobs.pop(jid, None)


def start(user_id: int, kind: str, run: Callable[[Job], Awaitable[None]], *,
          meta: Optional[dict] = None) -> Job:
    """Start ``run(job)`` in the background and return the job at once. ``run`` emits its
    own progress and finishes with ``job.emit("done", ...)``; returning without doing so
    is a plain ``done``. An ``AnalystError`` becomes a ``failed`` event carrying its
    user-facing text; anything else is logged and becomes a generic one — never a
    traceback in the page.

    The task copies the request's context, so the demo / impersonation kill switches
    (ContextVars) still hold inside it."""
    _prune()
    job = Job(id=secrets.token_urlsafe(12), user_id=user_id, kind=kind, meta=meta or {})
    job._loop = asyncio.get_running_loop()

    async def _run():
        try:
            await run(job)
            await flush()
            job.emit("done", {})
        except AnalystError as e:
            job.emit("failed", {"message": str(e) or GENERIC_ERROR})
        except Exception:
            logger.exception(f"JOB {kind} user={user_id} crashed")
            job.emit("failed", {"message": GENERIC_ERROR})

    _jobs[job.id] = job
    job.task = asyncio.create_task(_run())
    logger.info(f"JOB {kind} user={user_id} started id={job.id}")
    return job


def text_sink(job: "Job") -> Callable[[str], None]:
    """An ``on_text`` for the Claude helpers: each streamed piece becomes a ``delta``."""
    return lambda text: job.emit_threadsafe("delta", {"text": text})


async def flush() -> None:
    """Let every ``emit_threadsafe`` already queued run before what comes next. They are
    plain ``call_soon_threadsafe`` callbacks, FIFO on the loop, so one pass of the loop
    is enough — a terminal event emitted after this can't overtake a late delta."""
    await asyncio.sleep(0)


def get(job_id: str, user_id: int) -> Optional[Job]:
    """This user's job by id, or None (also for another user's id)."""
    job = _jobs.get(job_id)
    return job if job is not None and job.user_id == user_id else None


def running(user_id: int, kind: str) -> Optional[Job]:
    """This user's unfinished job of ``kind``, if any — one at a time per kind, so a
    double submit can't pay twice."""
    for job in _jobs.values():
        if job.user_id == user_id and job.kind == kind and not job.done:
            return job
    return None


def latest(user_id: int, kind: str) -> Optional[Job]:
    """This user's most recent job of ``kind`` still in the registry."""
    found = [j for j in _jobs.values() if j.user_id == user_id and j.kind == kind]
    return found[-1] if found else None


async def follow(job: Job, after: int = -1) -> AsyncIterator[Optional[tuple]]:
    """Yield ``(index, name, data)`` for every event after ``after`` — the backlog first,
    then live ones — and stop after the terminal event. Yields ``None`` every
    ``KEEPALIVE_S`` of silence so the caller can keep the connection warm."""
    q: asyncio.Queue = asyncio.Queue()
    job._subscribers.append(q)
    try:
        sent = after
        while True:
            # Replay anything not yet sent (backlog, or events that landed while the
            # previous one was being written out).
            while sent + 1 < len(job.events):
                sent += 1
                name, data = job.events[sent]
                yield sent, name, data
                if name in TERMINAL:
                    return
            try:
                await asyncio.wait_for(q.get(), timeout=KEEPALIVE_S)
            except asyncio.TimeoutError:
                yield None
    finally:
        job._subscribers.remove(q)


def reset_for_tests() -> None:
    _jobs.clear()
