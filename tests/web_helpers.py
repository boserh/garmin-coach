"""Shared helper for the web-layer router tests (B3 split of test_routers.py).

The ``client`` / ``auth_client`` fixtures live in ``conftest.py`` so every
``test_routers_*`` module picks them up; this holds the one plain helper the
tests call directly (``_seed_user``)."""
import anyio

from app.core.crypto import hash_password
from app.db import users
from app.db.base import async_session_maker


def _seed_user(email="t@example.com", password="pw", is_admin=True):
    async def seed():
        async with async_session_maker() as s:
            if not await users.get_by_email(s, email):
                await users.create_user(
                    s, email=email, password_hash=hash_password(password), is_admin=is_admin
                )

    anyio.run(seed)


def _user_id(email):
    async def get():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, email)
            return u.id if u else None

    return anyio.run(get)


def _seed_two_users_with_data():
    """alice + bob, each with one daily metric and one report. Returns their ids."""
    from app.garmin import repository
    from app.garmin.schemas import DailySummary

    _seed_user(email="alice@example.com", password="pw", is_admin=False)
    _seed_user(email="bob@example.com", password="pw", is_admin=False)
    aid, bid = _user_id("alice@example.com"), _user_id("bob@example.com")

    async def seed():
        async with async_session_maker() as s:
            await repository.upsert_daily(
                s, aid, DailySummary(date="2026-06-20", hrv_avg=55, has_data=True))
            await repository.upsert_daily(
                s, bid, DailySummary(date="2026-06-20", hrv_avg=70, has_data=True))
            await repository.log_report(s, user_id=aid, kind="report", model="m",
                                        ok=True, report_text="alice report")
            await repository.log_report(s, user_id=bid, kind="report", model="m",
                                        ok=True, report_text="bob report")
            await s.commit()

    anyio.run(seed)
    return aid, bid


def _report_id(user_id):
    from sqlalchemy import select

    from app.db.models import ReportLog

    async def get():
        async with async_session_maker() as s:
            return (await s.execute(
                select(ReportLog.id).where(ReportLog.user_id == user_id)
                .order_by(ReportLog.id.desc()).limit(1)
            )).scalar_one()

    return anyio.run(get)


def wait_live_jobs(timeout: float = 5.0) -> None:
    """Block until every background job (``app.livejobs``) has finished. A ``POST /chat``
    returns before its answer exists — the job runs on the TestClient's event loop, in
    another thread — so a test that asserts on the answer waits here first, INSIDE any
    ``patch`` it set up (a job that outlives the patch would run the real engine)."""
    import time

    from app import livejobs

    end = time.monotonic() + timeout
    while any(not j.done for j in list(livejobs._jobs.values())):
        if time.monotonic() > end:
            raise AssertionError("a live job did not finish in time")
        time.sleep(0.01)


def post_live(client, url, follow_redirects=True, **kwargs):
    """POST a paid button that now runs as a live job (``routers.live.start_button``) and
    return what the old synchronous handler returned: the post lands on ``/live/{id}``,
    the job is waited for, and that page's redirect to the result URL is handed back
    (followed, unless ``follow_redirects=False``). Refusals that never started a job
    come back exactly as before."""
    r = client.post(url, follow_redirects=False, **kwargs)
    location = r.headers.get("location", "")
    if r.status_code == 303 and location.startswith("/live/"):
        wait_live_jobs()
        r = client.get(location, follow_redirects=False)
    if follow_redirects and r.status_code in (302, 303):
        r = client.get(r.headers["location"])
    return r
