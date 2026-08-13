"""Shared plumbing for the two opt-in browser tests (mobile layout guard, chart touch).

Both render real pages through the app, write them next to the real stylesheet, and
drive them in headless Chromium over ``file://``. Keeping the seeding and the
file-staging here means the layout guard and the interaction test measure the same
pages, and a new browser test costs a few lines instead of a copy.

Neither the browser nor ``playwright`` is a hard dependency (CI installs ``.[dev]``
only) — callers guard with :func:`chromium_path` and ``pytest.importorskip``.
"""
import datetime as dt
import os
import re
import shutil

import anyio

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO_ROOT, "app", "static")

# Phone widths worth guarding: a modern iPhone and the narrowest phone still in use.
WIDTHS = (390, 320)


def chromium_path():
    """The browser Playwright should drive, or None when there isn't one installed."""
    for cand in ("/opt/pw-browsers/chromium", shutil.which("chromium"),
                 shutil.which("chromium-browser"), shutil.which("google-chrome")):
        if cand and os.path.exists(cand):
            return cand
    return None


def seed_rich_history(uid):
    """A user with enough history that every widget on the pages under test renders:
    trends, activity cards with series, a plan with structured steps, a matched session."""
    from app.db.base import async_session_maker
    from app.db.models import ActivityRecord, DailyMetric
    from app.garmin import repository
    from app.garmin.schemas import PlanStep, PlanWorkout

    today = dt.date.today()

    async def go():
        async with async_session_maker() as s:
            # Idempotent: the DB outlives a single test, so a module with several
            # browser tests would otherwise trip the (user_id, activity_id) unique key.
            existing = await repository.list_activities(s, uid, n=1)
            if existing:
                return existing[0]["id"]
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
            # UI-06: a few weeks of strength so /strength renders its charts rather than
            # its empty state — the layout worth guarding is the populated one.
            # Dated from yesterday back, so today's run stays the newest activity (the
            # one the activity-page tests open).
            for w in range(6):
                s.add(ActivityRecord(
                    user_id=uid, activity_id=1500 + w,
                    date=(today - dt.timedelta(days=7 * w + 1)).isoformat(),
                    type="strength_training", dur_min=55.0,
                    exercises={"sets": {
                        "Жим лежачи": {"count": 3, "reps": [5, 5, 5],
                                       "weight_kg": [70.0 + w, 70.0 + w, 70.0 + w]},
                        "Присідання зі штангою на спині": {
                            "count": 3, "reps": [5, 5, 5],
                            "weight_kg": [100.0 + w, 100.0 + w, 100.0 + w]},
                    }}))
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


def seed_report_logs(uid, kinds, per_kind=3):
    """A few `report_logs` rows per kind, so the admin cache page's hit-rate table has
    rows to measure. An empty table is narrow and proves nothing about its layout."""
    from app.db.base import async_session_maker
    from app.db.models import ReportLog

    async def go():
        async with async_session_maker() as s:
            for kind in kinds:
                for i in range(per_kind):
                    s.add(ReportLog(user_id=uid, kind=kind, model="claude-sonnet-5",
                                    input_tokens=1200, output_tokens=400, cost_usd=0.0123,
                                    ok=True, cached=bool(i)))
            await s.commit()

    anyio.run(go)


def stage_assets(tmp_path):
    """Copy the real stylesheet + the self-hosted font next to the staged pages, so the
    layout under test is the shipped one, laid out in the shipped typeface."""
    css = open(os.path.join(STATIC, "app.css"), encoding="utf-8").read()
    (tmp_path / "app.css").write_text(css.replace("/static/fonts/", "fonts/"),
                                      encoding="utf-8")
    if not (tmp_path / "fonts").exists():
        shutil.copytree(os.path.join(STATIC, "fonts"), tmp_path / "fonts")
    shutil.copy(os.path.join(STATIC, "app.js"), tmp_path / "app.js")


def stage_pages(client, tmp_path, pages):
    """Fetch ``{name: url}`` through the app and write each to ``tmp_path`` with its
    asset links repointed at the staged copies. Returns ``{name: Path}``."""
    files = {}
    for name, url in pages.items():
        r = client.get(url)
        assert r.status_code == 200, (name, r.status_code)
        # UI-02: the links carry a content-derived ?v=, so match the pattern rather than
        # a literal — the previous hardcoded "?v=3" had silently stopped matching, which
        # left the layout guard measuring an UNSTYLED page (passing on nothing at all).
        html, css_subs = re.subn(r"/static/app\.css\?v=\S*?(?=[\"'])", "app.css", r.text)
        assert css_subs == 1, (
            f"{name}: expected exactly one /static/app.css?v=… link to rewrite, got "
            f"{css_subs} — these checks only mean something with the real assets applied"
        )
        html, js_subs = re.subn(r"/static/app\.js\?v=\S*?(?=[\"'])", "app.js", html)
        assert js_subs == 1, f"{name}: expected one /static/app.js?v=… link, got {js_subs}"
        path = tmp_path / f"{name}.html"
        path.write_text(html, encoding="utf-8")
        files[name] = path
    return files


def local_only(external):
    """A Playwright route handler that serves ``file://`` and records anything else.

    Nothing may leave the machine: UI-02 moved the webfont in-repo, so a non-``file://``
    request means a CDN link crept back — recorded in ``external`` and aborted rather
    than left to hang the run (letting navigations wait on the network once turned a
    two-second check into four minutes)."""
    def handler(route):
        url = route.request.url
        if url.startswith("file://"):
            route.continue_()
        else:
            external.append(url)
            route.abort()

    return handler
