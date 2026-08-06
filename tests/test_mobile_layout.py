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
import datetime as dt
import os
import re
import shutil

import anyio
import pytest

from tests.web_helpers import _seed_user, _user_id

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed"
).sync_playwright

# Phone widths worth guarding: a modern iPhone and the narrowest phone still in use.
WIDTHS = (390, 320)


def _chromium_path():
    """The browser Playwright should drive, or None when there isn't one installed."""
    for cand in ("/opt/pw-browsers/chromium", shutil.which("chromium"),
                 shutil.which("chromium-browser"), shutil.which("google-chrome")):
        if cand and os.path.exists(cand):
            return cand
    return None


def _seed(uid):
    """A user with enough history that every widget on the pages under test renders:
    trends, activity cards with series, a plan with structured steps, a matched session."""
    from app.db.base import async_session_maker
    from app.db.models import ActivityRecord, DailyMetric
    from app.garmin import repository
    from app.garmin.schemas import PlanStep, PlanWorkout

    today = dt.date.today()

    async def go():
        async with async_session_maker() as s:
            for i in range(20):
                d = (today - dt.timedelta(days=i)).isoformat()
                s.add(DailyMetric(user_id=uid, date=d, hrv_avg=60 + i % 9,
                                  sleep_score=70 + i % 20, sleep_h=7.2,
                                  stress_avg=30 + i % 10, bb_charged=60,
                                  extra={"resting_hr": 50, "readiness_score": 65,
                                         "vo2max": 47.5}))
                s.add(ActivityRecord(
                    user_id=uid, activity_id=1000 + i, date=d,
                    type=("running" if i % 3 else "gravel_cycling"),
                    dist_km=65.7 if i % 3 == 0 else 8.3,
                    dur_min=278 if i % 3 == 0 else 47, avg_hr=139, max_hr=171, load=118.5,
                    series=[{"d": j * 100, "p": 5.5, "hr": 140, "e": 100} for j in range(40)],
                    subjective={"rpe": 7, "pain": "коліно"}, analysis="Розбір."))
            await repository.create_plan(
                s, uid, goal="general", goal_label="Загальна форма", target_date=None,
                start_date=(today - dt.timedelta(days=14)).isoformat(), days_per_week=3,
                intensity="easy", intake={}, summary="Блок на 3 тижні.",
                workouts=[
                    PlanWorkout(date=(today - dt.timedelta(days=13)).isoformat(), week=1,
                                type="easy", dist_km=5.0, description="легкий біг"),
                    # the shapes that broke before: a repeat group with a pace range, and a
                    # session carrying today + done + a link to the matched activity
                    PlanWorkout(date=today.isoformat(), week=3, type="intervals", dist_km=8.0,
                                description="Інтервали 6×800 м у темпі 4:50–5:00/км.",
                                steps=[PlanStep(kind="warmup", dist_m=1500, hr_zone=2),
                                       PlanStep(kind="repeat", reps=6, steps=[
                                           PlanStep(kind="run", dist_m=800,
                                                    pace_min_km=[4.83, 5.0]),
                                           PlanStep(kind="recovery", dur_s=120)]),
                                       PlanStep(kind="cooldown", dist_m=1000, hr_zone=1)]),
                ])
            plan = await repository.get_active_plan(s, uid)
            acts = await repository.list_activities(s, uid, n=1)
            act_id = acts[0]["id"] if acts else None
            for w in await repository.list_workouts(s, plan.id):
                if w.date == today.isoformat() and act_id:
                    w.status, w.completed_activity_id = "done", act_id
                    w.match_info = {"manual": True, "actual_dist_km": 8.2}
            await s.commit()
            return act_id

    return anyio.run(go)


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
    browser_path = _chromium_path()
    if not browser_path:
        pytest.skip("no chromium binary available")

    # A dedicated user: this test seeds a plan and 20 days of history, and the shared
    # auth_client account is one other modules assert things about (e.g. "no active plan
    # → the setup form"), so borrowing it would leak state across the suite.
    email = "mobile-layout@example.com"
    _seed_user(email=email, password="pw", is_admin=False)
    client.post("/login", data={"email": email, "password": "pw"})
    act_id = _seed(_user_id(email))

    # The pages are rendered to files and loaded over file:// — copy the real stylesheet
    # and the self-hosted webfont next to them so the layout under test is the shipped
    # one, laid out in the shipped typeface, not a fixture in a fallback font.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    static = os.path.join(repo_root, "app", "static")
    css = open(os.path.join(static, "app.css"), encoding="utf-8").read()
    (tmp_path / "app.css").write_text(css.replace("/static/fonts/", "fonts/"),
                                      encoding="utf-8")
    shutil.copytree(os.path.join(static, "fonts"), tmp_path / "fonts")

    pages = {"dashboard": "/dashboard", "me": "/me", "daily": "/me/daily_metrics",
             "activities": "/me/activities", "reports": "/me/report_logs",
             "plan": "/plan", "chat": "/chat", "settings": "/settings"}
    if act_id:
        pages["activity"] = f"/me/activities/{act_id}"

    files = {}
    for name, url in pages.items():
        r = client.get(url)
        assert r.status_code == 200, (name, r.status_code)
        # UI-02: the stylesheet link carries a content-derived ?v=, so match the pattern
        # rather than a literal — the previous hardcoded "?v=3" had silently stopped
        # matching when the templates moved to "?v=4", which left this guard measuring
        # an UNSTYLED page (and therefore passing on nothing at all).
        html, subs = re.subn(r"/static/app\.css\?v=\S*?(?=[\"'])", "app.css", r.text)
        assert subs == 1, (
            f"{name}: expected exactly one /static/app.css?v=… link to rewrite, got "
            f"{subs} — this guard only means something with the real stylesheet applied"
        )
        path = tmp_path / f"{name}.html"
        path.write_text(html, encoding="utf-8")
        files[name] = path

    failures = []
    external = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path)
        for width in WIDTHS:
            page = browser.new_page(viewport={"width": width, "height": 844})
            # Nothing may leave the machine: UI-02 moved the webfont in-repo, so any
            # non-file:// request is a regression (a re-added CDN link) and is recorded
            # rather than silently allowed to hang the run.
            def _guard(route):
                url = route.request.url
                if url.startswith("file://"):
                    route.continue_()
                else:
                    external.append(url)
                    route.abort()
            page.route("**/*", _guard)
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
