"""UI-03: the app installs, survives losing the server, and never keeps secrets offline.

The manifest shipped with EP-04 but there was no service worker at all, so the app
couldn't actually be installed and offline was a white page — on a server that lives on
a Pi in the home network, i.e. one that goes away every time you leave the house.

The feature's real risk is the mirror image of its benefit: personal pages sitting in a
cache on the device. So most of this file is about what must NOT be stored — /settings
holds credentials, and signing out has to take everything with it.

Needs a real HTTP origin (a service worker will not register over ``file://``), so it
spins up uvicorn on a loopback port. Opt-in like the other browser tests::

    ./venv/bin/python -m pytest tests/test_pwa_offline.py
"""
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from tests.browser_helpers import chromium_path, seed_rich_history
from tests.web_helpers import _seed_user, _user_id

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed"
).sync_playwright

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
EMAIL = "pwa@example.com"
PASSWORD = "pw"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url():
    """A real uvicorn on loopback. TestClient can't serve a service worker: registration
    requires an origin, and http://127.0.0.1 is a secure context."""
    import uvicorn

    from app.main import create_app

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
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def account():
    _seed_user(email=EMAIL, password=PASSWORD, is_admin=False)
    seed_rich_history(_user_id(EMAIL))
    return EMAIL


def _login(page, base_url):
    page.goto(base_url + "/login")
    page.fill('input[name="email"]', EMAIL)
    page.fill('input[name="password"]', PASSWORD)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_url(lambda u: "/login" not in u, timeout=15000)


def _wait_for_sw(page):
    page.wait_for_function(
        "() => navigator.serviceWorker && navigator.serviceWorker.controller !== null",
        timeout=20000)


def _cached_urls(page):
    return page.evaluate("""async () => {
      const names = await caches.keys();
      const out = [];
      for (const n of names) {
        const keys = await (await caches.open(n)).keys();
        keys.forEach(r => out.push(r.url));
      }
      return out;
    }""")


@pytest.fixture
def page(base_url, account):
    exe = chromium_path()
    if not exe:
        pytest.skip("no chromium binary available")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe)
        try:
            context = browser.new_context(viewport={"width": 390, "height": 844})
            pg = context.new_page()
            _login(pg, base_url)
            pg.goto(base_url + "/dashboard")
            _wait_for_sw(pg)
            pg.reload()            # first controlled load — now the worker sees the page
            yield pg
        finally:
            browser.close()


def test_the_dashboard_survives_the_server_going_away(page, base_url):
    """One online visit is enough: the Pi can then reboot, or you can walk out of the
    house, and today's readiness and plan are still there."""
    page.goto(base_url + "/dashboard")
    page.wait_for_load_state("networkidle")

    page.context.set_offline(True)
    page.goto(base_url + "/dashboard")
    assert page.locator(".wrap").count() > 0, "offline dashboard rendered nothing"
    assert "Дашборд" in page.content()


def test_the_offline_copy_admits_it_is_a_copy(page, base_url):
    """A stale readiness number is worse than no number, so the cached page says WHEN the
    data is from rather than passing for live."""
    page.goto(base_url + "/dashboard")
    page.wait_for_load_state("networkidle")

    page.context.set_offline(True)
    page.goto(base_url + "/dashboard")
    banner = page.locator(".banner--warn", has_text="збережену копію")
    assert banner.count() == 1, "no offline banner on the cached page"
    import re

    assert re.search(r"\d{2}:\d{2}", banner.inner_text()), "banner without a timestamp"


def test_an_online_visit_shows_no_offline_banner_at_all(page, base_url):
    """The regression this file exists to prevent. With stale-while-revalidate the worker
    served the saved copy whenever it had one, so every ordinary online load carried
    "Немає звʼязку із сервером — дані станом на 13:07" while the server was answering
    fine. Network-first: online means the real page, and silence."""
    page.goto(base_url + "/dashboard")
    page.wait_for_load_state("networkidle")
    page.goto(base_url + "/dashboard")          # second visit: the cache is warm now
    page.wait_for_load_state("networkidle")
    assert page.locator(".banner--warn", has_text="збережену копію").count() == 0
    assert "Немає звʼязку" not in page.content()
    # This is also the freshness assertion: the banner is injected exactly when the
    # cached copy is served, so its absence means the page came off the network.


def test_a_page_that_was_never_cached_falls_back_to_offline(page, base_url):
    page.context.set_offline(True)
    page.goto(base_url + "/me/report_logs")
    assert "Немає звʼязку" in page.content()
    # …and not a browser error page.
    assert "ERR_INTERNET_DISCONNECTED" not in page.content()


def test_settings_is_never_written_to_a_cache(page, base_url):
    """That page carries Garmin and Claude credentials — it must not exist on the device
    after the tab is closed."""
    page.goto(base_url + "/settings")
    page.wait_for_load_state("networkidle")
    urls = _cached_urls(page)
    assert not [u for u in urls if "/settings" in u], f"cached: {urls}"


@pytest.mark.parametrize("path", ["/login", "/register", "/status"])
def test_the_deny_list_holds(page, base_url, path):
    # /me/export is on the deny-list too but streams a ZIP, so navigating to it in a
    # browser starts a download instead of a page load — it's covered by the
    # source-level check at the bottom of this file.
    page.goto(base_url + path)
    page.wait_for_load_state("domcontentloaded")
    assert not [u for u in _cached_urls(page) if path in u]


def test_signing_out_empties_the_cache(page, base_url):
    """Otherwise the previous user's dashboard is one offline visit away on a shared
    phone."""
    page.goto(base_url + "/dashboard")
    page.wait_for_load_state("networkidle")
    assert any("/dashboard" in u for u in _cached_urls(page)), "nothing was cached to purge"

    # requestSubmit() rather than a click: at phone width the top row's logout is
    # hidden and the other copy lives inside the closed "Ще" sheet, but the submit event
    # app.js listens for is the same either way.
    page.evaluate(
        "() => document.querySelector('form[action=\"/logout\"]').requestSubmit()")
    page.wait_for_url(lambda u: "/login" in u, timeout=15000)

    # The purge is asynchronous (a message to the worker plus its own POST hook), and it
    # wipes EVERY cache. The stylesheet and fonts then legitimately re-warm as /login
    # loads them — those are the app's shell, not this user's data. What must be gone,
    # and stay gone, is every page.
    def personal():
        return [u for u in _cached_urls(page) if "/static/" not in u and "/offline" not in u]

    for _ in range(40):
        if not personal():
            break
        page.wait_for_timeout(100)
    assert personal() == [], "a personal page survived sign-out"


def test_a_changed_stylesheet_lands_without_clearing_site_data(page, base_url):
    """skipWaiting + clients.claim + a version in the worker's own URL: the caches from
    an older ?v= are dropped on activate, so a deploy is not a support request."""
    version = page.evaluate(
        "() => document.querySelector('meta[name=\"asset-v\"]').content")
    names = page.evaluate("() => caches.keys()")
    assert names, "no caches at all — the worker never installed"
    assert all(n.endswith(version) for n in names), (
        f"cache names {names} are not pinned to the asset version {version}")


def test_the_pages_still_work_with_javascript_off(base_url, account):
    """No worker, no registration, no install button — and nothing broken."""
    exe = chromium_path()
    if not exe:
        pytest.skip("no chromium binary available")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe)
        try:
            context = browser.new_context(java_script_enabled=False)
            pg = context.new_page()
            _login(pg, base_url)
            pg.goto(base_url + "/dashboard")
            assert pg.locator(".wrap").count() > 0
            assert "Дашборд" in pg.content()
        finally:
            browser.close()


# ---- manifest / install criteria: pure file checks, no browser needed ----

def test_the_manifest_meets_the_install_criteria():
    """Chrome won't offer an install prompt without raster icons at 192 and 512; the SVG
    alone gave a bookmark, not an app."""
    manifest = json.loads((STATIC / "manifest.json").read_text(encoding="utf-8"))
    for key in ("id", "name", "short_name", "start_url", "display", "icons"):
        assert manifest.get(key), f"manifest is missing {key}"
    assert manifest["display"] == "standalone"

    sizes = {i["sizes"] for i in manifest["icons"] if i.get("type") == "image/png"}
    assert "192x192" in sizes and "512x512" in sizes
    # Android crops to its own shape — without a maskable variant the logo gets clipped.
    assert any(i.get("purpose") == "maskable" for i in manifest["icons"])
    for icon in manifest["icons"]:
        assert (STATIC / icon["src"][len("/static/"):]).exists(), icon["src"]


def test_the_worker_never_caches_a_personal_path():
    """A source-level guard on the deny-list, so the browser test isn't the only thing
    standing between a credentials page and the device's disk."""
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    for path in ("/login", "/register", "/logout", "/settings", "/admin", "/me/export"):
        assert f"'{path}'" in sw, f"{path} dropped out of NEVER_CACHE"
    # Only these two pages are ever stored, and only as whole navigations.
    assert "var OFFLINE_PATHS = ['/dashboard', '/plan'];" in sw
