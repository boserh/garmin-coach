"""The global double-submit guard must not eat the pressed button's value.

``app.js`` disables a form's submit controls the moment it's submitted, so an impatient
second click can't fire a second (usually Claude-backed) request. But a *disabled*
control is not part of the submission — and on every multi-button form the site has, the
pressed button IS the answer: ``/chat/confirm``'s ✅/🛡/❌ and the OAuth consent screen's
Дозволити/Відхилити all carry it as the submitter's own ``name``/``value``. Locking the
form before the browser built the entry list therefore dropped ``action`` from the POST:
the chat's ✅ came back as FastAPI's raw 422 "Field required", and — worse — a consent
screen where "Відхилити" is indistinguishable from "Дозволити" defaults to granting.

Opt-in like the other browser guards (``playwright`` + a Chromium binary)::

    ./venv/bin/python -m pytest tests/test_form_submit.py
"""
import anyio
import pytest

from app.db.base import async_session_maker
from app.garmin import repository
from tests.browser_helpers import chromium_path, local_only, stage_assets, stage_pages
from tests.web_helpers import _seed_user, _user_id

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed"
).sync_playwright

# What the browser would actually send: the app's guard runs on the document in the
# CAPTURE phase, so this bubble-phase listener sees the form exactly as the submission
# algorithm would — hidden copies in, disabled controls out — and stops the navigation.
_INTERCEPT = """() => {
  window.__sent = null;
  document.addEventListener('submit', function (e) {
    // The guard cancels a duplicate submit rather than letting it through, so a submit
    // event that arrives already-defaultPrevented is one the browser never sends.
    window.__sent = e.defaultPrevented
      ? null : Array.from(new FormData(e.target).entries());
    e.preventDefault();
  }, false);
}"""

CONFIRM_FORM = "form[action='/chat/confirm']"


@pytest.fixture
def chat_page(client, tmp_path):
    email = "form-submit@example.com"
    _seed_user(email=email, password="pw", is_admin=False)
    client.post("/login", data={"email": email, "password": "pw"})
    uid = _user_id(email)

    async def stage():
        async with async_session_maker() as s:
            await repository.set_pending_plan_edit(
                s, uid,
                [{"action": "move", "date": "2026-07-01", "to_date": "2026-07-04"}],
                [{"action": "move", "date": "2026-07-01", "to_date": "2026-07-03"}],
                summary="Переніс довгу на суботу.", alt_summary="Або на п'ятницю.",
                risky=True,
            )

    anyio.run(stage)
    stage_assets(tmp_path)
    return stage_pages(client, tmp_path, {"chat": "/chat"})["chat"]


def _open(path):
    exe = chromium_path()
    if not exe:
        pytest.skip("no chromium binary available")
    return exe, path


@pytest.mark.parametrize("label,expected", [
    ("✅", "apply"),
    ("🛡", "apply_alt"),
    ("❌", "cancel"),
])
def test_the_pressed_button_reaches_the_server(chat_page, label, expected):
    exe, path = _open(chat_page)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe)
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.route("**/*", local_only([]))
            page.goto(path.as_uri())
            page.evaluate(_INTERCEPT)
            page.locator(f"{CONFIRM_FORM} button", has_text=label).first.click()
            assert page.evaluate("() => window.__sent") == [["action", expected]]
        finally:
            browser.close()


def test_the_double_submit_guard_still_locks_the_form(chat_page):
    """The value has to survive without giving up what the guard is for: after the first
    click every button is disabled, and a second click sends nothing at all."""
    exe, path = _open(chat_page)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe)
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.route("**/*", local_only([]))
            page.goto(path.as_uri())
            page.evaluate(_INTERCEPT)
            page.locator(f"{CONFIRM_FORM} button", has_text="✅").first.click()
            assert page.evaluate("() => window.__sent") == [["action", "apply"]]

            assert page.evaluate(
                f"() => Array.from(document.querySelectorAll({CONFIRM_FORM + ' button'!r}))"
                ".every(b => b.disabled)"
            )
            # and the copy is a copy, not a second answer: one entry, never two
            assert page.evaluate(
                f"() => document.querySelectorAll({CONFIRM_FORM!r} + ' [name=action]').length"
            ) >= 1
            page.evaluate(f"() => {{ window.__sent = null; "
                          f"document.querySelector({CONFIRM_FORM!r}).requestSubmit(); }}")
            assert page.evaluate("() => window.__sent") is None
        finally:
            browser.close()
