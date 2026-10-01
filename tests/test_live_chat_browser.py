"""The live chat in a real browser: a message is sent without leaving the page, the answer
streams into the thread, a reload mid-answer picks the stream back up, and the page still
works with JavaScript off.

Opt-in like the other browser guards (needs playwright + a Chromium binary) and, like
``test_pwa_offline``, a real uvicorn on loopback — the service worker registers on these
pages, and the live feed has to get past it. The coach is a fake that streams slowly
(no Claude call), writing its answer to report_logs the way the real one does.
"""
import asyncio
import socket
import threading
import time

import pytest

from tests.browser_helpers import chromium_path
from tests.web_helpers import _seed_user

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed"
).sync_playwright

EMAIL = "livechat@example.com"
PASSWORD = "pw"
ANSWER = ["Сон ", "цього ", "тижня ", "стабільний, ", "HRV ", "у ", "нормі."]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _fake_ask(session, question, *, user_id=None, api_key=None, on_event=None):
    from app.garmin import repository

    if on_event:
        on_event("status", {"text": "дивлюсь сон і відновлення…"})
    await asyncio.sleep(0.6)
    for chunk in ANSWER:
        if on_event:
            on_event("delta", {"text": chunk})
        await asyncio.sleep(0.25)
    text = "".join(ANSWER)
    await repository.log_report(session, user_id=user_id, kind="ask",
                                model="claude-sonnet-5", ok=True, question=question,
                                report_text=text)
    return text


@pytest.fixture(scope="module")
def base_url():
    import uvicorn

    from app.main import create_app
    from app.routers import chat as chat_router

    original = chat_router.run_ask
    chat_router.run_ask = _fake_ask
    port = _free_port()
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
        chat_router.run_ask = original
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="module")
def account():
    _seed_user(email=EMAIL, password=PASSWORD, is_admin=False)
    return EMAIL


def _browser(p, *, js=True, mobile=False):
    exe = chromium_path()
    if not exe:
        pytest.skip("no chromium binary available")
    browser = p.chromium.launch(executable_path=exe)
    viewport = {"width": 390, "height": 844} if mobile else {"width": 1100, "height": 900}
    context = browser.new_context(viewport=viewport, java_script_enabled=js)
    return browser, context.new_page()


def _login(page, base_url):
    page.goto(base_url + "/login")
    page.fill('input[name="email"]', EMAIL)
    page.fill('input[name="password"]', PASSWORD)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_url(lambda u: "/login" not in u, timeout=15000)


def _composer(page):
    return page.locator('form.composer:not(.refine) textarea')


def test_a_message_is_answered_in_place_as_it_streams(base_url, account):
    with sync_playwright() as p:
        browser, page = _browser(p, mobile=True)
        try:
            _login(page, base_url)
            page.goto(base_url + "/chat")
            navigations = []
            page.on("framenavigated", lambda f: navigations.append(f.url))

            _composer(page).fill("як мій сон цього тижня?")
            page.click('form.composer:not(.refine) button[type="submit"]')

            # The question is in the thread at once, and the composer is free again.
            mine = page.locator("#chat-thread .turn.me").last
            mine.wait_for(timeout=2000)
            assert mine.inner_text() == "як мій сон цього тижня?"
            assert _composer(page).input_value() == ""
            live = page.locator("#chat-thread .turn.bot").last
            # The coach says what it's doing before it writes …
            page.wait_for_function(
                "() => (document.querySelector('.turn.lv .lv-status')||{}).textContent"
                " === 'дивлюсь сон і відновлення…'", timeout=3000)
            # … then the answer appears piece by piece, before it is finished.
            page.wait_for_function(
                "() => { const t = document.querySelector('.turn.lv .lv-txt');"
                " return t && t.textContent.length > 0; }", timeout=5000)
            partial = live.inner_text()
            assert 0 < len(partial) < len("".join(ANSWER))
            assert page.locator('form.composer:not(.refine) button').is_disabled()

            page.wait_for_function("() => !document.querySelector('.turn.lv')", timeout=10000)
            assert live.inner_text() == "".join(ANSWER)
            assert not page.locator('form.composer:not(.refine) button').is_disabled()
            assert navigations == []           # never left the page

            # And it is a real turn: a fresh load shows it from report_logs.
            page.reload()
            assert "".join(ANSWER) in page.locator("#chat-thread").inner_text()
        finally:
            browser.close()


def test_a_reload_mid_answer_picks_the_stream_back_up(base_url, account):
    with sync_playwright() as p:
        browser, page = _browser(p)
        try:
            _login(page, base_url)
            page.goto(base_url + "/chat")
            _composer(page).fill("друге питання")
            _composer(page).press("Enter")          # Enter sends on a desktop keyboard
            page.wait_for_function(
                "() => { const t = document.querySelector('.turn.lv .lv-txt');"
                " return t && t.textContent.length > 0; }", timeout=5000)

            page.reload()
            page.wait_for_function("() => !document.querySelector('.turn.lv')", timeout=10000)
            thread = page.locator("#chat-thread").inner_text()
            # the full answer once — the resumed stream didn't replay what the page showed
            assert thread.count("".join(ANSWER)) >= 1
            assert "Сон цього Сон цього" not in thread
        finally:
            browser.close()


def test_without_javascript_the_page_refreshes_until_answered(base_url, account):
    with sync_playwright() as p:
        browser, page = _browser(p, js=False)
        try:
            _login(page, base_url)
            page.goto(base_url + "/chat")
            _composer(page).fill("без скриптів")
            page.click('form.composer:not(.refine) button[type="submit"]')
            page.wait_for_url(base_url + "/chat", timeout=5000)
            # the message is shown as in progress, and the page refreshes itself …
            assert "без скриптів" in page.locator("#chat-thread").inner_text()
            # … until the answer is in the thread.
            deadline = time.time() + 15
            while "data-live-job" in page.content():
                if time.time() > deadline:
                    pytest.fail("the no-JS page never showed the answer")
                time.sleep(0.5)
            page.wait_for_load_state()
            assert "".join(ANSWER) in page.locator("#chat-thread").inner_text()
        finally:
            browser.close()
