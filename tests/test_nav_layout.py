"""UI-07: the navigation must not eat the page, and must not cover it either.

Assertions about layout, so they need the real CSS in a real engine. Opt-in exactly
like the other browser tests (``playwright`` + a Chromium binary)::

    ./venv/bin/python -m pytest tests/test_nav_layout.py
"""
from contextlib import contextmanager

import pytest

from tests.browser_helpers import (
    chromium_path,
    local_only,
    seed_rich_history,
    stage_assets,
    stage_pages,
)
from tests.web_helpers import _seed_user, _user_id

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed"
).sync_playwright

WIDTHS = (390, 320)


@contextmanager
def _page(path, width):
    exe = chromium_path()
    if not exe:
        pytest.skip("no chromium binary available")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe)
        try:
            page = browser.new_page(viewport={"width": width, "height": 844},
                                    has_touch=True, is_mobile=True)
            page.route("**/*", local_only([]))
            page.goto(path.as_uri())
            yield page
        finally:
            browser.close()


@pytest.fixture
def pages(client, tmp_path):
    email = "nav-layout@example.com"
    _seed_user(email=email, password="pw", is_admin=False)
    client.post("/login", data={"email": email, "password": "pw"})
    seed_rich_history(_user_id(email))
    stage_assets(tmp_path)
    return stage_pages(client, tmp_path,
                       {"dashboard": "/dashboard", "plan": "/plan", "chat": "/chat"})


@pytest.mark.parametrize("width", WIDTHS)
def test_the_top_row_is_one_line_on_a_phone(pages, width):
    """It used to wrap to two or three lines of links at 390px and push the actual page
    below the fold. On a phone it now shows only where you are."""
    with _page(pages["dashboard"], width) as page:
        h = page.evaluate(
            "() => document.querySelector('.topnav').getBoundingClientRect().height")
        assert h <= 64, f"top nav is {h}px tall at {width}px — more than one line"
        assert page.evaluate(
            "() => getComputedStyle(document.querySelector('.topnav .navhere')).display"
        ) == "block"
        # Every link is in the bar below instead — not merely hidden with no replacement.
        assert page.evaluate(
            "() => getComputedStyle(document.querySelector('.tabbar')).display") == "flex"


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("name", ["dashboard", "plan", "chat"])
def test_the_tab_bar_does_not_cover_the_content(pages, width, name):
    # /chat matters most here: its last element is the composer you type into, and the
    # page opens scrolled to the bottom.
    with _page(pages[name], width) as page:
        overlap = page.evaluate("""() => {
          const bar = document.querySelector('.tabbar').getBoundingClientRect();
          window.scrollTo(0, document.body.scrollHeight);
          const last = [...document.querySelectorAll('.wrap > *')].pop();
          return last.getBoundingClientRect().bottom - bar.top;
        }""")
        assert overlap <= 0, (
            f"the fixed tab bar overlaps the last {overlap}px of the page at {width}px — "
            "body's bottom padding has to leave room for it")


@pytest.fixture
def chat_pages(client, tmp_path):
    """A chat with enough turns to scroll — the header can only be shown to hold on when
    there is a scroll, and this page opens at the bottom of it."""
    import anyio

    from app.db.base import async_session_maker
    from app.garmin import repository

    email = "nav-chat@example.com"
    _seed_user(email=email, password="pw", is_admin=False)
    client.post("/login", data={"email": email, "password": "pw"})
    uid = _user_id(email)

    async def seed():
        async with async_session_maker() as s:
            for i in range(25):
                await repository.log_report(
                    s, user_id=uid, kind="ask", model="claude-sonnet-5", ok=True,
                    question=f"питання номер {i}",
                    report_text=f"відповідь номер {i}, достатньо довга щоб зайняти рядок")

    anyio.run(seed)
    stage_assets(tmp_path)
    return stage_pages(client, tmp_path, {"chat": "/chat"})


@pytest.mark.parametrize("width", [1280, *WIDTHS])
def test_the_chat_header_stays_pinned(chat_pages, width):
    """/chat is the one page that opens scrolled to the bottom (the newest turn), so an
    unpinned header starts off screen: the page loads with no name on it and, on a
    desktop, no links either."""
    with _page(chat_pages["chat"], width) as page:
        m = page.evaluate("""() => {
          window.scrollTo(0, document.body.scrollHeight);
          const nav = document.querySelector('.topnav');
          const r = nav.getBoundingClientRect();
          return {scrolled: window.scrollY, top: r.top, bottom: r.bottom,
                  bg: getComputedStyle(nav).backgroundColor};
        }""")
        assert m["scrolled"] > 0, "the staged chat page never scrolled — nothing to guard"
        assert m["top"] <= 1 and m["bottom"] > 0, (
            f"the header scrolled away (top {m['top']}px) at {width}px")
        # Pinned over transparent chrome, the turns scrolling underneath read through it.
        assert m["bg"] not in ("transparent", "rgba(0, 0, 0, 0)"), (
            "the pinned header has no background of its own")


def test_the_bar_is_gone_on_a_desktop_width(pages):
    with _page(pages["dashboard"], 1280) as page:
        assert page.evaluate(
            "() => getComputedStyle(document.querySelector('.tabbar')).display") == "none"
        # …and the full row is back, links and all.
        assert page.evaluate(
            "() => getComputedStyle(document.querySelector('.topnav a')).display") != "none"
        assert page.evaluate(
            "() => getComputedStyle(document.querySelector('.topnav .navhere')).display"
        ) == "none"


def test_every_section_is_reachable_by_keyboard(pages):
    """Focus styles existed only on inputs before UI-07; a nav you can tab through but
    can't see is not keyboard-navigable."""
    with _page(pages["dashboard"], 1280) as page:
        page.locator(".topnav a").first.focus()
        outline = page.evaluate(
            "() => getComputedStyle(document.activeElement).outlineWidth")
        assert outline not in ("", "0px"), "focused nav link has no visible ring"
        assert page.evaluate("() => document.activeElement.tagName") == "A"


def test_the_more_sheet_opens_without_javascript(pages):
    """"Ще" is a <details>, not a JS dropdown — the site works with scripts off."""
    exe = chromium_path()
    if not exe:
        pytest.skip("no chromium binary available")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe)
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844},
                                    java_script_enabled=False)
            page.route("**/*", local_only([]))
            page.goto(pages["dashboard"].as_uri())
            assert not page.locator(".moresheet").is_visible()
            page.locator(".tabmore > summary").click()
            assert page.locator(".moresheet").is_visible()
            assert page.locator(".moresheet a", has_text="Налаштування").count() == 1
        finally:
            browser.close()


@pytest.fixture
def admin_pages(client, tmp_path):
    """An ADMIN account: "Ще" then holds nine entries, not four. The original test used a
    plain user and so never saw the sheet run out of room."""
    email = "nav-admin@example.com"
    _seed_user(email=email, password="pw", is_admin=True)
    client.post("/login", data={"email": email, "password": "pw"})
    seed_rich_history(_user_id(email))
    stage_assets(tmp_path)
    return stage_pages(client, tmp_path, {"dashboard": "/dashboard"})


@pytest.mark.parametrize("height", [311, 500, 844])
def test_the_more_sheet_never_squashes_its_rows(admin_pages, height):
    """On a short screen the sheet hit the top of the viewport and flexbox compressed
    every row to half its height, clipping the labels to unreadable slivers. Rows must
    keep their size and the list must scroll instead."""
    exe = chromium_path()
    if not exe:
        pytest.skip("no chromium binary available")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe)
        try:
            page = browser.new_page(viewport={"width": 390, "height": height},
                                    has_touch=True, is_mobile=True)
            page.route("**/*", local_only([]))
            page.goto(admin_pages["dashboard"].as_uri())
            page.click(".tabmore > summary")
            m = page.evaluate("""() => {
              const sheet = document.querySelector('.moresheet');
              const items = [...sheet.querySelectorAll('a, button')];
              const r = sheet.getBoundingClientRect();
              return {
                count: items.length,
                minH: Math.min(...items.map(i => i.getBoundingClientRect().height)),
                top: r.top,
                fits: sheet.scrollHeight <= sheet.clientHeight,
              };
            }""")
            assert m["count"] >= 9, "an admin should see the admin sections here"
            # 0.7rem padding top and bottom plus a line of text is ~2.4rem; anything
            # under 2rem means the row was compressed and its label clipped.
            assert m["minH"] >= 32, (
                f"rows squashed to {m['minH']}px at {height}px tall — the labels clip")
            # The sheet stays on screen; a list too long for the space scrolls.
            assert m["top"] >= -1, "the sheet overflowed off the top of the viewport"
            if not m["fits"]:
                assert page.evaluate(
                    "() => getComputedStyle(document.querySelector('.moresheet')).overflowY"
                ) in ("auto", "scroll")
        finally:
            browser.close()


def test_every_more_entry_is_reachable_on_a_short_screen(admin_pages):
    """Scrolling the sheet must actually get you to the last entry — sign-out is the one
    at the bottom."""
    exe = chromium_path()
    if not exe:
        pytest.skip("no chromium binary available")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe)
        try:
            page = browser.new_page(viewport={"width": 390, "height": 311},
                                    has_touch=True, is_mobile=True)
            page.route("**/*", local_only([]))
            page.goto(admin_pages["dashboard"].as_uri())
            page.click(".tabmore > summary")
            logout = page.locator(".moresheet button.logout")
            logout.scroll_into_view_if_needed()
            assert logout.is_visible()
        finally:
            browser.close()
