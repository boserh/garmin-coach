"""NF-28 · the lifestyle log and its binary variables in the correlation engine.

The feature's whole risk is confident nonsense on a thin diary, so most of these tests are
about what must NOT be reported: a tag with too few observations of either class, a finding
phrased as causation, or a judgemental sentence about the user's evening.
"""
import datetime as dt

import pytest

from app import correlations
from app.db import lifestyle as lifestyle_db
from app.db.models import User


async def _user(session) -> User:
    u = User(email="ls@example.com", password_hash="x")
    session.add(u)
    await session.commit()
    return u


def _history(n: int, start: dt.date, hrv_fn) -> list:
    """``n`` consecutive daily rows with a caller-computed HRV."""
    return [
        {"date": (start + dt.timedelta(days=i)).isoformat(),
         "hrv_avg": hrv_fn(i), "sleep_score": 80, "resting_hr": 50}
        for i in range(n)
    ]


# ---------- vocabulary / parsing ----------

def test_tag_slugs_are_stable_values():
    """Slugs are DB values — renaming one orphans a year of history, so only labels move."""
    assert set(lifestyle_db.TAG_ORDER) == {
        "alcohol", "caffeine", "late_meal", "stress", "travel", "sick"}


def test_parse_tags_is_order_canonical():
    assert lifestyle_db.parse_tags("вчора пиво і стрес") == ["alcohol", "stress"]
    assert lifestyle_db.parse_tags("стрес, пиво") == ["alcohol", "stress"]
    assert lifestyle_db.parse_tags("нічого цікавого") == []


def test_parse_date_relative_and_explicit():
    today = dt.date(2026, 8, 5)
    assert lifestyle_db.parse_date("пиво", today) == today
    assert lifestyle_db.parse_date("вчора пиво", today) == dt.date(2026, 8, 4)
    assert lifestyle_db.parse_date("позавчора пиво", today) == dt.date(2026, 8, 3)
    assert lifestyle_db.parse_date("2026-07-30 пиво", today) == dt.date(2026, 7, 30)


def test_parse_date_refuses_the_future_and_ancient_history():
    """Backfilling a random month from memory is not data — the caller says so instead of
    storing a guess."""
    today = dt.date(2026, 8, 5)
    assert lifestyle_db.parse_date("2026-08-09 пиво", today) is None
    assert lifestyle_db.parse_date("2026-01-01 пиво", today) is None


# ---------- storage ----------

@pytest.mark.asyncio
async def test_empty_tags_are_stored_as_data(session):
    """"Nothing happened" must be a ROW, not an absent one: without those nights there is
    no control group and no association can ever be computed."""
    u = await _user(session)
    row = await lifestyle_db.upsert(session, u.id, "2026-08-05", [])
    assert row.tags == []
    assert await lifestyle_db.get_day(session, u.id, "2026-08-05") is not None


@pytest.mark.asyncio
async def test_upsert_does_not_duplicate_a_day(session):
    u = await _user(session)
    await lifestyle_db.upsert(session, u.id, "2026-08-04", ["alcohol"])
    await lifestyle_db.upsert(session, u.id, "2026-08-04", ["stress"])
    rows = await lifestyle_db.read_range(session, u.id, days=30)
    assert [r["tags"] for r in rows] == [["stress"]]


@pytest.mark.asyncio
async def test_toggle_adds_then_removes(session):
    u = await _user(session)
    assert await lifestyle_db.toggle_tag(session, u.id, "2026-08-05", "alcohol") == ["alcohol"]
    assert await lifestyle_db.toggle_tag(session, u.id, "2026-08-05", "stress") == \
        ["alcohol", "stress"]
    assert await lifestyle_db.toggle_tag(session, u.id, "2026-08-05", "alcohol") == ["stress"]


@pytest.mark.asyncio
async def test_logs_are_user_scoped(session):
    a = await _user(session)
    b = User(email="other@example.com", password_hash="x")
    session.add(b)
    await session.commit()
    await lifestyle_db.upsert(session, a.id, "2026-08-05", ["alcohol"])
    assert await lifestyle_db.read_range(session, b.id, days=30) == []


# ---------- correlations ----------

def test_unlogged_days_stay_unknown_not_negative():
    """A day the user ignored is *unknown*. Folding it in as "nothing happened" would
    inflate the control group with every skipped evening."""
    start = dt.date(2026, 5, 1)
    history = _history(5, start, lambda i: 60)
    merged = correlations.merge_lifestyle(
        history, [{"date": start.isoformat(), "tags": ["alcohol"]}])
    assert merged[0]["tag:alcohol"] == 1.0
    assert "tag:alcohol" not in merged[1]


def test_tag_below_the_observation_floor_is_not_correlated():
    """Three logged beers must never produce "alcohol improves your sleep, r=0.9" — the
    single most likely way this feature could produce confident nonsense."""
    start = dt.date(2026, 5, 1)
    n = 60
    # A perfect, screaming signal — only 3 tagged days, so it must still be dropped.
    tagged = {0, 1, 2}
    history = _history(n, start, lambda i: 30 if i - 1 in tagged else 70)
    logs = [{"date": (start + dt.timedelta(days=i)).isoformat(),
             "tags": ["alcohol"] if i in tagged else []} for i in range(n)]
    found = correlations.find_correlations(history, lifestyle_logs=logs)
    assert not [f for f in found if f.get("kind") == "lifestyle"]


def test_tag_with_enough_of_both_classes_is_reported():
    start = dt.date(2026, 5, 1)
    n = 80
    tagged = set(range(0, n, 3))          # every third evening → both classes are large
    # HRV on day i is low when the PREVIOUS evening was tagged (lag 1), with a little
    # variation so the series isn't constant.
    history = _history(n, start, lambda i: (40 if (i - 1) in tagged else 70) + (i % 3))
    logs = [{"date": (start + dt.timedelta(days=i)).isoformat(),
             "tags": ["alcohol"] if i in tagged else []} for i in range(n)]
    findings = [f for f in correlations.find_correlations(history, lifestyle_logs=logs)
                if f.get("kind") == "lifestyle"]
    assert findings, "a strong, well-sampled association should surface"
    f = next(f for f in findings if f["y"] == "hrv_avg")
    assert f["x"] == "tag:alcohol" and f["lag"] == 1
    assert f["delta"] < 0     # HRV lower after tagged evenings


def test_lifestyle_findings_read_as_association_never_verdict():
    """AC: never causation, never judgement. The text is the product here — a sentence that
    reads as a verdict on the user's evening is a bug, not a wording preference."""
    f = correlations._tag_finding(
        "alcohol", "hrv_avg", -0.5, 60, mean_with=48.0, mean_without=60.0,
        unit="мс", y_name="HRV")
    text = f["detail"].lower()
    assert correlations.ASSOCIATION_NOTE in text
    for banned in ("шкідлив", "погана звичка", "кинь", "перестань", "треба менше",
                   "через це", "спричиня", "викликає"):
        assert banned not in text, f"judgemental/causal wording leaked: {banned}"


def test_build_context_separates_lifestyle_findings():
    findings = [
        {"x": "sleep_score", "y": "hrv_avg", "r": 0.4},
        {"x": "tag:alcohol", "y": "hrv_avg", "r": -0.4, "kind": "lifestyle"},
    ]
    ctx = correlations.build_context(findings, 90)
    assert len(ctx["findings"]) == 1 and len(ctx["lifestyle_findings"]) == 1


def test_lifestyle_findings_are_in_the_dedup_cache_key():
    """The backlog's cross-cutting trap: new context that isn't in the key means /insights
    keeps serving the pre-lifestyle text after the first tag is logged."""
    from app.analysis.cache import _insights_cache_key

    base = {"window_days": 90, "findings": [], "lifestyle_findings": []}
    with_tag = {**base, "lifestyle_findings": [{"x": "tag:alcohol", "r": -0.4}]}
    assert _insights_cache_key(base, "m") != _insights_cache_key(with_tag, "m")


# ---------- bot surface ----------

def test_keyboard_marks_selected_tags():
    from bot.handlers import lifestyle_keyboard

    kb = lifestyle_keyboard("2026-08-05", ["alcohol"])
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any(t.startswith("✓") and "алкоголь" in t for t in labels)
    assert any("Нічого такого" in t for t in labels)


def test_keyboard_callback_data_carries_the_date():
    """Stateless by construction: a prompt left unanswered past midnight still writes to
    the day it was asked about, not to whatever "today" is when it's finally tapped."""
    from bot.handlers import lifestyle_keyboard

    kb = lifestyle_keyboard("2026-08-05")
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert all(d.startswith("ls:") and "2026-08-05" in d for d in data)
