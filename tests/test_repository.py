"""Repository upsert idempotency, per-user isolation, and history reads."""
import datetime as dt

from sqlalchemy import func, select

from app.db.models import ActivityRecord, DailyMetric, PlannedWorkout, ReportLog, TrainingPlan
from app.garmin import repository
from app.garmin.schemas import DailySummary, Payload

U1, U2 = 1, 2  # FK enforcement is off in SQLite, so bare ids are fine for unit tests


async def _count(session, model):
    return (await session.execute(select(func.count(model.id)))).scalar_one()


async def test_upsert_daily_is_idempotent(session):
    s1 = DailySummary(date="2026-06-20", sleep_score=80, hrv_avg=55,
                      stress_avg=20, has_data=True)
    await repository.upsert_daily(session, U1, s1)
    await session.commit()

    # same (user, date), new values → update in place, not a second row
    s2 = DailySummary(date="2026-06-20", sleep_score=90, hrv_avg=58,
                      stress_avg=22, has_data=True)
    await repository.upsert_daily(session, U1, s2)
    await session.commit()

    assert await _count(session, DailyMetric) == 1
    got = await repository.read_daily_metrics(session, U1, ["2026-06-20"])
    assert got["2026-06-20"].sleep_score == 90
    assert got["2026-06-20"].hrv_avg == 58


async def test_extra_json_round_trips(session):
    await repository.upsert_daily(session, U1, DailySummary(
        date="2026-06-25", hrv_avg=50, has_data=True,
        extra={"resting_hr": 50, "readiness_score": 63, "acwr_pct": 100}))
    await session.commit()
    got = await repository.read_daily_metrics(session, U1, ["2026-06-25"])
    assert got["2026-06-25"].extra == {"resting_hr": 50, "readiness_score": 63, "acwr_pct": 100}


async def test_daily_metrics_isolated_per_user(session):
    await repository.upsert_daily(
        session, U1, DailySummary(date="2026-06-20", hrv_avg=55, has_data=True))
    await repository.upsert_daily(
        session, U2, DailySummary(date="2026-06-20", hrv_avg=70, has_data=True))
    await session.commit()

    # same date, two users → two rows, each sees only its own
    assert await _count(session, DailyMetric) == 2
    got1 = await repository.read_daily_metrics(session, U1, ["2026-06-20"])
    got2 = await repository.read_daily_metrics(session, U2, ["2026-06-20"])
    assert got1["2026-06-20"].hrv_avg == 55
    assert got2["2026-06-20"].hrv_avg == 70


async def test_upsert_activity_is_idempotent(session):
    row = {"date": "2026-06-20", "type": "strength_training", "dur_min": 45.0,
           "dist_km": 0.0, "avg_hr": 110, "max_hr": 140, "load": 60.0,
           "exercises": {"active_sets": 12, "sets": {"присідання": 4}}}
    await repository.upsert_activity(session, U1, 111, row)
    await session.commit()

    row2 = dict(row, load=75.0)
    await repository.upsert_activity(session, U1, 111, row2)
    await session.commit()

    assert await _count(session, ActivityRecord) == 1
    rec = (await session.execute(
        select(ActivityRecord).where(ActivityRecord.activity_id == 111)
    )).scalar_one()
    assert rec.load == 75.0
    assert rec.exercises == {"active_sets": 12, "sets": {"присідання": 4}}


async def test_upsert_activity_skips_when_no_id(session):
    rec = await repository.upsert_activity(session, U1, None, {"date": "2026-06-20"})
    await session.commit()
    assert rec is None
    assert await _count(session, ActivityRecord) == 0


async def test_upsert_activity_returns_record_only_when_new(session):
    row = {"date": "2026-06-20", "type": "running", "dist_km": 5.0}
    rec = await repository.upsert_activity(session, U1, 500, row)
    await session.commit()
    assert rec is not None
    assert rec.activity_id == 500

    # an update to the same activity_id is NOT reported as new
    rec2 = await repository.upsert_activity(session, U1, 500, dict(row, dist_km=5.5))
    await session.commit()
    assert rec2 is None


async def test_persist_payload_returns_only_new_activities(session):
    payload = Payload(
        generated="2026-06-24T08:00", window_days=1, synced_today=False,
        daily=[], recent_activities=[], planned_runs=[],
    )
    act_pairs = [
        (100, {"date": "2026-06-24", "type": "running", "dist_km": 5.0}),
        (200, {"date": "2026-06-24", "type": "running", "dist_km": 3.0}),
    ]
    new_activities = await repository.persist_payload(session, U1, payload, act_pairs)
    await session.commit()
    assert {a.activity_id for a in new_activities} == {100, 200}

    # persisting the same pairs again yields no new activities (idempotent re-fetch)
    new_again = await repository.persist_payload(session, U1, payload, act_pairs)
    await session.commit()
    assert new_again == []


async def test_log_report_stores_text(session):
    await repository.log_report(
        session, user_id=U1, kind="report", model="claude-sonnet-4-6",
        input_tokens=10, output_tokens=5, cost_usd=0.001, ok=True,
        report_text="🟢 hello report",
    )
    rows = (await session.execute(select(ReportLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].report_text == "🟢 hello report"
    assert rows[0].user_id == U1


async def test_get_last_report(session):
    import datetime as dt

    from sqlalchemy import select

    from app.db.models import ReportLog

    assert await repository.get_last_report(session, U1) is None
    # failed calls and null-text rows are ignored
    await repository.log_report(session, user_id=U1, kind="report", model="m",
                                ok=False, error="boom")
    # today's report is NOT day-over-day context (keeps the dedup key stable on
    # repeated same-day /report), and /deep is excluded entirely
    await repository.log_report(session, user_id=U1, kind="report", model="m",
                                ok=True, report_text="сьогоднішній")
    await repository.log_report(session, user_id=U1, kind="deep", model="m",
                                ok=True, report_text="глибокий")
    assert await repository.get_last_report(session, U1) is None

    # a daily report from yesterday IS the context
    await repository.log_report(
        session, user_id=U1, kind="morning", model="m", ok=True,
        report_text="🟢 учора все ок")
    yesterday = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    row = (await session.execute(
        select(ReportLog).where(ReportLog.report_text == "🟢 учора все ок")
    )).scalar_one()
    row.created_at = yesterday
    await session.commit()

    # another user's report must not leak in
    await repository.log_report(
        session, user_id=U2, kind="report", model="m", ok=True, report_text="чужий")

    text, date = await repository.get_last_report(session, U1)
    assert text == "🟢 учора все ок"
    assert date == yesterday.date().isoformat()


async def test_typical_run_pace(session):
    # no runs yet → None (the estimate then falls back to its own default)
    assert await repository.typical_run_pace(session, U1) is None

    today = dt.date.today().isoformat()
    # three runs at 6/7/8 min/km → median 7.0; a walk and a garbage-pace run are ignored
    for i, (km, dur) in enumerate([(5.0, 30.0), (5.0, 35.0), (5.0, 40.0)]):
        session.add(ActivityRecord(
            user_id=U1, activity_id=900 + i, date=today, type="running",
            dist_km=km, dur_min=dur))
    session.add(ActivityRecord(  # walk — not a run, excluded
        user_id=U1, activity_id=950, date=today, type="walking",
        dist_km=3.0, dur_min=45.0))
    session.add(ActivityRecord(  # 2.0 min/km is below the sanity floor, excluded
        user_id=U1, activity_id=951, date=today, type="running",
        dist_km=5.0, dur_min=10.0))
    # another user's fast run must not leak in
    session.add(ActivityRecord(
        user_id=U2, activity_id=952, date=today, type="running",
        dist_km=5.0, dur_min=20.0))
    await session.commit()

    assert abs(await repository.typical_run_pace(session, U1) - 7.0) < 0.01


async def test_get_recent_reports_filters_and_orders(session):
    await repository.log_report(session, user_id=U1, kind="report", model="m",
                                ok=False, error="x")
    await repository.log_report(session, user_id=U1, kind="deep", model="m", ok=True,
                                report_text="глибокий розбір")
    await repository.log_report(session, user_id=U1, kind="ask", model="m", ok=True,
                                report_text="відповідь на питання")
    await repository.log_report(session, user_id=U1, kind="report", model="m", ok=True,
                                report_text="звіт A")
    await repository.log_report(session, user_id=U1, kind="report", model="m", ok=True,
                                report_text="звіт B")
    await repository.log_report(session, user_id=U2, kind="report", model="m", ok=True,
                                report_text="чужий звіт")

    recent = await repository.get_recent_reports(session, U1, n=3)
    texts = [r["text"] for r in recent]
    assert texts == ["звіт B", "звіт A"]  # deep/ask/failed/other-user excluded
    assert all("date" in r for r in recent)


async def test_get_recent_asks(session):
    # only this user's successful asks, with question + answer, within the window
    await repository.log_report(session, user_id=U1, kind="report", model="m", ok=True,
                                question="звіт?", report_text="звіт — не ask")
    await repository.log_report(session, user_id=U1, kind="ask", model="m", ok=True,
                                question="чи бігти?", report_text="так, легко")
    await repository.log_report(session, user_id=U1, kind="ask", model="m", ok=False,
                                question="впав", error="boom")  # failed → excluded
    await repository.log_report(session, user_id=U2, kind="ask", model="m", ok=True,
                                question="чуже", report_text="чужа відповідь")

    asks = await repository.get_recent_asks(session, U1, minutes=5)
    assert asks == [{"question": "чи бігти?", "answer": "так, легко"}]

    # nothing in a zero-minute window (everything is older than 'now')
    assert await repository.get_recent_asks(session, U1, minutes=0) == []


async def test_list_and_get_activity_scoped(session):
    await repository.upsert_activity(session, U1, 111, {
        "date": "2026-06-20", "type": "running", "dist_km": 5.0, "dur_min": 30.0, "avg_hr": 140})
    await repository.upsert_activity(session, U1, 222, {
        "date": "2026-06-22", "type": "cycling", "dist_km": 20.0, "dur_min": 60.0, "avg_hr": 130})
    await repository.upsert_activity(session, U2, 333, {
        "date": "2026-06-22", "type": "running", "dist_km": 3.0})
    await session.commit()

    lst = await repository.list_activities(session, U1, n=5)
    assert [a["type"] for a in lst] == ["cycling", "running"]  # newest first, U1 only

    rid = lst[0]["id"]
    act = await repository.get_activity(session, U1, rid)
    assert act is not None and act.user_id == U1
    assert await repository.get_activity(session, U2, rid) is None  # not theirs


async def test_state_is_per_user(session):
    await repository.set_state(session, U1, "morning_sent_date", "2026-06-22")
    await repository.set_state(session, U2, "morning_sent_date", "2026-06-21")
    assert await repository.get_state(session, U1, "morning_sent_date") == "2026-06-22"
    assert await repository.get_state(session, U2, "morning_sent_date") == "2026-06-21"
    assert await repository.get_state(session, U1, "missing") is None


# ---------- pending plan edit (EP-11: shared bot/web confirm state) ----------

_NO_EXTRAS = {"summary": None, "alt_summary": None, "risky": False}


async def test_pending_plan_edit_round_trips_and_is_single_use(session):
    ops = [{"action": "move", "date": "2026-07-01", "new_date": "2026-07-02"}]
    alt = [{"action": "modify", "date": "2026-07-01", "dist_km": 5.0}]
    await repository.set_pending_plan_edit(session, U1, ops, alt)

    got = await repository.pop_pending_plan_edit(session, U1)
    assert got == {"ops": ops, "alt": alt, **_NO_EXTRAS}

    # single-use: a second pop finds nothing
    assert await repository.pop_pending_plan_edit(session, U1) is None


async def test_pending_plan_edit_is_per_user(session):
    await repository.set_pending_plan_edit(session, U1, [{"action": "skip"}], [])
    assert await repository.pop_pending_plan_edit(session, U2) is None
    got = await repository.pop_pending_plan_edit(session, U1)
    assert got == {"ops": [{"action": "skip"}], "alt": [], **_NO_EXTRAS}


async def test_pending_plan_edit_defaults_alt_to_empty_list(session):
    await repository.set_pending_plan_edit(session, U1, [{"action": "skip"}])
    got = await repository.pop_pending_plan_edit(session, U1)
    assert got == {"ops": [{"action": "skip"}], "alt": [], **_NO_EXTRAS}


async def test_pending_plan_edit_stores_summary_and_risky(session):
    ops = [{"action": "modify", "date": "2026-07-01", "dist_km": 15.0}]
    await repository.set_pending_plan_edit(
        session, U1, ops, [{"action": "modify", "date": "2026-07-01", "dist_km": 10.0}],
        summary="Збільшити довгу до 15 км", alt_summary="Краще 10 км", risky=True,
    )
    got = await repository.get_pending_plan_edit(session, U1)
    assert got["summary"] == "Збільшити довгу до 15 км"
    assert got["alt_summary"] == "Краще 10 км"
    assert got["risky"] is True
    # peek doesn't clear it
    assert await repository.get_pending_plan_edit(session, U1) is not None
    popped = await repository.pop_pending_plan_edit(session, U1)
    assert popped["risky"] is True
    assert await repository.get_pending_plan_edit(session, U1) is None


# ---------- chat history (EP-11: shared bot/web transcript) ----------

async def test_get_chat_history_reads_ask_and_plan_edit_oldest_first(session):
    await repository.log_report(
        session, user_id=U1, kind="ask", model="claude-sonnet-5", ok=True,
        question="як мій сон?", report_text="Сон непоганий.",
    )
    await repository.log_report(
        session, user_id=U1, kind="plan_edit", model="claude-sonnet-5", ok=True,
        question="перенеси довгу на суботу", report_text="Переніс довгу на суботу.",
    )
    # a different kind never shows up in the chat thread
    await repository.log_report(
        session, user_id=U1, kind="report", model="claude-sonnet-5", ok=True,
        report_text="Щоденний звіт.",
    )
    # another user's turns never leak in
    await repository.log_report(
        session, user_id=U2, kind="ask", model="claude-sonnet-5", ok=True,
        question="інше питання", report_text="Інша відповідь.",
    )

    hist = await repository.get_chat_history(session, U1)
    assert [h["kind"] for h in hist] == ["ask", "plan_edit"]
    assert hist[0]["question"] == "як мій сон?" and hist[0]["answer"] == "Сон непоганий."
    assert hist[1]["answer"] == "Переніс довгу на суботу."


async def test_get_chat_history_renders_error_for_failed_turn(session):
    await repository.log_report(
        session, user_id=U1, kind="ask", model="claude-sonnet-5", ok=False,
        question="чому падає?", error="AnalystError: щось пішло не так",
    )
    hist = await repository.get_chat_history(session, U1)
    assert len(hist) == 1
    assert hist[0]["ok"] is False
    assert hist[0]["answer"] == "AnalystError: щось пішло не так"


async def test_read_history_orders_oldest_first(session):
    import datetime as dt
    today = dt.date.today()
    for i in (2, 0, 1):  # insert out of order
        d = (today - dt.timedelta(days=i)).isoformat()
        await repository.upsert_daily(
            session, U1, DailySummary(date=d, hrv_avg=50 + i, has_data=True)
        )
    await session.commit()

    trend = await repository.read_history(session, U1, days=7)
    dates = [r["date"] for r in trend]
    assert dates == sorted(dates)
    assert len(trend) == 3


async def test_read_history_surfaces_resting_hr_from_extra(session):
    import datetime as dt
    d = dt.date.today().isoformat()
    await repository.upsert_daily(session, U1, DailySummary(
        date=d, hrv_avg=55, has_data=True, extra={"resting_hr": 48}))
    await session.commit()
    trend = await repository.read_history(session, U1, days=2)
    assert trend[-1]["resting_hr"] == 48


async def test_get_recent_extra_coalesces_newest_per_key(session):
    import datetime as dt
    today = dt.date.today()
    # older day has race predictions; newer day has readiness but no race prediction
    await repository.upsert_daily(session, U1, DailySummary(
        date=(today - dt.timedelta(days=5)).isoformat(), has_data=True,
        extra={"race_5k_s": 1600, "vo2max": 46, "resting_hr": 50}))
    await repository.upsert_daily(session, U1, DailySummary(
        date=today.isoformat(), has_data=True,
        extra={"readiness_score": 70, "resting_hr": 48}))
    # a stale day outside the window must be ignored
    await repository.upsert_daily(session, U1, DailySummary(
        date=(today - dt.timedelta(days=40)).isoformat(), has_data=True,
        extra={"endurance_score": 999}))
    await session.commit()

    merged = await repository.get_recent_extra(session, U1, days=21)
    assert merged["race_5k_s"] == 1600        # only on the older (in-window) day
    assert merged["vo2max"] == 46
    assert merged["readiness_score"] == 70
    assert merged["resting_hr"] == 48          # newest day wins
    assert "endurance_score" not in merged     # 40 days ago → outside the window


async def test_read_fitness_history_keeps_per_day_series(session):
    import datetime as dt
    today = dt.date.today()
    d1 = (today - dt.timedelta(days=10)).isoformat()
    d2 = (today - dt.timedelta(days=3)).isoformat()
    d_stale = (today - dt.timedelta(days=200)).isoformat()
    await repository.upsert_daily(session, U1, DailySummary(
        date=d1, has_data=True, extra={"race_5k_s": 1620, "vo2max": 45}))
    await repository.upsert_daily(session, U1, DailySummary(
        date=d2, has_data=True, extra={"race_5k_s": 1600}))
    # a day with no fitness-trend keys at all contributes no row
    await repository.upsert_daily(session, U1, DailySummary(
        date=today.isoformat(), has_data=True, extra={"readiness_score": 70}))
    await repository.upsert_daily(session, U1, DailySummary(
        date=d_stale, has_data=True, extra={"race_5k_s": 2000}))
    await session.commit()

    rows = await repository.read_fitness_history(session, U1, days=120)
    assert [r["date"] for r in rows] == [d1, d2]   # oldest first, stale day excluded
    assert rows[0] == {"date": d1, "race_5k_s": 1620, "vo2max": 45}
    assert rows[1] == {"date": d2, "race_5k_s": 1600}


async def test_weekly_run_volume_groups_by_iso_week(session):
    import collections
    import datetime as dt
    today = dt.date.today()
    runs = {
        today.isoformat(): ("running", 5.0),
        (today - dt.timedelta(days=1)).isoformat(): ("trail_running", 8.0),
        (today - dt.timedelta(days=14)).isoformat(): ("running", 10.0),
    }
    aid = 1
    for ds, (typ, km) in runs.items():
        await repository.upsert_activity(session, U1, aid, {"date": ds, "type": typ, "dist_km": km})
        aid += 1
    # a non-run and an out-of-window run must be excluded
    await repository.upsert_activity(session, U1, 90, {
        "date": today.isoformat(), "type": "cycling", "dist_km": 30.0})
    await repository.upsert_activity(session, U1, 91, {
        "date": (today - dt.timedelta(weeks=20)).isoformat(), "type": "running", "dist_km": 99.0})
    await session.commit()

    vol = await repository.weekly_run_volume(session, U1, weeks=8)
    assert vol == sorted(vol, key=lambda b: b["week"])          # oldest first
    assert round(sum(b["km"] for b in vol), 1) == 23.0          # cycling + old run excluded

    exp = collections.defaultdict(lambda: {"km": 0.0, "runs": 0, "longest": 0.0})
    for ds, (_t, km) in runs.items():
        w = dt.date.fromisoformat(ds).strftime("%G-W%V")
        exp[w]["km"] += km
        exp[w]["runs"] += 1
        exp[w]["longest"] = max(exp[w]["longest"], km)
    weeks = {b["week"]: b for b in vol}
    assert set(weeks) == set(exp)
    for w, e in exp.items():
        assert weeks[w]["km"] == round(e["km"], 1)
        assert weeks[w]["runs"] == e["runs"]
        assert weeks[w]["longest_km"] == round(e["longest"], 1)


# ---------- EP-09 /ask tool queries ----------

async def test_query_activities_filters_and_scopes(session):
    await repository.upsert_activity(session, U1, 1, {
        "date": "2026-06-01", "type": "running", "dist_km": 5.0, "dur_min": 30.0})
    await repository.upsert_activity(session, U1, 2, {
        "date": "2026-06-10", "type": "trail_running", "dist_km": 15.0, "dur_min": 100.0})
    await repository.upsert_activity(session, U1, 3, {
        "date": "2026-06-15", "type": "cycling", "dist_km": 40.0, "dur_min": 90.0})
    await repository.upsert_activity(session, U2, 4, {
        "date": "2026-06-10", "type": "running", "dist_km": 8.0, "dur_min": 45.0})
    await session.commit()

    all_u1 = await repository.query_activities(session, U1)
    assert {a["id"] for a in all_u1} == {1, 2, 3}  # scoped to U1, not U2's row 4
    assert all_u1[0]["date"] == "2026-06-15"  # newest first

    runs = await repository.query_activities(session, U1, type="running")
    assert {a["id"] for a in runs} == {1, 2}  # substring match incl. trail_running

    long_runs = await repository.query_activities(session, U1, type="running", min_dist_km=10)
    assert [a["id"] for a in long_runs] == [2]

    ranged = await repository.query_activities(
        session, U1, date_from="2026-06-05", date_to="2026-06-12")
    assert {a["id"] for a in ranged} == {2}

    got = next(a for a in all_u1 if a["id"] == 1)
    assert got["avg_pace_minkm"] == 6.0  # 30 min / 5 km
    assert "series" not in got  # compact row, no raw points


async def test_query_activities_caps_at_ask_max_rows(session, monkeypatch):
    # B1: ASK_MAX_ROWS is read internally by query_activities in the repository.core
    # submodule, so patch it at the definition site (patching the facade re-export wouldn't
    # reach the in-module reference).
    monkeypatch.setattr(repository.core, "ASK_MAX_ROWS", 3)
    for i in range(1, 6):
        await repository.upsert_activity(session, U1, i, {
            "date": f"2026-06-{i:02d}", "type": "running", "dist_km": 5.0})
    await session.commit()
    got = await repository.query_activities(session, U1, limit=100)  # over-limit clamps
    assert len(got) == 3


async def test_query_daily_whitelists_fields_and_orders_oldest_first(session):
    await repository.upsert_daily(session, U1, DailySummary(
        date="2026-06-01", sleep_score=70, hrv_avg=50, has_data=True,
        extra={"resting_hr": 48, "acwr_pct": 110}))
    await repository.upsert_daily(session, U1, DailySummary(
        date="2026-06-02", sleep_score=80, hrv_avg=55, has_data=True,
        extra={"resting_hr": 47}))
    await session.commit()

    rows = await repository.query_daily(session, U1)
    assert [r["date"] for r in rows] == ["2026-06-01", "2026-06-02"]  # oldest first
    assert rows[0]["resting_hr"] == 48
    assert rows[0]["acwr_pct"] == 110
    assert "acwr_pct" not in rows[1]  # absent, not null-filled

    only_hrv = await repository.query_daily(session, U1, fields=["hrv_avg", "not_a_real_field"])
    assert only_hrv[0] == {"date": "2026-06-01", "hrv_avg": 50}  # bogus field silently dropped


async def test_aggregate_weekly_run_km_matches_weekly_run_volume(session):
    today = dt.date.today()
    await repository.upsert_activity(session, U1, 1, {
        "date": today.isoformat(), "type": "running", "dist_km": 6.0})
    await repository.upsert_activity(session, U1, 2, {
        "date": (today - dt.timedelta(days=1)).isoformat(), "type": "running", "dist_km": 4.0})
    await session.commit()

    vol = await repository.weekly_run_volume(session, U1, weeks=4)
    agg = await repository.aggregate_weekly(session, U1, "run_km", weeks=4)
    assert agg["metric"] == "run_km"
    assert agg["weeks"] == [{"week": w["week"], "value": w["km"]} for w in vol]


async def test_aggregate_weekly_recovery_metric_averages_per_week(session):
    today = dt.date.today()
    await repository.upsert_daily(session, U1, DailySummary(
        date=today.isoformat(), hrv_avg=60, has_data=True))
    await repository.upsert_daily(session, U1, DailySummary(
        date=(today - dt.timedelta(days=1)).isoformat(), hrv_avg=40, has_data=True))
    await session.commit()

    agg = await repository.aggregate_weekly(session, U1, "hrv_avg", weeks=4)
    assert agg["metric"] == "hrv_avg"
    this_week = today.strftime("%G-W%V")
    week_entry = next(w for w in agg["weeks"] if w["week"] == this_week)
    # Both days share this ISO week only when today isn't a Monday; on a Monday, yesterday
    # (Sunday) belongs to the previous ISO week, so this week holds today's 60 alone.
    yesterday = today - dt.timedelta(days=1)
    if yesterday.strftime("%G-W%V") == this_week:
        assert week_entry["value"] == 50.0  # avg(60, 40)
    else:
        assert week_entry["value"] == 60.0  # only today's value falls in this week


async def test_aggregate_weekly_unknown_metric_is_a_soft_error(session):
    got = await repository.aggregate_weekly(session, U1, "not_a_metric")
    assert "error" in got


async def test_latest_daily_date(session):
    assert await repository.latest_daily_date(session, U1) is None
    await repository.upsert_daily(session, U1, DailySummary(date="2026-06-01", has_data=True))
    await repository.upsert_daily(session, U1, DailySummary(date="2026-06-05", has_data=True))
    await repository.upsert_daily(session, U2, DailySummary(date="2026-06-20", has_data=True))
    await session.commit()
    assert await repository.latest_daily_date(session, U1) == "2026-06-05"  # not U2's later date


async def test_query_training_plan_no_active_plan(session):
    assert await repository.query_training_plan(session, U1) == {"plan": None}


async def test_query_training_plan_returns_goal_and_sessions_in_range(session):
    plan = TrainingPlan(user_id=U1, goal="first_5k", goal_label="Перші 5К",
                        status="active", start_date="2026-06-01", target_date="2026-08-01",
                        days_per_week=3, intensity="easy", summary="плавний старт")
    session.add(plan)
    await session.flush()
    session.add(PlannedWorkout(plan_id=plan.id, user_id=U1, date="2026-06-01", week=1,
                               type="easy", dist_km=5.0, description="легко",
                               status="done"))
    session.add(PlannedWorkout(plan_id=plan.id, user_id=U1, date="2026-06-10", week=2,
                               type="tempo", dist_km=8.0, description="темпова",
                               status="planned"))
    # a different (archived) plan for the same user must not leak in
    other = TrainingPlan(user_id=U1, goal="g", status="archived",
                         start_date="2026-01-01", target_date="2026-02-01")
    session.add(other)
    await session.flush()
    session.add(PlannedWorkout(plan_id=other.id, user_id=U1, date="2026-06-05",
                               type="long", status="planned"))
    await session.commit()

    got = await repository.query_training_plan(session, U1)
    assert got["plan"]["goal"] == "first_5k"
    assert got["plan"]["goal_label"] == "Перші 5К"
    assert got["plan"]["target_date"] == "2026-08-01"
    assert [s["date"] for s in got["sessions"]] == ["2026-06-01", "2026-06-10"]

    ranged = await repository.query_training_plan(
        session, U1, date_from="2026-06-05", date_to="2026-06-30")
    assert [s["date"] for s in ranged["sessions"]] == ["2026-06-10"]

    assert await repository.query_training_plan(session, U2) == {"plan": None}


async def _make_plan(session, user_id: int, start: str, end: str) -> TrainingPlan:
    plan = TrainingPlan(user_id=user_id, goal="g", status="active",
                        start_date=start, target_date=end)
    session.add(plan)
    await session.flush()
    return plan


async def test_upcoming_plan_workouts_returns_window(session):
    today = dt.date.today()
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    past = (today - dt.timedelta(days=1)).isoformat()
    future = (today + dt.timedelta(days=3)).isoformat()

    plan = await _make_plan(session, 1, past, future)
    for d, t in [(past, "easy"), (today.isoformat(), "tempo"),
                 (tomorrow, "long"), (future, "rest")]:
        session.add(PlannedWorkout(plan_id=plan.id, user_id=1, date=d, type=t,
                                   status="planned"))
    await session.flush()

    ws = await repository.upcoming_plan_workouts(session, user_id=1, days=2)
    assert [w.date for w in ws] == [today.isoformat(), tomorrow]
    assert [w.type for w in ws] == ["tempo", "long"]


async def test_upcoming_plan_workouts_excludes_non_planned(session):
    today = dt.date.today().isoformat()
    plan = await _make_plan(session, 1, today, today)
    session.add(PlannedWorkout(plan_id=plan.id, user_id=1, date=today, type="easy",
                               status="completed"))
    session.add(PlannedWorkout(plan_id=plan.id, user_id=1, date=today, type="tempo",
                               status="planned"))
    await session.flush()

    ws = await repository.upcoming_plan_workouts(session, user_id=1, days=2)
    assert len(ws) == 1 and ws[0].type == "tempo"


async def test_upcoming_plan_workouts_user_scoped(session):
    today = dt.date.today().isoformat()
    plan1 = await _make_plan(session, 1, today, today)
    plan2 = await _make_plan(session, 2, today, today)
    session.add(PlannedWorkout(plan_id=plan1.id, user_id=1, date=today, type="easy",
                               status="planned"))
    session.add(PlannedWorkout(plan_id=plan2.id, user_id=2, date=today, type="tempo",
                               status="planned"))
    await session.flush()

    ws1 = await repository.upcoming_plan_workouts(session, user_id=1, days=2)
    ws2 = await repository.upcoming_plan_workouts(session, user_id=2, days=2)
    assert len(ws1) == 1 and ws1[0].type == "easy"
    assert len(ws2) == 1 and ws2[0].type == "tempo"


async def test_upcoming_plan_workouts_no_plan(session):
    ws = await repository.upcoming_plan_workouts(session, user_id=99, days=2)
    assert ws == []


async def test_month_cost_sums_current_month_only(session):
    assert await repository.month_cost(session, U1) == 0.0

    await repository.log_report(session, user_id=U1, kind="report", model="m", cost_usd=0.01)
    await repository.log_report(session, user_id=U1, kind="deep", model="m", cost_usd=0.02)
    # a different user's cost never counts
    await repository.log_report(session, user_id=U2, kind="report", model="m", cost_usd=5.0)
    await session.commit()
    assert await repository.month_cost(session, U1) == 0.03

    # a call from last month is excluded
    row = (await session.execute(
        select(ReportLog).where(ReportLog.user_id == U1, ReportLog.kind == "deep")
    )).scalar_one()
    this_month_start = dt.datetime.now(dt.timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    row.created_at = this_month_start - dt.timedelta(days=1)
    await session.commit()
    assert await repository.month_cost(session, U1) == 0.01


# ---------- costs_for_month (ST-12) ----------

def _this_month_bounds_utc():
    start = dt.datetime.now(dt.timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    end = dt.datetime(
        start.year + (start.month == 12), (start.month % 12) + 1, 1, tzinfo=dt.timezone.utc)
    return start, end


async def test_costs_for_month_aggregates_by_kind_and_top3(session):
    start, end = _this_month_bounds_utc()

    await repository.log_report(session, user_id=U1, kind="report", model="m", cost_usd=0.01)
    await repository.log_report(session, user_id=U1, kind="report", model="m", cost_usd=0.02)
    await repository.log_report(session, user_id=U1, kind="deep", model="m", cost_usd=0.05)
    await repository.log_report(
        session, user_id=U1, kind="report", model="m", cost_usd=0.0, cached=True)
    # a different user and a call outside the window never count
    await repository.log_report(session, user_id=U2, kind="report", model="m", cost_usd=9.0)
    await repository.log_report(session, user_id=U1, kind="report", model="m", cost_usd=1.0)
    outside = (await session.execute(
        select(ReportLog).where(ReportLog.user_id == U1, ReportLog.cost_usd == 1.0)
    )).scalar_one()
    outside.created_at = start - dt.timedelta(days=1)
    await session.commit()

    agg = await repository.costs_for_month(session, U1, start, end)
    assert agg["total_usd"] == 0.08
    assert agg["calls"] == 4
    assert agg["cached"] == 1
    assert agg["by_kind"]["report"] == {"cost": 0.03, "calls": 3}
    assert agg["by_kind"]["deep"] == {"cost": 0.05, "calls": 1}
    assert agg["top3"][0]["kind"] == "deep"
    assert agg["top3"][0]["cost"] == 0.05
    # the cached (cost=0) row never appears in top3
    assert all(t["cost"] > 0 for t in agg["top3"])


async def test_costs_for_month_empty_when_no_calls(session):
    start, end = _this_month_bounds_utc()
    agg = await repository.costs_for_month(session, U1, start, end)
    assert agg == {"total_usd": 0.0, "calls": 0, "cached": 0, "by_kind": {}, "top3": []}


# ---------- recent_step_match (NF-14) ----------

async def test_recent_step_match_only_includes_scored_matches(session):
    plan = TrainingPlan(user_id=U1, goal="g", status="active",
                        start_date="2026-06-01", target_date="2026-09-01")
    session.add(plan)
    await session.flush()

    scored = ActivityRecord(id=101, user_id=U1, activity_id=9101, date="2026-07-05",
                            type="running", dist_km=8.0, dur_min=40.0,
                            step_match={"steps_hit": 6, "steps_total": 8, "misses": []})
    unscored = ActivityRecord(id=102, user_id=U1, activity_id=9102, date="2026-07-08",
                              type="running", dist_km=5.0, dur_min=25.0)
    session.add_all([scored, unscored])
    session.add(PlannedWorkout(plan_id=plan.id, user_id=U1, date="2026-07-05", week=1,
                               type="tempo", status="done", completed_activity_id=101))
    session.add(PlannedWorkout(plan_id=plan.id, user_id=U1, date="2026-07-08", week=1,
                               type="easy", status="done", completed_activity_id=102))
    await session.commit()

    rows = await repository.recent_step_match(session, plan.id)
    assert rows == [{"date": "2026-07-05", "steps_hit": 6, "steps_total": 8}]


async def test_recent_step_match_respects_days_window(session):
    plan = TrainingPlan(user_id=U1, goal="g", status="active",
                        start_date="2026-01-01", target_date="2026-09-01")
    session.add(plan)
    await session.flush()

    old = ActivityRecord(id=201, user_id=U1, activity_id=9201, date="2026-01-05",
                         type="running", dist_km=8.0, dur_min=40.0,
                         step_match={"steps_hit": 4, "steps_total": 8, "misses": []})
    session.add(old)
    session.add(PlannedWorkout(plan_id=plan.id, user_id=U1, date="2026-01-05", week=1,
                               type="tempo", status="done", completed_activity_id=201))
    await session.commit()

    assert await repository.recent_step_match(session, plan.id, days=30) == []


async def test_recent_step_match_empty_without_matches(session):
    plan = TrainingPlan(user_id=U1, goal="g", status="active",
                        start_date="2026-06-01", target_date="2026-09-01")
    session.add(plan)
    await session.flush()
    await session.commit()
    assert await repository.recent_step_match(session, plan.id) == []
