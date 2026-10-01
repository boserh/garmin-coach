"""Live chat: a message runs as a background job (``app.livejobs``) and its answer streams
to the page over ``/live/{id}/events`` (SSE) instead of holding the POST open.

Covers the job registry itself, the streaming branch of ``_complete_tools``, the /ask
agent's progress events, and the web endpoints end to end. Every Claude call is faked —
the suite spends $0.
"""
import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from fastapi.testclient import TestClient

from app import livejobs
from app.analysis import client as client_mod
from app.analysis import reports
from app.analysis.client import AnalystError, CallStats
from app.core.crypto import hash_password
from app.db import users
from app.db.base import async_session_maker
from app.main import create_app
from app.routers import chat as chat_router
from tests.web_helpers import wait_live_jobs

# ---------- the job registry ----------


async def _collect(job, after=-1):
    out = []
    async for item in livejobs.follow(job, after):
        if item is not None:
            out.append(item)
    return out


async def test_a_job_streams_its_events_and_ends_with_done():
    async def run(job):
        job.emit("status", {"text": "дивлюсь"})
        await asyncio.sleep(0)
        job.emit("delta", {"text": "При"})
        job.emit("delta", {"text": "віт"})

    job = livejobs.start(1, "chat", run)
    events = await _collect(job)
    assert [e[1] for e in events] == ["status", "delta", "delta", "done"]
    assert job.done and job.text() == "Привіт" and job.status() == "дивлюсь"


async def test_a_late_subscriber_gets_the_backlog_and_can_resume():
    gate = asyncio.Event()

    async def run(job):
        job.emit("delta", {"text": "a"})
        job.emit("delta", {"text": "b"})
        await gate.wait()
        job.emit("delta", {"text": "c"})

    job = livejobs.start(1, "chat", run)
    await asyncio.sleep(0.01)
    task = asyncio.create_task(_collect(job, after=0))   # already has event 0
    await asyncio.sleep(0.01)
    gate.set()
    events = await task
    assert [d.get("text") for _, n, d in events if n == "delta"] == ["b", "c"]


async def test_reset_drops_the_preamble_of_a_tool_round():
    async def run(job):
        job.emit("delta", {"text": "Подивлюсь…"})
        job.emit("reset", {})
        job.emit("delta", {"text": "Відповідь"})

    job = livejobs.start(1, "chat", run)
    await _collect(job)
    assert job.text() == "Відповідь"


async def test_an_analyst_error_is_a_failed_event_with_its_message():
    async def run(job):
        raise AnalystError("Немає плану.")

    job = livejobs.start(1, "chat", run)
    events = await _collect(job)
    assert events[-1][1:] == ("failed", {"message": "Немає плану."})
    assert job.error() == "Немає плану."


async def test_a_crash_is_a_generic_failure_not_a_traceback():
    async def run(job):
        raise RuntimeError("secret internals")

    job = livejobs.start(1, "chat", run)
    events = await _collect(job)
    assert events[-1][2]["message"] == livejobs.GENERIC_ERROR


async def test_jobs_are_user_scoped_and_one_at_a_time_per_kind():
    gate = asyncio.Event()

    async def run(job):
        await gate.wait()

    job = livejobs.start(1, "chat", run)
    assert livejobs.get(job.id, 1) is job
    assert livejobs.get(job.id, 2) is None          # someone else's id is "no such job"
    assert livejobs.running(1, "chat") is job
    assert livejobs.running(2, "chat") is None
    gate.set()
    await _collect(job)
    assert livejobs.running(1, "chat") is None


async def test_a_worker_thread_can_emit_and_order_is_kept():
    async def run(job):
        loop = asyncio.get_running_loop()

        def work():
            for ch in "abc":
                job.emit_threadsafe("delta", {"text": ch})
        await loop.run_in_executor(None, work)

    job = livejobs.start(1, "chat", run)
    await _collect(job)
    assert job.text() == "abc"
    assert job.events[-1][0] == "done"               # done never overtakes a late delta


# ---------- streaming one /ask round ----------

class _FakeStream:
    def __init__(self, chunks, final):
        self.text_stream = iter(chunks)
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._final


def test_complete_tools_streams_text_and_returns_the_same_message(monkeypatch):
    final = SimpleNamespace(stop_reason="end_turn", content=[],
                            usage=SimpleNamespace(input_tokens=100, output_tokens=20))
    calls = {}

    def stream(**kwargs):
        calls["kwargs"] = kwargs
        return _FakeStream(["При", "віт"], final)

    fake = SimpleNamespace(messages=SimpleNamespace(
        stream=stream, create=lambda **k: pytest.fail("streamed call must not create")))
    monkeypatch.setattr(client_mod, "_get_client", lambda key=None: fake)
    got = []
    msg, stats = client_mod._complete_tools(
        "claude-sonnet-5", "sys", [{"role": "user", "content": "?"}], [], "k", 500,
        got.append)
    assert got == ["При", "віт"]
    assert msg is final
    assert stats.input_tokens == 100 and stats.output_tokens == 20 and stats.cost_usd > 0
    assert calls["kwargs"]["thinking"] == {"type": "disabled"}   # same request as before


def test_complete_tools_without_a_callback_does_not_stream(monkeypatch):
    final = SimpleNamespace(stop_reason="end_turn", content=[], usage=None)
    fake = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **k: final, stream=lambda **k: pytest.fail("must not stream")))
    monkeypatch.setattr(client_mod, "_get_client", lambda key=None: fake)
    msg, _ = client_mod._complete_tools("claude-sonnet-5", "sys", [], [], "k", 500)
    assert msg is final


# ---------- the /ask agent's progress ----------

def _text_block(t):
    return SimpleNamespace(type="text", text=t)


async def test_ask_agent_reports_tool_rounds_and_streams_the_answer(session, monkeypatch):
    rounds = iter([
        (SimpleNamespace(stop_reason="tool_use", content=[
            _text_block("Подивлюсь…"),
            SimpleNamespace(type="tool_use", id="t1", name="query_daily", input={})]),
         "Подивлюсь…"),
        (SimpleNamespace(stop_reason="end_turn", content=[_text_block("Сон добрий.")]),
         "Сон добрий."),
    ])

    def fake(model, system, messages, tools, api_key, max_tokens, on_text=None):
        msg, text = next(rounds)
        for ch in text.split(" "):
            on_text(ch)
        return msg, CallStats(kind="ask", model=model)

    monkeypatch.setattr(reports, "_complete_tools", fake)
    events = []
    text, _stats, n = await reports.run_ask_agent(
        session, 1, "як сон?", [], [], None, on_event=lambda n, d: events.append((n, d)))
    assert text == "Сон добрий." and n == 2
    names = [e[0] for e in events]
    assert names.index("reset") < names.index("status")
    assert ("status", {"text": "дивлюсь сон і відновлення…"}) in events
    assert events[-1][0] == "delta"


# ---------- web ----------

@pytest.fixture
def web(request):
    email = f"{request.node.name}@example.com"
    with TestClient(create_app()) as c:
        async def seed():
            async with async_session_maker() as s:
                u = await users.get_by_email(s, email) or await users.create_user(
                    s, email=email, password_hash=hash_password("pw"), is_admin=False)
                return u.id
        uid = anyio.run(seed)
        assert c.post("/login", data={"email": email, "password": "pw"}).status_code == 200
        yield c, uid


def _sse(client, url):
    """Read a whole event stream: [(event, data)]."""
    out, name = [], None
    with client.stream("GET", url) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: ") and name:
                out.append((name, json.loads(line[6:])))
                if name in livejobs.TERMINAL:
                    break
    return out


def _streaming_ask(*chunks, reply=None):
    async def fake(session, question, *, user_id=None, api_key=None, on_event=None,
                   on_plan_change=None):
        for ch in chunks:
            on_event("delta", {"text": ch})
            await asyncio.sleep(0)
        return reply if reply is not None else "".join(chunks)
    return fake


def test_send_returns_at_once_and_the_answer_streams(web):
    client, _uid = web
    with patch.object(chat_router, "run_ask", _streaming_ask("Сон ", "добрий.")):
        r = client.post("/chat", data={"message": "як мій сон?"},
                        headers={"Accept": "application/json"})
        assert r.status_code == 200
        job = r.json()["job"]
        assert r.json()["events"] == f"/live/{job}/events"
        events = _sse(client, f"/live/{job}/events")
    deltas = "".join(d["text"] for n, d in events if n == "delta")
    assert deltas == "Сон добрий."
    assert events[-1] == ("done", {"reply": "Сон добрий."})


def test_a_resumed_stream_skips_what_the_page_already_shows(web):
    client, _uid = web
    with patch.object(chat_router, "run_ask", _streaming_ask("a", "b", "c")):
        job = client.post("/chat", data={"message": "?"},
                          headers={"Accept": "application/json"}).json()["job"]
        wait_live_jobs()
        events = _sse(client, f"/live/{job}/events?after=0")   # the page shows "a"
    assert [d.get("text") for n, d in events if n == "delta"] == ["b", "c"]


def test_someone_elses_stream_is_a_404(web):
    client, uid = web

    async def run(job):
        return None

    async def start():
        return livejobs.start(uid + 1000, "chat", run)
    other = client.portal.call(start)
    assert client.get(f"/live/{other.id}/events").status_code == 404
    assert client.get("/live/nope/events").status_code == 404


def test_a_second_message_while_one_is_answered_is_refused(web):
    client, _uid = web
    release = threading.Event()

    async def slow(session, question, *, user_id=None, api_key=None, on_event=None,
                   on_plan_change=None):
        while not release.is_set():
            await asyncio.sleep(0.01)
        return "ok"

    with patch.object(chat_router, "run_ask", slow):
        first = client.post("/chat", data={"message": "раз"},
                            headers={"Accept": "application/json"})
        second = client.post("/chat", data={"message": "два"},
                             headers={"Accept": "application/json"})
        assert first.status_code == 200
        assert second.status_code == 409 and second.json()["error"] == chat_router.CHAT_BUSY_MSG

        # The page shows the message being answered, ready for the script to re-attach,
        # and refreshes itself only when there is no script to do it.
        page = client.get("/chat").text
        assert 'data-live-job="' + first.json()["job"] + '"' in page
        assert "раз" in page
        assert '<noscript><meta http-equiv="refresh"' in page
        release.set()
        wait_live_jobs()
    assert "data-live-job" not in client.get("/chat").text


def test_a_plan_edit_finishes_with_the_proposal_card(web):
    from app.garmin.schemas import PlanEdit, PlanOp

    client, _uid = web
    edit = PlanEdit(summary="Переніс довгу на суботу.",
                    operations=[PlanOp(action="move", date="2026-07-01", to_date="2026-07-04")])
    with patch.object(chat_router, "run_plan_edit", AsyncMock(return_value=(object(), edit))):
        job = client.post("/chat", data={"message": "перенеси довгу на суботу"},
                          headers={"Accept": "application/json"}).json()["job"]
        events = _sse(client, f"/live/{job}/events")
    assert events[0] == ("status", {"text": "думаю над змінами в плані…"})
    name, done = events[-1]
    assert name == "done" and done["reply"] == "Переніс довгу на суботу."
    assert "Пропозиція для плану" in done["card"] and 'value="apply"' in done["card"]


def test_a_failure_arrives_as_failed_with_the_message(web):
    client, _uid = web
    with patch.object(chat_router, "run_ask", AsyncMock(side_effect=AnalystError("Ліміт."))):
        job = client.post("/chat", data={"message": "?"},
                          headers={"Accept": "application/json"}).json()["job"]
        events = _sse(client, f"/live/{job}/events")
    assert events[-1] == ("failed", {"message": "Ліміт."})
    # shown live, so the page doesn't repeat it on the next load
    assert "Ліміт." not in client.get("/chat").text


def test_the_live_feed_is_never_cached_and_never_buffered(web):
    client, _uid = web
    with patch.object(chat_router, "run_ask", _streaming_ask("x")):
        job = client.post("/chat", data={"message": "?"},
                          headers={"Accept": "application/json"}).json()["job"]
        with client.stream("GET", f"/live/{job}/events") as r:
            assert r.headers["cache-control"] == "no-store"
            assert r.headers["x-accel-buffering"] == "no"
            for _ in r.iter_lines():
                pass


def test_the_service_worker_leaves_the_live_feed_alone():
    from pathlib import Path

    sw = (Path(__file__).resolve().parent.parent / "app" / "static" / "sw.js").read_text()
    head = sw[sw.index("self.addEventListener('fetch'"):]
    # the bypass must come before any respondWith in the handler
    assert head.index("'/live/'") < head.index("respondWith")


def test_wait_helper_times_out_loudly():
    async def never(job):
        await asyncio.sleep(3600)

    async def start():
        return livejobs.start(1, "chat", never)
    with TestClient(create_app()) as c:
        job = c.portal.call(start)
        t0 = time.monotonic()
        with pytest.raises(AssertionError):
            wait_live_jobs(timeout=0.05)
        assert time.monotonic() - t0 < 1
        c.portal.call(lambda: _cancel(job))


async def _cancel(job):
    job.task.cancel()
