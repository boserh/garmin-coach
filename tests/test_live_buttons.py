"""Every paid web button as a live job (``routers.live.start_button``): the request answers
at once, the result streams, and the page lands on the same URL the old synchronous
handler redirected to. Plan generation/rebuild report progress the same way.

Every Claude call, Garmin call and Telegram send is faked — the suite spends $0.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from fastapi.testclient import TestClient

import app.garmin.credentials as credentials_mod
from app import livejobs
from app.analysis import client as client_mod
from app.analysis import plans, reports
from app.analysis.client import CallStats
from app.core.crypto import hash_password
from app.db import users
from app.db.base import async_session_maker
from app.db.models import ActivityRecord
from app.garmin.schemas import GeneratedPlan, PlanWorkout
from app.main import create_app
from tests.web_helpers import wait_live_jobs

JSON = {"Accept": "application/json"}


@pytest.fixture
def web(request, monkeypatch):
    email = f"{request.node.name}@example.com"
    monkeypatch.setattr(credentials_mod, "load_credentials",
                        lambda user: SimpleNamespace(anthropic_key="test-key"))
    with TestClient(create_app()) as c:
        async def seed():
            async with async_session_maker() as s:
                u = await users.get_by_email(s, email) or await users.create_user(
                    s, email=email, password_hash=hash_password("pw"), is_admin=False)
                act = ActivityRecord(user_id=u.id, activity_id=abs(hash(email)) % 10**9,
                                     date="2026-06-21", type="running", analysis="старий")
                s.add(act)
                await s.commit()
                return u.id, act.id
        uid, row = anyio.run(seed)
        assert c.post("/login", data={"email": email, "password": "pw"}).status_code == 200
        yield c, uid, row


def _sse(client, url):
    out, name = [], None
    with client.stream("GET", url) as r:
        for line in r.iter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: ") and name:
                out.append((name, json.loads(line[6:])))
                if name in livejobs.TERMINAL:
                    break
    return out


def _streaming_analysis(*chunks):
    def fake(data, api_key=None, on_text=None):
        for ch in chunks:
            on_text(ch)
        return "".join(chunks), CallStats(kind="activity", model="m")
    return fake


# ---------- the activity buttons ----------

def test_regenerate_streams_the_new_analysis_then_lands_on_the_banner(web, monkeypatch):
    client, _uid, row = web
    monkeypatch.setattr(reports, "analyze_activity_with_stats",
                        _streaming_analysis("Темп ", "рівний."))
    r = client.post(f"/me/activities/{row}/regenerate", headers=JSON)
    assert r.status_code == 200
    events = _sse(client, r.json()["events"])
    assert "".join(d["text"] for n, d in events if n == "delta") == "Темп рівний."
    assert events[-1] == ("done", {"redirect": f"/me/activities/{row}?regen=ok"})
    page = client.get(f"/me/activities/{row}?regen=ok").text
    assert "Темп рівний." in page and "Розбір перегенеровано." in page


def test_a_refusal_never_starts_a_job_and_points_at_the_banner(web, monkeypatch):
    client, _uid, row = web
    monkeypatch.setattr(credentials_mod, "load_credentials",
                        lambda user: SimpleNamespace(anthropic_key=None))
    r = client.post(f"/me/activities/{row}/regenerate", headers=JSON)
    assert r.json() == {"redirect": f"/me/activities/{row}?regen=nokey"}
    assert livejobs._jobs == {}


def test_a_second_tap_while_it_runs_gets_the_running_job(web, monkeypatch):
    client, uid, row = web

    async def slow(job):
        await asyncio.sleep(3600)

    async def start():
        return livejobs.start(uid, f"activity:{row}", slow)
    running = client.portal.call(start)
    r = client.post(f"/me/activities/{row}/regenerate", headers=JSON)
    assert r.status_code == 409 and r.json()["job"] == running.id
    # without a script, the second tap lands on the running job's own page
    r = client.post(f"/me/activities/{row}/regenerate", follow_redirects=False)
    assert r.headers["location"] == f"/live/{running.id}"
    client.portal.call(lambda: _cancel(running))


async def _cancel(job):
    job.task.cancel()


def test_without_a_script_the_post_lands_on_a_page_that_follows_the_job(web, monkeypatch):
    client, uid, row = web
    gate = {"open": False}

    def slow_analysis(data, api_key=None, on_text=None):
        on_text("Пишу…")
        import time
        while not gate["open"]:
            time.sleep(0.01)
        return "Готово.", CallStats(kind="activity", model="m")

    monkeypatch.setattr(reports, "analyze_activity_with_stats", slow_analysis)
    r = client.post(f"/me/activities/{row}/regenerate", follow_redirects=False)
    live_url = r.headers["location"]
    assert live_url.startswith("/live/")

    import time
    deadline = time.monotonic() + 3
    while "Пишу…" not in (page := client.get(live_url).text):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert '<noscript><meta http-equiv="refresh"' in page    # refreshes without JS
    assert "data-live-follow=" in page                        # followed live with JS

    gate["open"] = True
    wait_live_jobs()
    done = client.get(live_url, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"].endswith("?regen=ok")


def test_a_crashed_job_page_says_so_with_the_way_back(web, monkeypatch):
    client, _uid, row = web

    def boom(data, api_key=None, on_text=None):
        raise RuntimeError("internals")

    monkeypatch.setattr(reports, "analyze_activity_with_stats", boom)
    r = client.post(f"/me/activities/{row}/regenerate", follow_redirects=False)
    wait_live_jobs()
    page = client.get(r.headers["location"]).text
    assert livejobs.GENERIC_ERROR in page and "internals" not in page
    assert f'href="/me/activities/{row}"' in page


def test_someone_elses_job_page_is_not_theirs(web):
    client, uid, _row = web

    async def run(job):
        return None

    async def start():
        return livejobs.start(uid + 1000, "x", run)
    other = client.portal.call(start)
    r = client.get(f"/live/{other.id}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"


# ---------- streaming one completion ----------

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


def test_complete_streams_through_the_same_request(monkeypatch):
    final = SimpleNamespace(content=[SimpleNamespace(type="text", text="ab")],
                            usage=SimpleNamespace(input_tokens=10, output_tokens=2))
    sent = {}

    def stream(**kwargs):
        sent.update(kwargs)
        return _FakeStream(["a", "b"], final)

    fake = SimpleNamespace(messages=SimpleNamespace(
        stream=stream, create=lambda **k: pytest.fail("must stream")))
    monkeypatch.setattr(client_mod, "_get_client", lambda key=None: fake)
    got = []
    text, stats = client_mod._complete("claude-sonnet-5", "sys", {"q": 1}, "checkup", "k",
                                       700, on_text=got.append)
    assert got == ["a", "b"] and text == "ab" and stats.output_tokens == 2
    assert sent["max_tokens"] == 700 and sent["thinking"] == {"type": "disabled"}


# ---------- plan generation progress ----------

def test_the_session_counter_counts_across_chunk_boundaries():
    seen = []
    on_text = plans._session_counter(lambda n, d: seen.append(d["text"]))
    for chunk in ['{"workouts": [{"da', 'te": "2026-10-02"}, {"date": "x"}, {"d', 'ate"']:
        on_text(chunk)
    assert seen[-1] == "складаю план: 3 тренувань…"


async def test_generation_reports_progress_and_streams_the_counter(session):
    events = []

    def fake_gen(context, api_key=None, model=None, on_text=None):
        on_text('[{"date": "2026-10-02"}, {"date": "2026-10-04"}]')
        return GeneratedPlan(summary="s", workouts=[
            PlanWorkout(date="2026-10-02", week=1, type="easy", dist_km=4.0,
                        description="d")]), CallStats(kind="plan", model="m")

    with patch.object(plans, "generate_plan_with_stats", side_effect=fake_gen):
        await plans.run_plan_generation(
            session, user_id=1, goal="first_5k", goal_label="5k", target_date="2026-12-01",
            start_date="2026-10-01", days_per_week=3, intensity="moderate", intake={},
            progress=lambda n, d: events.append((n, d)))
    texts = [d["text"] for n, d in events if n == "status"]
    assert texts[0] == "складаю план…"
    assert "складаю план: 2 тренувань…" in texts


def test_a_generation_job_lands_on_the_new_plan(web):
    client, uid, _row = web
    from app.routers import plan as plan_router

    with patch.object(plan_router, "_generate_plan_bg", AsyncMock(return_value=True)):
        client.portal.call(_spawn, plan_router, uid)
        job = livejobs.latest(uid, plan_router.PLAN_LIVE_KIND)
        wait_live_jobs()
    assert job.events[-1] == ("done", {"redirect": "/plan?created=1"})


async def _spawn(plan_router, uid):
    plan_router._spawn_plan_generation(uid, {})


def test_the_waiting_page_follows_the_live_job(web):
    client, uid, _row = web
    from app.garmin import repository
    from app.routers import plan as plan_router

    async def slow(job):
        job.emit("status", {"text": "складаю план: 4 тренувань…"})
        await asyncio.sleep(3600)

    async def start():
        async with async_session_maker() as s:
            import time
            await repository.set_state(s, uid, plan_router.PLAN_GEN_KEY,
                                       f"pending:{int(time.time())}")
        return livejobs.start(uid, plan_router.PLAN_LIVE_KIND, slow)
    job = client.portal.call(start)
    page = client.get("/plan").text
    assert f'data-live-follow="{job.id}"' in page
    assert "складаю план: 4 тренувань…" in page
    assert '<noscript><meta http-equiv="refresh"' in page
    client.portal.call(lambda: _cancel(job))
