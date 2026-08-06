"""UI-01: the charts have to work with a finger, not just a mouse.

The app is mobile-first, yet every chart tooltip listened for ``mousemove`` only — on
the device it's designed for, the most expensive part of the page (HRV/sleep/pace/HR)
was decoration. This drives the real pages in headless Chromium with a touch pointer
and asserts a value actually appears, that lifting the finger doesn't take it away, and
that scrubbing a chart hasn't cost the page its vertical scroll.

Opt-in like the layout guard (``playwright`` + a Chromium binary)::

    ./venv/bin/python -m pytest tests/test_chart_touch.py
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

# A finger drag across the first chart's plot area, in the coordinates the page sees.
_SCRUB = """(sel) => {
  const wrap = document.querySelector(sel);
  const r = wrap.getBoundingClientRect();
  const at = (frac) => ({
    pointerId: 1, pointerType: 'touch', isPrimary: true, bubbles: true, cancelable: true,
    clientX: r.left + r.width * frac, clientY: r.top + r.height / 2, buttons: 1
  });
  wrap.dispatchEvent(new PointerEvent('pointerdown', at(0.2)));
  wrap.dispatchEvent(new PointerEvent('pointermove', at(0.6)));
  const tip = wrap.querySelector('.tip');
  const during = {display: getComputedStyle(tip).display, text: tip.textContent};
  wrap.dispatchEvent(new PointerEvent('pointerup', {...at(0.6), buttons: 0}));
  return {during, after: {display: getComputedStyle(tip).display, text: tip.textContent}};
}"""

CHART = ".chart[data-pts] .cwrap"


@contextmanager
def _page(path, **kwargs):
    """A loaded page, with the browser started inside the test body.

    Deliberately not a fixture: ``sync_playwright`` drives its own event loop, and
    holding it open across fixture setup breaks the ``anyio.run`` the seeding helpers
    use ("cannot run the event loop while another loop is running").
    """
    exe = chromium_path()
    if not exe:
        pytest.skip("no chromium binary available")
    opts = {"viewport": {"width": 390, "height": 844}, "has_touch": True,
            "is_mobile": True, **kwargs}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe)
        try:
            page = browser.new_page(**opts)
            page.route("**/*", local_only([]))
            page.goto(path.as_uri())
            yield page
        finally:
            browser.close()


@pytest.fixture
def pages(client, tmp_path):
    email = "chart-touch@example.com"
    _seed_user(email=email, password="pw", is_admin=False)
    client.post("/login", data={"email": email, "password": "pw"})
    act_id = seed_rich_history(_user_id(email))
    stage_assets(tmp_path)
    urls = {"dashboard": "/dashboard"}
    if act_id:
        urls["activity"] = f"/me/activities/{act_id}"
    return stage_pages(client, tmp_path, urls)


@pytest.mark.parametrize("name", ["dashboard", "activity"])
def test_a_finger_drag_reads_a_value_off_the_chart(pages, name):
    if name not in pages:
        pytest.skip(f"no {name} page seeded")
    with _page(pages[name]) as page:
        assert page.locator(CHART).count() > 0, "no interactive chart on the page"
        r = page.evaluate(_SCRUB, CHART)
        assert r["during"]["display"] != "none", "tooltip stayed hidden under the finger"
        assert r["during"]["text"].strip(), "tooltip came up empty"
        # A phone has no mouseleave; a bubble that vanishes the instant the finger lifts
        # reads as "nothing happened", so it must survive pointerup.
        assert r["after"]["display"] != "none"
        assert r["after"]["text"] == r["during"]["text"]


def test_a_tap_outside_dismisses_the_tooltip(pages):
    with _page(pages["dashboard"]) as page:
        page.evaluate(_SCRUB, CHART)
        page.evaluate("""() => document.body.dispatchEvent(new PointerEvent(
            'pointerdown', {pointerId: 2, pointerType: 'touch', bubbles: true,
                            clientX: 5, clientY: 5}))""")
        assert page.evaluate(
            "() => getComputedStyle(document.querySelector('.chart[data-pts] .tip')).display"
        ) == "none"


def test_the_chart_does_not_swallow_the_page_scroll(pages):
    with _page(pages["dashboard"]) as page:
        # touch-action must be scoped so the vertical gesture still belongs to the page:
        # `pan-y` on .cwrap, never on body — the latter would kill scrolling everywhere.
        assert page.evaluate(
            f"() => getComputedStyle(document.querySelector({CHART!r})).touchAction"
        ) == "pan-y"
        assert page.evaluate("() => getComputedStyle(document.body).touchAction") == "auto"


def test_arrow_keys_walk_the_series(pages):
    with _page(pages["dashboard"]) as page:
        page.locator(CHART).first.focus()
        page.keyboard.press("ArrowRight")
        first = page.evaluate(
            "() => document.querySelector('.chart[data-pts] .tip').textContent")
        assert first.strip(), "keyboard focus + ArrowRight showed nothing"
        page.keyboard.press("End")
        last = page.evaluate(
            "() => document.querySelector('.chart[data-pts] .tip').textContent")
        assert last.strip() and last != first
        # The reading has to be announced, not just painted.
        assert page.evaluate(
            "() => document.querySelector('.chart[data-pts] .tip').getAttribute('aria-live')"
        ) == "polite"


def test_the_page_still_renders_without_javascript(pages):
    """No JS (or a blocked script) must leave the chart as a plain static SVG, not a
    broken page — the SVG is rendered server-side by app/charts.py on purpose."""
    with _page(pages["dashboard"], java_script_enabled=False) as page:
        assert page.locator(".chart[data-pts] svg polyline").count() > 0
