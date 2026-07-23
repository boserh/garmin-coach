"""Web smoke tests: the /me per-user browser and the data export ZIP."""


from tests.web_helpers import _report_id, _seed_two_users_with_data, _seed_user


def test_me_requires_login(client):
    assert client.get("/me", follow_redirects=False).status_code == 303


def test_me_shows_only_own_data(client):
    _seed_two_users_with_data()
    client.post("/login", data={"email": "alice@example.com", "password": "pw"})

    assert client.get("/me").status_code == 200
    rl = client.get("/me/report_logs")
    assert rl.status_code == 200
    assert "alice report" in rl.text
    assert "bob report" not in rl.text          # other user's data not visible

    # user-facing browser exposes only the three data tables
    assert client.get("/me/users").status_code == 404
    assert client.get("/me/bot_state").status_code == 404


def test_me_row_isolation(client):
    aid, bid = _seed_two_users_with_data()
    client.post("/login", data={"email": "alice@example.com", "password": "pw"})

    assert client.get(f"/me/report_logs/{_report_id(aid)}").status_code == 200
    # alice cannot open bob's row
    assert client.get(f"/me/report_logs/{_report_id(bid)}").status_code == 404


def test_me_index_links_to_export(client):
    _seed_two_users_with_data()
    client.post("/login", data={"email": "alice@example.com", "password": "pw"})
    assert "/me/export" in client.get("/me").text


def test_me_export_requires_login(client):
    assert client.get("/me/export", follow_redirects=False).status_code == 303


def test_me_export_zip_scoped_to_own_user_no_secrets(client):
    """NF-13 AC: the ZIP contains only the logged-in user's rows, no secrets anywhere
    (users table is never touched), and every JSON file parses."""
    import json
    import zipfile
    from io import BytesIO

    aid, bid = _seed_two_users_with_data()
    client.post("/login", data={"email": "alice@example.com", "password": "pw"})

    r = client.get("/me/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(BytesIO(r.content))
    names = set(zf.namelist())
    assert names == {
        "daily_metrics.json", "daily_metrics.csv", "activities.json", "activities.csv",
        "personal_records.json", "plans.json", "report_logs.json",
    }

    daily = json.loads(zf.read("daily_metrics.json"))
    assert all(d["hrv_avg"] == 55 for d in daily) and len(daily) >= 1  # only alice's own row(s)
    reports = json.loads(zf.read("report_logs.json"))
    assert reports and all(r["report_text"] == "alice report" for r in reports)

    raw = zf.read("daily_metrics.json") + zf.read("report_logs.json")
    for secret in (b"garth_token", b"password_hash", b"anthropic_key", b"bob report"):
        assert secret not in raw


def test_me_export_empty_history_is_valid_zip(client):
    _seed_user(email="fresh@example.com", password="pw", is_admin=False)
    client.post("/login", data={"email": "fresh@example.com", "password": "pw"})

    import json
    import zipfile
    from io import BytesIO

    r = client.get("/me/export")
    assert r.status_code == 200
    zf = zipfile.ZipFile(BytesIO(r.content))
    assert json.loads(zf.read("daily_metrics.json")) == []
    assert json.loads(zf.read("activities.json")) == []
    assert json.loads(zf.read("plans.json")) == []


# ---- ST-15: manual resync ----

def _seed_activity(user_id, activity_id=111):
    import anyio

    from app.db.base import async_session_maker
    from app.db.models import ActivityRecord

    async def seed():
        async with async_session_maker() as s:
            s.add(ActivityRecord(user_id=user_id, activity_id=activity_id,
                                 date="2026-06-21", type="running"))
            await s.commit()

    anyio.run(seed)


def _activity_row_id(user_id):
    import anyio
    from sqlalchemy import select

    from app.db.base import async_session_maker
    from app.db.models import ActivityRecord

    async def get():
        async with async_session_maker() as s:
            return (await s.execute(
                select(ActivityRecord.id).where(ActivityRecord.user_id == user_id)
            )).scalar_one()

    return anyio.run(get)


def test_resync_other_users_activity_404(client):
    """ST-15 AC: resyncing an activity that isn't yours → 404 (never touches it)."""
    aid, bid = _seed_two_users_with_data()
    _seed_activity(bid)                     # bob owns the activity
    bob_row = _activity_row_id(bid)
    client.post("/login", data={"email": "alice@example.com", "password": "pw"})
    r = client.post(f"/me/activities/{bob_row}/resync", follow_redirects=False)
    assert r.status_code == 404


def test_resync_days_range_cap_rejected(client):
    """ST-15 AC: a range over 31 days is rejected with a human banner, no Garmin call."""
    _seed_two_users_with_data()
    client.post("/login", data={"email": "alice@example.com", "password": "pw"})
    r = client.post("/me/resync-days",
                    data={"date_from": "2026-01-01", "date_to": "2026-12-31"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "resync_error=range" in r.headers["location"]


def test_resync_days_bad_date_rejected(client):
    _seed_two_users_with_data()
    client.post("/login", data={"email": "alice@example.com", "password": "pw"})
    r = client.post("/me/resync-days", data={"date_from": "not-a-date"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "resync_error=format" in r.headers["location"]


def test_resync_days_requires_login(client):
    r = client.post("/me/resync-days", data={"date_from": "2026-06-21"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


# ---- strength exercise rows: reps + weight display ----

def test_exercise_rows_formats_reps_and_weight():
    from app.routers.me import _exercise_rows

    ex = {"active_sets": 5, "sets": {
        "присідання": {"count": 2, "reps": [12, 12], "weight_kg": [22.0, 22.0]},
        "утримання": {"count": 1, "reps": [None], "weight_kg": [None]},
        "жим": {"count": 2, "reps": [10, 12], "weight_kg": [50.0, 55.0]},
    }}
    by = {r["name"]: r["detail"] for r in _exercise_rows(ex)}
    assert by["присідання"] == "2×12 · 22 кг"
    assert by["утримання"] == "1 підх. · власна вага"   # no reps, bodyweight
    assert by["жим"] == "2×10–12 · 50–55 кг"            # varying reps + weight → ranges


def test_exercise_rows_legacy_count_only_shape():
    from app.routers.me import _exercise_rows

    rows = _exercise_rows({"active_sets": 4, "sets": {"присідання": 4}})
    assert rows == [{"name": "присідання", "detail": "4"}]


def test_exercise_rows_empty():
    from app.routers.me import _exercise_rows

    assert _exercise_rows(None) == []
    assert _exercise_rows({}) == []


