"""A paid button in a real browser: «Перегенерувати розбір» streams the new analysis into
the page (the old one hidden meanwhile), then the page lands on its usual banner — and
the same button without JavaScript still gets there through /live/{id}.

Opt-in like the other browser guards. Real uvicorn on loopback; the analysis is a fake
that streams slowly (no Claude call).
"""
import socket
import threading
import time
from types import SimpleNamespace

import anyio
import pytest

from app.analysis.client import CallStats
from tests.browser_helpers import chromium_path
from tests.web_helpers import _seed_user, _user_id

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed"
).sync_playwright

EMAIL = "livebutton@example.com"
PASSWORD = "pw"
CHUNKS = ["Темп ", "рівний ", "від ", "початку ", "до ", "кінця, ", "пульс ", "стабільний."]


def _slow_analysis(data, api_key=None, on_text=None):
    for ch in CHUNKS:
        if on_text:
            on_text(ch)
        time.sleep(0.25)
    return "".join(CHUNKS), CallStats(kind="activity", model="m")


@pytest.fixture(scope="module")
def base_url():
    import uvicorn

    import app.garmin.credentials as credentials_mod
    from app.analysis import reports
    from app.main import create_app

    saved = (reports.analyze_activity_with_stats, credentials_mod.load_credentials)
    reports.analyze_activity_with_stats = _slow_analysis
    credentials_mod.load_credentials = lambda user: SimpleNamespace(anthropic_key="k")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not getattr(server, "started", False):
        if time.time() > deadline:
            pytest.skip("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        reports.analyze_activity_with_stats, credentials_mod.load_credentials = saved
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="module")
def activity_row():
    from app.db.base import async_session_maker
    from app.db.models import ActivityRecord

    _seed_user(email=EMAIL, password=PASSWORD, is_admin=False)
    uid = _user_id(EMAIL)

    async def seed():
        async with async_session_maker() as s:
            from app.db.models import User
            user = await s.get(User, uid)
            # The page only offers the button with a stored key; its value is never used
            # (load_credentials is faked above), so any non-empty blob will do.
            user.anthropic_key_enc = "stored"
            act = ActivityRecord(user_id=uid, activity_id=987654321, date="2026-09-28",
                                 type="running", analysis="Старий розбір.")
            s.add(act)
            await s.commit()
            return act.id
    return anyio.run(seed)


def _page(p, js=True):
    exe = chromium_path()
    if not exe:
        pytest.skip("no chromium binary available")
    browser = p.chromium.launch(executable_path=exe)
    page = browser.new_context(viewport={"width": 390, "height": 844},
                               java_script_enabled=js).new_page()
    return browser, page


def _login(page, base_url):
    page.goto(base_url + "/login")
    page.fill('input[name="email"]', EMAIL)
    page.fill('input[name="password"]', PASSWORD)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_url(lambda u: "/login" not in u, timeout=15000)


def test_regenerate_streams_in_place_then_shows_the_banner(base_url, activity_row):
    with sync_playwright() as p:
        browser, page = _page(p)
        try:
            _login(page, base_url)
            page.goto(f"{base_url}/me/activities/{activity_row}")
            page.click('form[action$="/regenerate"] button')
            # the button says what's happening, the old analysis steps aside …
            assert page.locator('form[action$="/regenerate"] button').inner_text() == "Пишу розбір…"
            page.wait_for_function(
                "() => { const t = document.querySelector('#analysis-live .lv-txt');"
                " return t && t.textContent.length > 0; }", timeout=5000)
            assert page.locator("#analysis").is_hidden()
            partial = page.locator("#analysis-live .lv-txt").inner_text()
            assert 0 < len(partial) < len("".join(CHUNKS))
            # … and once it's written the page lands on the usual banner with the new text
            page.wait_for_url(lambda u: "regen=ok" in u, timeout=10000)
            body = page.locator("body").inner_text()
            assert "Розбір перегенеровано." in body and "".join(CHUNKS) in body
        finally:
            browser.close()


def test_without_javascript_it_gets_there_through_the_live_page(base_url, activity_row):
    import app.routers.me as me_router
    me_router._regen_guard.clear()      # the cool-down from the previous test
    with sync_playwright() as p:
        browser, page = _page(p, js=False)
        try:
            _login(page, base_url)
            page.goto(f"{base_url}/me/activities/{activity_row}")
            page.click('form[action$="/regenerate"] button')
            page.wait_for_url(lambda u: "/live/" in u, timeout=5000)
            # The meta refresh moves on by itself (page.url lags behind a refresh-driven
            # navigation, so read what's on the page instead).
            deadline = time.time() + 15
            while "Розбір перегенеровано." not in page.content():
                if time.time() > deadline:
                    pytest.fail("the no-JS live page never moved on")
                time.sleep(0.5)
            assert "".join(CHUNKS) in page.locator("body").inner_text()
        finally:
            browser.close()


def test_the_live_page_with_javascript_follows_and_moves_on(base_url, activity_row):
    """/live/{id} is also where a script-enabled page lands if its fetch failed (and the
    plan's waiting page uses the same follower): it streams, then moves on at once."""
    import app.routers.me as me_router
    me_router._regen_guard.clear()
    with sync_playwright() as p:
        browser, page = _page(p)
        try:
            _login(page, base_url)
            page.goto(f"{base_url}/me/activities/{activity_row}")
            # a native submit skips the page's own handler — the plain-post path
            page.evaluate("document.querySelector('form[action$=\"/regenerate\"]').submit()")
            page.wait_for_url(lambda u: "/live/" in u, timeout=5000)
            page.wait_for_function(
                "() => { const t = document.querySelector('[data-live-follow] .lv-txt');"
                " return t && t.textContent.length > 0; }", timeout=5000)
            page.wait_for_url(lambda u: "regen=ok" in u, timeout=10000)
            assert "Розбір перегенеровано." in page.locator("body").inner_text()
        finally:
            browser.close()
