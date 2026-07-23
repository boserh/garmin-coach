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


