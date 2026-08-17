"""NF-34 · away periods: the pure rules, the storage, and — the point of the whole
feature — the fact that every coaching surface reads the same declared absence.

The bug this closes is a divergence, not a missing field: the morning report knew about a
vacation (via yesterday's report text) while the Sunday digest scored the same week as
"compliance 0%, ні, відстаєш". So the load-bearing tests here are the ones asserting that
BOTH contexts carry it — and that a declared week silences the "схоже, ти захворів" nudge.
"""
import datetime as dt
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import away
from app.analysis import reports, service
from app.analysis.client import CallStats
from app.db import away as away_db
from app.db.models import ActivityRecord, User

U1 = 1
TODAY = dt.date(2026, 8, 16)     # a Sunday — the digest's own day


def _period(start: str, end: str, kind="rest", note=None) -> dict:
    return {"start_date": start, "end_date": end, "kind": kind, "note": note}


async def _user(session, email="away@example.com") -> User:
    u = User(email=email, password_hash="x")
    session.add(u)
    await session.commit()
    return u


# ---------- vocabulary ----------

def test_kind_slugs_are_stable_storage_values():
    """Slugs are DB values; only the labels may change (same rule as NF-28's tags)."""
    assert set(away.KIND_ORDER) == {"rest", "active", "sport", "work"}
    assert away.label("sport").endswith("інший спорт")


def test_each_kind_carries_a_body_expectation():
    """The coach reads `expect`, not the slug — a kite week and a beach week must not
    produce the same advice."""
    assert len({away.expectation(k) for k in away.KIND_ORDER}) == len(away.KIND_ORDER)


@pytest.mark.parametrize("text,slug", [
    ("кайт тиждень в Дахабі", "sport"),
    ("гори, трекінг по 15 км", "active"),
    ("лежатиму на пляжі", "rest"),
    ("відрядження в Берлін", "work"),
    ("поїздка до батьків", None),
])
def test_parse_kind_from_free_text(text, slug):
    assert away.parse_kind(text) == slug


# ---------- date parsing ----------

def test_parse_dates_dm_range():
    s, e = away.parse_dates("16.08-24.08 кайт", TODAY)
    assert (s, e) == (dt.date(2026, 8, 16), dt.date(2026, 8, 24))


def test_parse_dates_iso_range():
    s, e = away.parse_dates("з 2026-09-01 до 2026-09-07 гори", TODAY)
    assert (s, e) == (dt.date(2026, 9, 1), dt.date(2026, 9, 7))


def test_parse_dates_duration_fills_the_end():
    s, e = away.parse_dates("20.12 на 10 днів гори", TODAY)
    assert (s, e) == (dt.date(2026, 12, 20), dt.date(2026, 12, 29))


def test_parse_dates_duration_alone_starts_today():
    s, e = away.parse_dates("на 7 днів кайт", TODAY)
    assert (s, e) == (TODAY, TODAY + dt.timedelta(days=6))


def test_bare_day_month_rolls_forward_to_the_next_occurrence():
    """A period is declared BEFORE it happens, so "05.02" typed in August means next
    February, not the one seven months gone."""
    s, _e = away.parse_dates("05.02 на 5 днів лижі", TODAY)
    assert s == dt.date(2027, 2, 5)


def test_a_range_that_crosses_new_year_keeps_its_order():
    s, e = away.parse_dates("28.12-04.01 лижі", TODAY)
    assert s == dt.date(2026, 12, 28) and e == dt.date(2027, 1, 4)


def test_strip_meta_leaves_only_what_the_user_will_be_doing():
    assert away.strip_meta("16.08-24.08 кайт тиждень в Дахабі") == "кайт тиждень в Дахабі"
    assert away.strip_meta("на 7 днів лежатиму догори пупцем") == "лежатиму догори пупцем"


# ---------- validation ----------

def test_normalize_keeps_the_note_and_infers_the_kind():
    p = away.normalize("2026-08-16", "2026-08-24", None, "кайт щодня", today=TODAY)
    assert p == {"start_date": "2026-08-16", "end_date": "2026-08-24",
                 "kind": "sport", "note": "кайт щодня"}


def test_normalize_swaps_reversed_dates():
    p = away.normalize("2026-08-24", "2026-08-16", "rest", None, today=TODAY)
    assert p["start_date"] == "2026-08-16" and p["end_date"] == "2026-08-24"


def test_normalize_refuses_a_missing_end():
    with pytest.raises(away.AwayError):
        away.normalize("2026-08-16", None, "rest", None, today=TODAY)


def test_normalize_refuses_an_unbounded_period():
    """An "away" that never ends would mute the coach's compliance judgement forever —
    the one failure mode this feature must not create."""
    with pytest.raises(away.AwayError):
        away.normalize("2026-08-16", "2027-08-16", "rest", None, today=TODAY)


def test_normalize_truncates_an_overlong_note():
    p = away.normalize("2026-08-16", "2026-08-20", "rest", "x" * 500, today=TODAY)
    assert len(p["note"]) == away.MAX_NOTE_CHARS


def test_from_op_never_raises_on_model_junk():
    """A slip in a field nobody asked about must not sink the plan edit it rode along
    with — and it gets no more trust than a hand-typed period."""
    assert away.from_op(None) is None
    assert away.from_op({"start": "не дата", "end": "теж ні"}) is None
    assert away.from_op({"start": "2026-08-16", "end": "2027-08-16"}) is None   # too long
    ok = away.from_op({"start": "2026-08-16", "end": "2026-08-24", "note": "кайт"})
    assert ok["kind"] == "sport"


# ---------- overlap maths ----------

def test_days_in_range_counts_only_the_overlap():
    p = _period("2026-08-13", "2026-08-19")
    assert away.days_in_range(p, dt.date(2026, 8, 10), dt.date(2026, 8, 16)) == 4
    assert away.days_in_range(p, dt.date(2026, 9, 1), dt.date(2026, 9, 7)) == 0


def test_status_and_current():
    active = _period("2026-08-14", "2026-08-20")
    future = _period("2026-09-01", "2026-09-07")
    assert away.status(active, TODAY) == "active"
    assert away.status(future, TODAY) == "upcoming"
    assert away.status(_period("2026-07-01", "2026-07-07"), TODAY) == "past"
    assert away.current([future, active], TODAY) == active


# ---------- prompt context ----------

def test_to_context_is_none_for_someone_who_never_declared_one():
    """Absent, not present-and-empty: a user without periods must get byte-for-byte the
    prompts they got before this feature existed (the same rule as the coach profile)."""
    assert away.to_context([], TODAY) is None


def test_to_context_carries_the_note_and_the_expectation():
    ctx = away.to_context([_period("2026-08-14", "2026-08-20", "sport", "кайт щодня")], TODAY)
    p = ctx["periods"][0]
    assert p["status"] == "active" and p["days_left"] == 4 and p["note"] == "кайт щодня"
    assert p["expect"] == away.expectation("sport")


def test_to_context_measures_the_week_being_judged():
    """`days_in_week` is what stops the digest reading a deliberate zero as a failure."""
    ctx = away.to_context(
        [_period("2026-08-12", "2026-08-18")], TODAY,
        week_start=dt.date(2026, 8, 10), week_end=dt.date(2026, 8, 16))
    assert ctx["periods"][0]["days_in_week"] == 5


def test_to_context_keeps_a_just_finished_period():
    """The Sunday digest runs after the week it judges — "he got back on Wednesday"
    explains the week's shape as much as being away does."""
    ctx = away.to_context([_period("2026-08-01", "2026-08-10")], TODAY)
    assert ctx["periods"][0]["status"] == "past"
    assert ctx["periods"][0]["days_since_end"] == 6


def test_to_context_drops_ancient_history():
    assert away.to_context([_period("2026-01-01", "2026-01-10")], TODAY) is None


# ---------- storage ----------

async def test_save_get_current_and_delete(session):
    u = await _user(session)
    data = away.normalize("2026-08-14", "2026-08-20", "sport", "кайт", today=TODAY)
    row = await away_db.save(session, u.id, data)
    await session.commit()

    assert await away_db.get_current(session, u.id, TODAY) is not None
    assert await away_db.get_current(session, u.id, dt.date(2026, 9, 1)) is None
    assert await away_db.delete(session, u.id, row.id) is True
    await session.commit()
    assert await away_db.list_periods(session, u.id) == []


async def test_saving_the_same_period_twice_updates_instead_of_stacking(session):
    """Declaring a trip through /plan and then also through /away is a normal thing to do;
    two overlapping rows would make `days_in_week` meaningless."""
    u = await _user(session, "dup@example.com")
    data = away.normalize("2026-08-14", "2026-08-20", "rest", None, today=TODAY)
    await away_db.save(session, u.id, data)
    await session.commit()
    await away_db.save(session, u.id, {**data, "kind": "sport", "note": "кайт"})
    await session.commit()

    rows = await away_db.list_periods(session, u.id)
    assert len(rows) == 1 and rows[0]["kind"] == "sport" and rows[0]["note"] == "кайт"


async def test_periods_are_user_scoped(session):
    a = await _user(session, "a@example.com")
    b = await _user(session, "b@example.com")
    await away_db.save(session, a.id,
                       away.normalize("2026-08-14", "2026-08-20", "rest", None, today=TODAY))
    await session.commit()
    assert await away_db.list_periods(session, b.id) == []
    assert await away_db.get_current(session, b.id, TODAY) is None


async def test_list_periods_since_keeps_a_still_running_period(session):
    """A trip that started last month but is still running must survive the `since` filter —
    otherwise the coach forgets the athlete is away halfway through."""
    u = await _user(session, "run@example.com")
    await away_db.save(session, u.id,
                       away.normalize("2026-07-20", "2026-08-20", "rest", None, today=TODAY))
    await session.commit()
    assert len(await away_db.list_periods(session, u.id, since=TODAY)) == 1


# ---------- the surfaces ----------

async def test_digest_context_carries_the_declared_week(session):
    """The bug in one test: a declared vacation must reach the digest that judges the week."""
    today = dt.date.today()
    session.add(ActivityRecord(user_id=U1, activity_id=9001, date=today.isoformat(),
                               type="running", dist_km=6.0))
    await session.commit()
    await away_db.save(session, U1, away.normalize(
        today - dt.timedelta(days=2), today + dt.timedelta(days=2), "sport",
        "кайт тиждень", today=today))
    await session.commit()

    stats = CallStats(kind="digest", model=service.MODEL_DIGEST)
    with patch.object(reports, "digest_with_stats",
                      return_value=("підсумок", stats)) as m:
        await reports.run_digest(session, user_id=U1)

    ctx = m.call_args.args[0]
    assert ctx["away"] is not None
    p = ctx["away"]["periods"][0]
    assert p["kind"] == "sport" and p["note"] == "кайт тиждень"
    assert p["days_in_week"] >= 1


async def test_digest_context_has_no_away_field_without_a_period(session):
    today = dt.date.today()
    session.add(ActivityRecord(user_id=U1, activity_id=9002, date=today.isoformat(),
                               type="running", dist_km=6.0))
    await session.commit()

    stats = CallStats(kind="digest", model=service.MODEL_DIGEST)
    with patch.object(reports, "digest_with_stats", return_value=("підсумок", stats)) as m:
        await reports.run_digest(session, user_id=U1)
    assert m.call_args.args[0]["away"] is None


async def test_daily_report_context_carries_the_period(session):
    today = dt.date.today()
    await away_db.save(session, U1, away.normalize(
        today, today + dt.timedelta(days=5), "active", "гори", today=today))
    await session.commit()

    captured = {}

    def fake_analyze(payload, question="", deep=False, kind=None, previous_report=None,
                     api_key=None, weather=None, plan_today=None, fitness=None,
                     records=None, norm=None, subjective=None, health_alerts=None,
                     fueling=None, today=None, intensity_ctx=None,
                     athlete_profile=None, away_ctx=None):
        captured["away"] = away_ctx
        return "звіт", CallStats(kind=kind or "report", model="m")

    with patch.object(reports, "analyze_with_stats", fake_analyze):
        await reports.run_analysis(
            session, {"daily": [], "recent_activities": [], "planned_runs": []},
            user_id=U1, today=today)

    assert captured["away"]["periods"][0]["note"] == "гори"


async def test_declaring_a_period_busts_the_report_dedup_cache(session):
    """Otherwise telling the coach about the trip would change the prompt but not the hash,
    and the day's report would keep coming back pre-vacation."""
    today = dt.date.today()
    payload = {"daily": [], "recent_activities": [], "planned_runs": []}
    calls = []

    def fake_analyze(payload, question="", deep=False, kind=None, previous_report=None,
                     api_key=None, weather=None, plan_today=None, fitness=None,
                     records=None, norm=None, subjective=None, health_alerts=None,
                     fueling=None, today=None, intensity_ctx=None,
                     athlete_profile=None, away_ctx=None):
        calls.append(away_ctx)
        return "звіт", CallStats(kind=kind or "report", model="m")

    with patch.object(reports, "analyze_with_stats", fake_analyze):
        await reports.run_analysis(session, payload, user_id=U1, today=today)
        await away_db.save(session, U1, away.normalize(
            today, today + dt.timedelta(days=3), "rest", None, today=today))
        await session.commit()
        await reports.run_analysis(session, payload, user_id=U1, today=today)

    assert len(calls) == 2 and calls[0] is None and calls[1] is not None


def test_the_away_block_reaches_every_advising_prompt():
    """One wording, appended in one place — the digest divergence started as two prompts
    knowing different things, and the injury/health advisories repeated it: a real signal
    delivered as "прибери tempo/intervals/long, можу перебудувати план" to someone with
    nothing scheduled that week."""
    from app.analysis import prompts

    for p in (prompts.SYSTEM, prompts.SYSTEM_DIGEST, prompts.SYSTEM_PLAN,
              prompts.SYSTEM_PLAN_ADAPT, prompts.SYSTEM_PLAN_EDIT, prompts.SYSTEM_SICK,
              prompts.SYSTEM_ASK_TOOLS, prompts.SYSTEM_INJURY, prompts.SYSTEM_HEALTH,
              prompts.SYSTEM_WEATHER_PLAN):
        assert prompts.AWAY_BLOCK in p


def test_the_away_block_forbids_advice_the_athlete_cannot_act_on():
    """The warning may be right and its action still useless — the block has to say that
    removing sessions and rebuilding the plan are not the advice for someone mid-trip."""
    from app.analysis import prompts

    assert "перебудувати план" in prompts.AWAY_BLOCK
    assert "tempo/intervals/long" in prompts.AWAY_BLOCK


async def test_injury_advisory_context_carries_the_period(session):
    """The exact bug the user hit: HRV below baseline during a declared kite week, advised
    as if there were tempo sessions to cancel."""
    from app import injury
    from app.analysis import reports as reports_mod

    today = dt.date.today()
    await away_db.save(session, U1, away.normalize(
        today - dt.timedelta(days=1), today + dt.timedelta(days=5), "sport",
        "кайт щодня", today=today))
    await session.commit()

    daily = [{"date": (today - dt.timedelta(days=13 - i)).isoformat(),
              "hrv_avg": 30, "acwr_pct": 150, "sleep_score": 60, "resting_hr": 55}
             for i in range(14)]
    assessment = injury.assess(daily, [], history_days=60)
    stats = CallStats(kind="injury", model=service.MODEL_INJURY)
    with patch.object(reports_mod, "injury_with_stats",
                      return_value=("бережи себе", stats)) as m:
        await reports_mod.run_injury_check(
            session, user_id=U1, assessment=assessment, api_key="k")

    ctx = m.call_args.args[0]
    assert ctx["away"]["periods"][0]["note"] == "кайт щодня"


async def test_health_alert_context_carries_the_period(session):
    from app import health
    from app.analysis import reports as reports_mod

    today = dt.date.today()
    await away_db.save(session, U1, away.normalize(
        today, today + dt.timedelta(days=3), "rest", "лежатиму", today=today))
    await session.commit()

    history = [{"date": (today - dt.timedelta(days=29 - i)).isoformat(),
                "hrv_avg": 60 if i < 25 else 30, "sleep_score": 80, "resting_hr": 50}
               for i in range(30)]
    report = health.detect(history, min_history_days=7)
    assert report.actionable          # the signal is real; only the ADVICE needs context
    stats = CallStats(kind="health", model=service.MODEL_HEALTH)
    with patch.object(reports_mod, "health_with_stats",
                      return_value=("спокійніше", stats)) as m:
        await reports_mod.run_health_alert(session, user_id=U1, report=report, api_key="k")

    assert m.call_args.args[0]["away"]["periods"][0]["note"] == "лежатиму"


def test_the_away_block_forbids_the_catch_up_advice():
    """Piling the missed volume onto the return week is how people get hurt after a break;
    the prompt has to say so, not merely stay quiet."""
    from app.analysis import prompts

    assert "наздоганяти" in prompts.AWAY_BLOCK or "наздогнати" in prompts.AWAY_BLOCK
    assert "days_in_week" in prompts.AWAY_BLOCK


# ---------- the nudges that must stay quiet ----------

async def test_sickness_nudge_is_silent_during_a_declared_absence(session, monkeypatch):
    """Three missed sessions during a kite week is not an illness to repair — asking is
    exactly the nag this feature exists to stop."""
    from bot import jobs as jobs_module

    u = await _user(session, "sick@example.com")
    u.telegram_chat_id = 42
    await session.commit()
    today = dt.date.today()
    await away_db.save(session, u.id, away.normalize(
        today - dt.timedelta(days=2), today + dt.timedelta(days=4), "sport", "кайт",
        today=today))
    await session.commit()

    async def _boom(*a, **kw):   # the detector must never even be reached
        raise AssertionError("sickness detector ran during a declared absence")

    monkeypatch.setattr(jobs_module.repository, "recent_plan_workouts", _boom)
    sent = await jobs_module._sickness_check_for_user(
        SimpleNamespace(bot=None), session, u, SimpleNamespace(anthropic_key="k"),
        today.isoformat())
    assert sent is False


# ---------- the plan-edit round trip ----------

async def test_plan_edit_away_is_stored_with_the_proposal_and_applied_on_confirm(session):
    """"Зсунь тренування, я у відпустці 16-24.08" is one decision: ❌ leaves no trace,
    ✅ records the trip even though it is not a plan operation."""
    from app.garmin import repository

    u = await _user(session, "edit@example.com")
    proposed = away.from_op({"start": "2026-08-16", "end": "2026-08-24",
                             "kind": "sport", "note": "кайт"})
    await repository.set_pending_plan_edit(session, u.id, [], [], away=proposed)

    pending = await repository.pop_pending_plan_edit(session, u.id)
    assert pending["away"] == proposed

    saved = await away_db.apply_pending(session, u.id, pending)
    assert saved == proposed
    rows = await away_db.list_periods(session, u.id)
    assert len(rows) == 1 and rows[0]["note"] == "кайт"


async def test_a_refined_proposal_keeps_the_declared_trip(session):
    """A correction ("краще 8 км") returns a fresh operation set and says nothing about the
    trip again — the period must not evaporate on the second turn."""
    from app.garmin import repository

    u = await _user(session, "refine@example.com")
    proposed = away.from_op({"start": "2026-08-16", "end": "2026-08-24", "note": "кайт"})
    await repository.set_pending_plan_edit(session, u.id, [{"action": "skip"}], [],
                                            away=proposed)
    pending = await repository.get_pending_plan_edit(session, u.id)

    # what bot.handlers._plan_edit computes for a refinement whose PlanEdit has no `away`
    carried = away.from_op(None) or (pending or {}).get("away")
    assert carried == proposed


async def test_a_cancelled_proposal_records_nothing(session):
    from app.garmin import repository

    u = await _user(session, "cancel@example.com")
    await repository.set_pending_plan_edit(
        session, u.id, [], [],
        away=away.from_op({"start": "2026-08-16", "end": "2026-08-24"}))
    # ❌ pops the pending state and applies nothing.
    await repository.pop_pending_plan_edit(session, u.id)
    assert await away_db.list_periods(session, u.id) == []


# ---------- the /away command ----------

class _Msg:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)
        return SimpleNamespace(chat_id=1, message_id=1)


@asynccontextmanager
async def _fake_session_maker(session):
    yield session


async def _run_away(session, user, args):
    """Drive ``/away`` with the handler's session/user resolution stubbed out."""
    from bot import handlers

    msg = _Msg()
    update = SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=42))
    ctx = SimpleNamespace(args=args)
    with patch.object(handlers, "async_session_maker",
                      lambda: _fake_session_maker(session)), \
         patch.object(handlers, "_resolve_user", AsyncReturn(user)):
        await handlers.away_cmd(update, ctx)
    return msg.replies


class AsyncReturn:
    """A stand-in for an async function returning a fixed value."""

    def __init__(self, value):
        self.value = value

    async def __call__(self, *a, **kw):
        return self.value


async def test_away_command_records_dates_kind_and_note(session):
    u = await _user(session, "cmd@example.com")
    replies = await _run_away(session, u, ["16.08-24.08", "кайт", "тиждень"])
    assert "Записав" in replies[0]

    rows = await away_db.list_periods(session, u.id, since=dt.date(2026, 1, 1))
    assert len(rows) == 1
    assert rows[0]["kind"] == "sport" and "кайт" in rows[0]["note"]


async def test_away_command_without_args_explains_itself(session):
    u = await _user(session, "help@example.com")
    replies = await _run_away(session, u, [])
    assert "/away" in replies[0]


async def test_away_command_rejects_a_period_it_cannot_parse(session):
    u = await _user(session, "bad@example.com")
    replies = await _run_away(session, u, ["колись", "влітку"])
    assert "Не зрозумів" in replies[0] or "Потрібна дата" in replies[0]
    assert await away_db.list_periods(session, u.id, since=dt.date(2026, 1, 1)) == []


# ---------- the web form ----------

def test_profile_page_declares_and_deletes_a_period(auth_client):
    """The third door onto the same validator — /me/profile is where "what the coach knows
    about me" already lives, and this is the half the athlete writes."""
    today = dt.date.today()
    end = (today + dt.timedelta(days=6)).isoformat()
    r = auth_client.post("/me/away", data={
        "start": today.isoformat(), "end": end, "kind": "sport",
        "note": "кайт у Дахабі"}, follow_redirects=True)
    assert r.status_code == 200
    assert "кайт у Дахабі" in r.text

    row_id = _away_row_id(r.text)
    r = auth_client.post(f"/me/away/{row_id}/delete", follow_redirects=True)
    assert "кайт у Дахабі" not in r.text


def test_profile_page_reports_a_refused_period_instead_of_storing_it(auth_client):
    today = dt.date.today()
    r = auth_client.post("/me/away", data={
        "start": today.isoformat(), "end": "", "kind": "rest", "note": ""},
        follow_redirects=True)
    assert r.status_code == 200
    assert "Потрібна дата" in r.text
    assert _away_row_id(r.text) is None


def _away_row_id(html: str):
    """The id of the last declared period rendered on the page (or None)."""
    import re as _re

    ids = _re.findall(r'action="/me/away/(\d+)/delete"', html)
    return int(ids[-1]) if ids else None
