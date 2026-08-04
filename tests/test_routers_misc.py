"""Web smoke tests: health/status/history, /ui browse, activities, /info, reports."""
from unittest.mock import AsyncMock, patch

import anyio

from app.db import users
from app.db.base import async_session_maker
from tests.web_helpers import _user_id


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_status_requires_login(client):
    assert client.get("/status", follow_redirects=False).status_code == 303


def test_status(auth_client):
    r = auth_client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["garmin_login"] == "ok"
    assert "cost_usd_total" in body
    # OPS-05: error counters present (0 when nothing has failed)
    assert body["garmin_errors_24h"] == 0
    assert body["garmin_errors_breakdown"] == {}
    # ST-18: incomplete-day counter present
    assert body["incomplete_days_30d"] == 0
    # OPS-04: last-morning-job field present (None until a tick has run)
    assert "last_morning_job" in body
    # OPS-08: admin-only backup-freshness fields; no backups/ dir in the test env → None
    assert body["backup_age_hours"] is None
    assert body["backup_rsync_ok"] is None


def test_status_shows_backup_age_for_admin(auth_client, tmp_path, monkeypatch):
    import json
    import time as _t

    from app.backup_status import MARKER_NAME
    from app.core.config import settings

    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path), raising=False)
    (tmp_path / MARKER_NAME).write_text(
        json.dumps({"ts": _t.time() - 7200, "path": "x", "size": 1, "rsync_ok": True})
    )
    r = auth_client.get("/status")
    body = r.json()
    assert body["backup_age_hours"] == 2.0
    assert body["backup_rsync_ok"] is True


def test_me_jobs_page_renders(auth_client):
    r = auth_client.get("/me/jobs")
    assert r.status_code == 200
    assert "Фонові задачі" in r.text


def test_admin_jobs_page_renders(auth_client):
    # auth_client is an admin (see /ui tests) → /admin/jobs is reachable
    r = auth_client.get("/admin/jobs")
    assert r.status_code == 200
    assert "Фонові задачі" in r.text


def test_history_requires_login(client):
    assert client.get("/history", follow_redirects=False).status_code == 303


def test_history_when_logged_in(auth_client):
    r = auth_client.get("/history")
    assert r.status_code == 200
    assert "trend" in r.json()


def test_ui_requires_login(client):
    # unauthenticated → redirect to /login
    r = client.get("/ui", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_ui_browse(auth_client):
    assert auth_client.get("/ui").status_code == 200
    r = auth_client.get("/ui/daily_metrics")
    assert r.status_code == 200
    assert "daily_metrics" in r.text
    assert auth_client.get("/ui/nope").status_code == 404


def test_activities_minimal_index_and_run_chart(auth_client):
    from app.garmin import repository

    uid = _user_id("t@example.com")
    series = [{"d": 0.0, "p": 7.0, "hr": 120}, {"d": 0.5, "p": 6.5, "hr": 140},
              {"d": 1.0, "p": 6.0, "hr": 150}]

    async def seed():
        async with async_session_maker() as s:
            await repository.upsert_activity(s, uid, 999, {
                "date": "2026-06-24", "type": "running", "dur_min": 30.0,
                "dist_km": 5.0, "avg_hr": 140, "max_hr": 155, "load": 80.0,
                "series": series,
            })
            await s.commit()

    anyio.run(seed)

    def _row_id():
        from sqlalchemy import select

        from app.db.models import ActivityRecord

        async def get():
            async with async_session_maker() as s:
                return (await s.execute(
                    select(ActivityRecord.id).where(ActivityRecord.activity_id == 999)
                )).scalar_one()

        return anyio.run(get)

    # list view: minimal columns — heavy fields are not column headers
    lst = auth_client.get("/ui/activities").text
    assert "<th>dist_km</th>" in lst
    assert "<th>series</th>" not in lst
    assert "<th>load</th>" not in lst

    # detail view: pace + HR charts rendered, raw series not dumped
    detail = auth_client.get(f"/ui/activities/{_row_id()}").text
    assert "<polyline" in detail
    assert "Темп, хв/км" in detail
    assert "Пульс" in detail
    # hover tooltip: per-point data embedded + the mousemove handler present
    assert "data-pts=" in detail
    assert "mousemove" in detail


def test_ui_table_column_filters(auth_client):
    """/ui's real per-column filters (dropdowns + date range), not the old
    substring-only search box."""
    from app.garmin import repository

    uid = _user_id("t@example.com")

    async def seed():
        async with async_session_maker() as s:
            for i, (aid, date, atype) in enumerate([
                (9001, "2026-06-01", "running"),
                (9002, "2026-06-05", "cycling"),
                (9003, "2026-06-10", "running"),
            ]):
                await repository.upsert_activity(s, uid, aid, {
                    "date": date, "type": atype, "dur_min": 30.0 + i,
                })
            await s.commit()

    anyio.run(seed)

    # a dropdown is offered for the low-cardinality "type" column
    page = auth_client.get("/ui/activities").text
    assert 'name="f_type"' in page
    assert '<option value="running">running</option>' in page

    # exact-match dropdown filter narrows the rows to just that type — the
    # dropdown itself still lists every option (so it doesn't disappear once
    # picked), so assert on a *row*, not the presence of "cycling" anywhere.
    filtered = auth_client.get("/ui/activities?f_type=running")
    assert filtered.status_code == 200
    assert "2026-06-01" in filtered.text and "2026-06-10" in filtered.text
    assert "2026-06-05" not in filtered.text  # the cycling row's date

    # date-range filter excludes rows outside the window
    ranged = auth_client.get("/ui/activities?date_from=2026-06-06")
    assert "2026-06-10" in ranged.text
    assert "2026-06-01" not in ranged.text

    # column filter + text search compose (AND, not OR)
    combo = auth_client.get("/ui/activities?f_type=running&search=nope-no-match")
    assert "з 0" in combo.text or "1–0" in combo.text or "0 з 0" in combo.text

    # a table with no "date" column skips the date-range inputs entirely
    no_date = auth_client.get("/ui/bot_state")
    assert no_date.status_code == 200
    assert 'name="date_from"' not in no_date.text


def test_ui_table_user_filter(auth_client):
    """A dedicated "Користувач" dropdown (email labels) on every table with a
    user_id column — user_id itself is excluded from the generic enum scan
    (raw ids aren't a meaningful label), so it needs its own filter."""
    uid = _user_id("t@example.com")

    page = auth_client.get("/ui/daily_metrics").text
    assert 'name="f_user_id"' in page
    assert f'<option value="{uid}">t@example.com</option>' in page

    # filtering by a nonexistent user_id returns zero rows, not an error
    r = auth_client.get("/ui/daily_metrics?f_user_id=999999")
    assert r.status_code == 200
    assert "з 0" in r.text or "0 з 0" in r.text

    # the users table has no user_id column — no such filter offered there
    users_page = auth_client.get("/ui/users").text
    assert 'name="f_user_id"' not in users_page


def test_activity_analysis_shown_on_detail(auth_client):
    from app.garmin import repository

    uid = _user_id("t@example.com")

    async def seed():
        async with async_session_maker() as s:
            await repository.upsert_activity(s, uid, 777, {
                "date": "2026-06-23", "type": "running", "dist_km": 5.0})
            act = await repository.get_activity(
                s, uid, (await repository.list_activities(s, uid, 1))[0]["id"])
            act.analysis = "🏃 Рівний легкий біг, пульс у нормі."
            await s.commit()
            return act.id

    rid = anyio.run(seed)
    detail = auth_client.get(f"/ui/activities/{rid}").text
    assert "Аналіз" in detail
    assert "Рівний легкий біг" in detail
    # the raw analysis column is not also dumped as a plain field row
    assert "<th>analysis</th>" not in detail


def test_info_requires_login(client):
    assert client.get("/info", follow_redirects=False).status_code == 303


def test_admin_clears_bot_state(auth_client):
    from app.garmin import repository

    uid = _user_id("t@example.com")

    async def seed():
        async with async_session_maker() as s:
            await repository.set_state(s, uid, "morning_sent_date", "2026-06-24")

    anyio.run(seed)
    assert "morning_sent_date" in auth_client.get("/ui/bot_state").text

    r = auth_client.post(
        "/ui/bot_state/delete",
        data={"user_id": str(uid), "key": "morning_sent_date"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    async def check():
        async with async_session_maker() as s:
            return await repository.get_state(s, uid, "morning_sent_date")

    assert anyio.run(check) is None


def test_info_when_logged_in(auth_client):
    r = auth_client.get("/info")
    assert r.status_code == 200
    assert "Як це працює" in r.text


def test_ui_daily_metrics_has_trend_chart(auth_client):
    import datetime as dt

    from app.garmin import repository
    from app.garmin.schemas import DailySummary

    async def seed():
        async with async_session_maker() as s:
            user = await users.get_by_email(s, "t@example.com")
            today = dt.date.today()
            for i, hrv in enumerate([58, 61, 55, 63]):
                d = (today - dt.timedelta(days=i)).isoformat()
                await repository.upsert_daily(
                    s, user.id, DailySummary(date=d, hrv_avg=hrv, sleep_h=7.0 + i * 0.2,
                                             sleep_score=80, has_data=True)
                )
            await s.commit()

    anyio.run(seed)
    body = auth_client.get("/ui/daily_metrics").text
    assert 'class="charts"' in body
    assert "HRV avg" in body
    assert "<polyline" in body
    # other tables get no chart
    assert 'class="charts"' not in auth_client.get("/ui/report_logs").text


def test_report_json_uses_shared_helper_and_stale_note(auth_client):
    """CODE-05: /report.json funnels through delivery.build_report, forwards the report
    question, and sources its stale note from delivery.STALE_NOTE. Patch the helper +
    payload build so no Garmin/Claude call happens."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.analysis import delivery
    from app.routers import reports as reports_router

    payload = SimpleNamespace(synced_today=False, last_data_date="2026-07-08")
    captured = {}

    async def fake_build_report(session, user, pl, *, question, kind, api_key=None, weather=None):
        captured.update(question=question, kind=kind, payload=pl)
        return delivery.ReportResult(
            text="звіт тут", synced_today=pl.synced_today, last_data_date=pl.last_data_date,
        )

    @asynccontextmanager
    async def fake_runtime(session, user):
        yield SimpleNamespace(anthropic_key="k")  # skip decrypt (creds pollution across tests)

    with patch.object(reports_router, "user_runtime", fake_runtime), \
         patch.object(reports_router.service, "build_payload_cached",
                      AsyncMock(return_value=(payload, []))), \
         patch.object(reports_router.delivery, "build_report", new=fake_build_report):
        r = auth_client.get("/report.json")

    assert r.status_code == 200
    body = r.json()
    assert body["report"] == "звіт тут"
    assert body["synced_today"] is False
    assert body["last_data_date"] == "2026-07-08"
    assert body["note"] == delivery.STALE_NOTE          # stale wording sourced from the helper
    assert captured["question"] == reports_router._REPORT_Q  # question forwarded through
    assert captured["payload"] is payload


def test_report_json_forwards_weather(auth_client):
    """ST-03: /report.json passes the user's forecast (via weather.forecast_for_user)
    through to the shared helper, so on-demand reports are weather-aware too."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.analysis import delivery
    from app.routers import reports as reports_router

    payload = SimpleNamespace(synced_today=True, last_data_date="2026-07-11")
    wx = {"summary": "ясно", "t_min_c": 14, "t_max_c": 27}
    captured = {}

    async def fake_build_report(session, user, pl, *, question, kind, api_key=None, weather=None):
        captured.update(weather=weather)
        return delivery.ReportResult(
            text="звіт", synced_today=pl.synced_today, last_data_date=pl.last_data_date,
        )

    @asynccontextmanager
    async def fake_runtime(session, user):
        yield SimpleNamespace(anthropic_key="k")

    with patch.object(reports_router, "user_runtime", fake_runtime), \
         patch.object(reports_router.service, "build_payload_cached",
                      AsyncMock(return_value=(payload, []))), \
         patch.object(reports_router.weather, "forecast_for_user",
                      AsyncMock(return_value=wx)), \
         patch.object(reports_router.delivery, "build_report", new=fake_build_report):
        r = auth_client.get("/report.json")

    assert r.status_code == 200
    assert captured["weather"] == wx  # forecast forwarded to the analysis
