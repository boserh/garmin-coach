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
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (!r.width) continue;
    if (r.right > vw + 1 || r.left < -1) {
      const raw = typeof el.className === 'string' ? el.className.trim() : '';
      const cls = raw ? '.' + raw.split(/\\s+/).join('.') : '';
      const sel = el.tagName.toLowerCase() + cls;
      const box = `[${Math.round(r.left)}..${Math.round(r.right)}]`;
      if (!seen.has(sel)) { seen.add(sel); bad.push(sel + ' ' + box); }
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
  return {scrollW: doc.scrollWidth, clientW: vw, offenders: bad.slice(0, 6)};
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
             "insights": "/insights"}
    if act_id:
        pages["activity"] = f"/me/activities/{act_id}"
    files = stage_pages(client, tmp_path, pages)

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
            page.close()
        browser.close()

    assert not external, "pages requested external hosts: " + ", ".join(sorted(set(external)))
    assert not failures, "horizontal overflow:\n" + "\n".join(failures)
