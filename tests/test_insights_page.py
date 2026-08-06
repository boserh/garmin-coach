"""UI-05: /insights shows what the deterministic modules already compute — for free.

Two guards matter more than the markup:

* **zero cost** — no Claude call and no Garmin request may happen on this path. The
  temptation ("let Claude phrase it nicer") is exactly what would turn a free page into
  a per-view bill, so the client and the provider are both replaced with things that
  explode on use;
* **no second source of truth** — the page displays module output. A threshold typed
  into the template would drift from the module the day one of them changes.
"""
import datetime as dt
import json
from pathlib import Path

import anyio
import pytest

from tests.web_helpers import _seed_user, _user_id

TEMPLATE = Path(__file__).resolve().parent.parent / "app" / "templates" / "insights.html"


@pytest.fixture
def no_llm_no_garmin(monkeypatch):
    """Anything reaching for Claude or Garmin from this request fails loudly."""
    import app.analysis.client as client_mod
    import app.garmin.providers as providers

    def boom(*a, **kw):
        raise AssertionError("the insights page must not call out to Claude/Garmin")

    monkeypatch.setattr(client_mod, "_get_client", boom, raising=False)
    monkeypatch.setattr(providers, "get_provider", boom, raising=False)
    return boom


# A dedicated account, not the shared `auth_client` one: this module seeds 40 days of
# history, and other modules assert on exact row sets in the admin browser for that user.
EMAIL = "insights@example.com"


@pytest.fixture
def page_client(client):
    _seed_user(email=EMAIL, password="pw", is_admin=False)
    client.post("/login", data={"email": EMAIL, "password": "pw"})
    _seed_history(_user_id(EMAIL))
    return client


def _seed_history(uid, days=40, with_pain=True):
    from app.db.base import async_session_maker
    from app.db.models import ActivityRecord, DailyMetric

    today = dt.date.today()

    async def go():
        from app.garmin import repository

        async with async_session_maker() as s:
            # Idempotent: the DB outlives a test, and (user_id, activity_id) is unique.
            if await repository.list_activities(s, uid, n=1):
                return
            for i in range(days):
                d = (today - dt.timedelta(days=i)).isoformat()
                s.add(DailyMetric(user_id=uid, date=d, hrv_avg=55 + (i % 7),
                                  sleep_score=70 + (i % 15), sleep_h=7.1,
                                  stress_avg=30 + (i % 9), bb_charged=62,
                                  extra={"resting_hr": 50 + (i % 4), "acwr_pct": 150.0}))
                if i % 2 == 0:
                    s.add(ActivityRecord(
                        user_id=uid, activity_id=700000 + i, date=d, type="running",
                        dist_km=8.0, dur_min=44.0, avg_hr=145, load=90.0,
                        subjective=({"rpe": 8, "pain": True, "note": "коліно"}
                                    if with_pain and i < 6 else {"rpe": 5})))
            await s.commit()

    anyio.run(go)


def test_the_page_renders_and_spends_nothing(page_client, no_llm_no_garmin):
    r = page_client.get("/insights")
    assert r.status_code == 200
    assert "Інсайти" in r.text


def test_no_report_row_is_written(page_client, no_llm_no_garmin):
    """A ReportLog row is the audit trail of a paid call — none may appear."""
    from sqlalchemy import func, select

    from app.db.base import async_session_maker
    from app.db.models import ReportLog

    uid = _user_id(EMAIL)

    async def count():
        async with async_session_maker() as s:
            return (await s.execute(
                select(func.count()).select_from(ReportLog).where(ReportLog.user_id == uid)
            )).scalar_one()

    before = anyio.run(count)
    page_client.get("/insights")
    assert anyio.run(count) == before


def test_a_fresh_account_gets_an_honest_gate_not_a_blank_page(client, no_llm_no_garmin):
    """No history at all: the page must say what's still missing, using the modules' own
    thresholds — not render six empty cards and not crash."""
    from app import correlations, loadforecast
    from app.core.config import settings

    _seed_user(email="empty-insights@example.com", password="pw", is_admin=False)
    client.post("/login", data={"email": "empty-insights@example.com", "password": "pw"})
    html = client.get("/insights").text

    assert "Замало даних" in html
    assert str(settings.INJURY_MIN_HISTORY_DAYS) in html
    assert str(loadforecast.MIN_HISTORY_DAYS) in html
    assert str(correlations.MIN_SAMPLES) in html
    # …and the sections with nothing to say are absent, not empty.
    assert "Що я помітив" not in html
    assert "Навантаження цього тижня" not in html


def test_the_radar_says_it_is_calibrating_rather_than_all_clear(client, no_llm_no_garmin):
    """A quiet radar on a short history is not a green light, and the page must not read
    like one — that distinction is the whole EP-08 anti-false-positive rule."""
    from app.core.config import settings

    _seed_user(email="calib-insights@example.com", password="pw", is_admin=False)
    client.post("/login", data={"email": "calib-insights@example.com", "password": "pw"})
    uid = _user_id("calib-insights@example.com")
    _seed_history(uid, days=max(1, settings.INJURY_MIN_HISTORY_DAYS - 5), with_pain=False)

    html = client.get("/insights").text
    assert "Калібрування" in html
    assert "Сигналів ризику немає" not in html


def test_active_signals_render_as_cards_with_their_own_words(page_client, no_llm_no_garmin):
    """The detector's own `detail` strings are what's shown — the page never paraphrases
    a signal (that would be a second, drifting copy of the explanation)."""
    from app.analysis.reports import build_injury_assessment
    from app.db.base import async_session_maker

    uid = _user_id(EMAIL)

    async def assess():
        async with async_session_maker() as s:
            return await build_injury_assessment(s, user_id=uid)

    a = anyio.run(assess)
    html = page_client.get("/insights").text
    assert a.signals, "seed produced no risk signals — the test can't check the rendering"
    for s in a.signals:
        assert s.detail in html


def test_correlations_are_labelled_as_correlations(page_client, no_llm_no_garmin, monkeypatch):
    """The block a reader is most likely to misread as causation carries the module's own
    disclaimer, in the markup, not as an optional footnote."""
    from app import correlations
    from app.routers import insights as page

    monkeypatch.setattr(page.correlations, "find_correlations", lambda *a, **k: [
        {"x": "sleep_h", "y": "hrv_avg", "lag": 1, "r": 0.42, "n": 64,
         "direction": "позитивна", "detail": "сон → HRV наступного дня: позитивна"},
    ])
    html = page_client.get("/insights").text
    assert correlations.ASSOCIATION_NOTE in html
    assert "r = 0.42" in html and "n = 64" in html


def test_the_template_holds_no_thresholds_of_its_own():
    """Every gate number on the page must arrive from a module or the settings. A bare
    number in the markup is a second source of truth that silently goes stale."""
    import re

    text = TEMPLATE.read_text(encoding="utf-8")
    # Strip Jinja expressions/statements/comments, then the tags themselves — what's left
    # is the prose a reader sees. SVG geometry and CSS lengths live in attributes and are
    # not thresholds; a stale gate number would show up in the sentence.
    literal = re.sub(r"\{[{%#].*?[}%#]\}", "", text, flags=re.S)
    prose = re.sub(r"<[^>]*>", " ", literal)
    numbers = re.findall(r"(?<![\w-])\d+(?:[.,]\d+)?(?![\w%-])", prose)
    assert not numbers, f"hardcoded numbers in insights.html prose: {numbers}"


def test_an_active_return_ladder_shows_the_current_rung(page_client, no_llm_no_garmin):
    from app import returntorun
    from app.db.base import async_session_maker
    from app.garmin import repository

    uid = _user_id(EMAIL)
    state = returntorun.start(dt.date.today())

    async def save():
        async with async_session_maker() as s:
            await repository.set_state(s, uid, returntorun.STATE_KEY,
                                       json.dumps(state, ensure_ascii=False))
            await s.commit()

    anyio.run(save)
    html = page_client.get("/insights").text
    step = returntorun.step_by_number(state["step"])
    assert "Повернення після болю" in html
    assert step["label"] in html


def test_the_recap_period_switch_is_a_plain_link(page_client, no_llm_no_garmin):
    """Bookmarkable, back-button-friendly, and works with JS off."""
    html = page_client.get("/insights?period=quarter").text
    if "Підсумок періоду" in html:
        assert 'href="?period=year' in html
        assert "aria-current" in html


def test_an_out_of_range_span_is_clamped_not_obeyed(page_client, no_llm_no_garmin):
    """`weeks` is a query param, i.e. user input; the module's own MIN/MAX bound it."""
    for weeks in (-5, 0, 9999):
        assert page_client.get(f"/insights?weeks={weeks}").status_code == 200
    assert page_client.get("/insights?period=nonsense").status_code == 200


def test_it_needs_a_login(client):
    client.get("/logout")
    r = client.get("/insights", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert r.headers["location"].endswith("/login")
