"""No page may scroll horizontally on a phone.

Twice now a flex row that couldn't shrink pushed a card past the screen edge (the
structured-steps repeat group on /plan, then the badge row on a matched session) — a
class of bug no template assertion catches, because it only exists once the CSS is laid
out. So this renders the real pages through the app, hands the HTML to a headless
browser with the real stylesheet, and asserts ``scrollWidth <= clientWidth``.

Opt-in: it needs ``playwright`` plus a Chromium binary, so it skips where neither is set
up (CI installs only ``.[dev]``). Run it locally with::

    ./venv/bin/python -m pip install playwright
    PLAYWRIGHT_BROWSERS_PATH=... ./venv/bin/python -m pytest tests/test_mobile_layout.py
"""
import pytest

from tests.browser_helpers import (
    WIDTHS,
    chromium_path,
    local_only,
    seed_report_logs,
    seed_rich_history,
    stage_assets,
    stage_pages,
)
from tests.web_helpers import _seed_user, _user_id

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed"
).sync_playwright


# Every element wider than the viewport, so a failure names the culprit instead of just
# reporting a number. <details> are forced open first — collapsed content still has to fit.
_PROBE = """() => {
  document.querySelectorAll('details').forEach(d => d.open = true);
  const doc = document.documentElement, vw = doc.clientWidth, seen = new Set(), bad = [];
  const seenLeft = new Set(), left = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (!r.width) continue;
    if (r.right > vw + 1 || r.left < -1) {
      const raw = typeof el.className === 'string' ? el.className.trim() : '';
      const cls = raw ? '.' + raw.split(/\\s+/).join('.') : '';
      const sel = el.tagName.toLowerCase() + cls;
      const box = `[${Math.round(r.left)}..${Math.round(r.right)}]`;
      if (!seen.has(sel)) { seen.add(sel); bad.push(sel + ' ' + box); }
      // Off the LEFT edge is reported separately because scrollWidth cannot see it:
      // the page scrolls right, never left, so an element centred while wider than the
      // viewport hangs off both sides and the scrollWidth check passes on a form that
      // is visibly cut in half. That is exactly how the auth stack shipped broken.
      if (r.left < -1 && !seenLeft.has(sel)) { seenLeft.add(sel); left.push(sel + ' ' + box); }
    }
  }
  // Text nodes too: an unbreakable token (a long email in a heading) paints past the
  // edge while its parent's box stays put, so an element-only scan reports nothing.
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.nodeValue.trim()) continue;
    const range = document.createRange();
    range.selectNodeContents(n);
    if (range.getBoundingClientRect().right > vw + 1) {
      bad.push('text "' + n.nodeValue.trim().slice(0, 30) + '" in ' +
               n.parentElement.tagName.toLowerCase());
    }
  }
  return {scrollW: doc.scrollWidth, clientW: vw, offenders: bad.slice(0, 6),
          leftOffenders: left.slice(0, 6)};
}"""


def test_pages_do_not_scroll_horizontally_on_a_phone(client, tmp_path):
    browser_path = chromium_path()
    if not browser_path:
        pytest.skip("no chromium binary available")

    # A dedicated user: this test seeds a plan and 20 days of history, and the shared
    # auth_client account is one other modules assert things about (e.g. "no active plan
    # → the setup form"), so borrowing it would leak state across the suite.
    email = "mobile-layout@example.com"
    _seed_user(email=email, password="pw", is_admin=False)
    client.post("/login", data={"email": email, "password": "pw"})
    act_id = seed_rich_history(_user_id(email))

    stage_assets(tmp_path)
    pages = {"dashboard": "/dashboard", "me": "/me", "daily": "/me/daily_metrics",
             "activities": "/me/activities", "reports": "/me/report_logs",
             "plan": "/plan", "chat": "/chat", "settings": "/settings",
             "insights": "/insights", "strength": "/strength",
             "offline": "/offline", "info": "/info", "onboarding": "/onboarding",
             # The signed-out pages were never measured at all. Their own defect was a
             # centring one (see the test below), but they belong in the overflow sweep
             # too — they are the first thing anyone sees.
             "login": "/login", "register": "/register"}
    if act_id:
        pages["activity"] = f"/me/activities/{act_id}"
    files = stage_pages(client, tmp_path, pages)

    # The borrowed-session bar (app.core.impersonate) exists only while an admin is
    # impersonating, so it needs a page staged from that state: it's a full-width sticky
    # row carrying two email addresses and a button — the shape that overflows first.
    _seed_user(email="layout-admin@example.com", password="pw", is_admin=True)
    client.post("/login", data={"email": "layout-admin@example.com", "password": "pw"})
    client.post(f"/admin/users/{_user_id(email)}/impersonate")
    files.update(stage_pages(client, tmp_path, {"dashboard_impersonated": "/dashboard"}))
    client.post("/impersonate/stop")

    failures = []
    external = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path)
        for width in WIDTHS:
            page = browser.new_page(viewport={"width": width, "height": 844})
            page.route("**/*", local_only(external))
            for name, path in files.items():
                page.goto(path.as_uri())
                m = page.evaluate(_PROBE)
                if m["scrollW"] > m["clientW"]:
                    failures.append(
                        f"{name} @{width}px: scrollWidth {m['scrollW']} > {m['clientW']} "
                        f"— {', '.join(m['offenders'])}")
                if m["leftOffenders"]:
                    failures.append(
                        f"{name} @{width}px: off the left edge — "
                        f"{', '.join(m['leftOffenders'])}")
            page.close()
        browser.close()

    assert not external, "pages requested external hosts: " + ", ".join(sorted(set(external)))
    assert not failures, "horizontal overflow:\n" + "\n".join(failures)


def test_admin_pages_do_not_scroll_horizontally_on_a_phone(client, tmp_path):
    """The admin pages were never measured, and /admin/cache shipped unusable on a phone:
    its hit-rate table is five columns wide with nothing to scroll inside, so the PAGE
    scrolled and the last column sat off-screen. These are read on a phone like every
    other page — the DB browser's own tables already scroll inside a `.tscroll` box, which
    is the shape the rest has to match."""
    browser_path = chromium_path()
    if not browser_path:
        pytest.skip("no chromium binary available")

    email = "admin-layout@example.com"
    _seed_user(email=email, password="pw", is_admin=True)
    client.post("/login", data={"email": email, "password": "pw"})
    uid = _user_id(email)
    seed_rich_history(uid)
    # The hit-rate table is only as wide as its widest row, so an empty one measures
    # nothing: seed the longest real `kind` names and a three-digit rate.
    seed_report_logs(uid, kinds=("supplements", "checkup_ocr", "race_debrief", "morning"))

    stage_assets(tmp_path)
    files = stage_pages(client, tmp_path, {
        "admin_cache": "/admin/cache",
        "admin_jobs": "/admin/jobs",
        "admin_users": "/admin/users",
        "ui_index": "/ui",
        "ui_table": "/ui/activities",
    })

    failures = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path)
        for width in WIDTHS:
            page = browser.new_page(viewport={"width": width, "height": 844})
            page.route("**/*", local_only([]))
            for name, path in files.items():
                page.goto(path.as_uri())
                m = page.evaluate(_PROBE)
                if m["scrollW"] > m["clientW"]:
                    failures.append(
                        f"{name} @{width}px: scrollWidth {m['scrollW']} > {m['clientW']} "
                        f"— {', '.join(m['offenders'])}")
            page.close()
        browser.close()

    assert not failures, "horizontal overflow:\n" + "\n".join(failures)


# A page-private <style> block only overrides the properties it NAMES. Everything else
# leaks in from whatever rule in app.css happens to share the class name — which is how
# the DB browser's filter row became a right-aligned column: `.fbar` over there is the
# activity filters on /me/activities, and its `flex-direction: column` (never
# re-declared here) applied. Same failure as `.step` on /plan; see the CLAUDE.md rule.
_FILTER_PROBE = """() => {
  document.querySelectorAll('details').forEach(d => d.open = true);
  // Structural, not by class name: renaming the class is exactly the fix, and a probe
  // that keys on the new name would "pass" the old markup by failing to find it.
  const form = document.querySelector('details.fwrap form');
  if (!form) return {found: false};
  const lefts = [...form.querySelectorAll('.ffield')]
    .map(el => Math.round(el.getBoundingClientRect().left));
  return {
    found: true,
    direction: getComputedStyle(form).flexDirection,
    align: getComputedStyle(form).alignItems,
    lefts: lefts,
    formLeft: Math.round(form.getBoundingClientRect().left),
  };
}"""


def test_db_browser_filters_lay_out_as_a_left_aligned_row(client, tmp_path):
    """The filter fields start at the form's left edge, on every screen width.

    Not an overflow bug, so the guard above can't see it: the fields stayed inside the
    viewport, just pushed to the right at a different offset each — unusable, and
    passing every test we had."""
    browser_path = chromium_path()
    if not browser_path:
        pytest.skip("no chromium binary available")

    email = "db-filters@example.com"
    _seed_user(email=email, password="pw", is_admin=True)   # /ui is admin-only
    client.post("/login", data={"email": email, "password": "pw"})

    stage_assets(tmp_path)
    files = stage_pages(client, tmp_path, {"ui_activities": "/ui/activities"})

    failures = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path)
        for width in WIDTHS:
            page = browser.new_page(viewport={"width": width, "height": 844})
            page.route("**/*", local_only([]))
            page.goto(files["ui_activities"].as_uri())
            m = page.evaluate(_FILTER_PROBE)
            assert m["found"], "the DB browser's filter form was not rendered"
            if m["direction"] != "row":
                failures.append(f"@{width}px: flex-direction is {m['direction']}, not row")
            # The FIRST field, not every field: this row wraps, and a field sharing a
            # line with the one before it legitimately starts further right. What the
            # bug did was push every field off the left edge, first one included.
            if m["lefts"] and m["lefts"][0] != m["formLeft"]:
                failures.append(
                    f"@{width}px: the first filter field starts at {m['lefts'][0]}, "
                    f"not at the form's left edge ({m['formLeft']})")
            page.close()
        browser.close()

    assert not failures, "DB browser filters:\n" + "\n".join(failures)


# Widths, not just phone widths. The auth stack's defect was invisible at 390px (a 3px
# nudge) and glaring at 900px (the card sat 170px left of centre) — because the stack had
# no width of its own, so it took its max-content and the cards hugged its left edge while
# `body.auth` dutifully centred the oversized stack. Anything that only measures a phone
# reports this page as fine.
_CENTRE_PROBE = """() => {
  const vw = document.documentElement.clientWidth;
  return [...document.querySelectorAll('.authcard')].map(el => {
    const r = el.getBoundingClientRect();
    return {left: Math.round(r.left), rightGap: Math.round(vw - r.right)};
  });
}"""

AUTH_WIDTHS = (900, 390, 320)


def test_signed_out_pages_stay_centred_at_every_width(client, tmp_path):
    """/login and /register are one centred column of cards — at any window size."""
    browser_path = chromium_path()
    if not browser_path:
        pytest.skip("no chromium binary available")

    stage_assets(tmp_path)
    files = stage_pages(client, tmp_path, {"login": "/login", "register": "/register"})

    failures = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path)
        for width in AUTH_WIDTHS:
            page = browser.new_page(viewport={"width": width, "height": 900})
            page.route("**/*", local_only([]))
            for name, path in files.items():
                page.goto(path.as_uri())
                cards = page.evaluate(_CENTRE_PROBE)
                assert cards, f"{name}: no .authcard rendered"
                for i, c in enumerate(cards):
                    if abs(c["left"] - c["rightGap"]) > 2:
                        failures.append(
                            f"{name} @{width}px: card {i} off-centre — {c['left']}px on "
                            f"the left, {c['rightGap']}px on the right")
            page.close()
        browser.close()

    assert not failures, "auth pages off-centre:\n" + "\n".join(failures)
