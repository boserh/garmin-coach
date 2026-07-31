"""Web smoke tests for the /checkups tab (health checkups / lab results): create, list,
edit, delete, per-user isolation, and the on-demand Claude interpretation route."""
from unittest.mock import patch

from tests.web_helpers import _seed_user, _user_id


def test_checkups_requires_login(client):
    assert client.get("/checkups", follow_redirects=False).status_code == 303


def test_checkups_empty_state(auth_client):
    assert "Ще немає жодного запису" in auth_client.get("/checkups").text


def test_create_list_and_view_checkup(auth_client):
    r = auth_client.post(
        "/checkups",
        data={
            "date": "2026-07-15",
            "title": "Загальний аналіз крові",
            "category": "кров",
            "result_name": ["Феритин", ""],
            "result_value": ["45", ""],
            "result_unit": ["нг/мл", ""],
            "result_ref": ["30-400", ""],
            "notes": "все в нормі",
            "next_due_date": "2027-01-15",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/checkups?saved=1"

    listing = auth_client.get("/checkups").text
    assert "Загальний аналіз крові" in listing
    assert "2026-07-15" in listing
    assert "1 показник" in listing

    # find the created row's id via the DB directly (no id in the redirect)
    import anyio

    from app.db import checkups as checkups_db
    from app.db.base import async_session_maker

    uid = _user_id("t@example.com")

    async def get_id():
        async with async_session_maker() as s:
            rows = await checkups_db.list_checkups(s, uid)
            return next(r.id for r in rows if r.title == "Загальний аналіз крові")

    cid = anyio.run(get_id)

    detail = auth_client.get(f"/checkups/{cid}").text
    assert "Феритин" in detail and "45" in detail and "нг/мл" in detail and "30-400" in detail
    assert "все в нормі" in detail
    assert "2027-01-15" in detail


def test_update_checkup(auth_client):
    auth_client.post(
        "/checkups",
        data={"date": "2026-07-01", "title": "Огляд лікаря"},
    )
    import anyio

    from app.db import checkups as checkups_db
    from app.db.base import async_session_maker

    uid = _user_id("t@example.com")

    async def get_id():
        async with async_session_maker() as s:
            rows = await checkups_db.list_checkups(s, uid)
            return next(r.id for r in rows if r.title == "Огляд лікаря")

    cid = anyio.run(get_id)

    r = auth_client.post(
        f"/checkups/{cid}",
        data={"date": "2026-07-02", "title": "Огляд лікаря (повторно)"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    detail = auth_client.get(f"/checkups/{cid}").text
    assert "Огляд лікаря (повторно)" in detail and "2026-07-02" in detail


def test_missing_required_fields_redirects_with_error(auth_client):
    r = auth_client.post("/checkups", data={"date": "", "title": ""}, follow_redirects=False)
    assert r.status_code == 303 and "err=required" in r.headers["location"]


def test_delete_checkup(auth_client):
    auth_client.post("/checkups", data={"date": "2026-07-01", "title": "Тимчасовий"})
    import anyio

    from app.db import checkups as checkups_db
    from app.db.base import async_session_maker

    uid = _user_id("t@example.com")

    async def get_id():
        async with async_session_maker() as s:
            rows = await checkups_db.list_checkups(s, uid)
            return next(r.id for r in rows if r.title == "Тимчасовий")

    cid = anyio.run(get_id)
    r = auth_client.post(f"/checkups/{cid}/delete", follow_redirects=False)
    assert r.status_code == 303
    # a deleted (or never-owned) id just bounces back to the list, no 500
    assert auth_client.get(f"/checkups/{cid}").status_code == 200
    assert "Тимчасовий" not in auth_client.get("/checkups").text


def _get_id_by_title(uid, title):
    import anyio

    from app.db import checkups as checkups_db
    from app.db.base import async_session_maker

    async def get_id():
        async with async_session_maker() as s:
            rows = await checkups_db.list_checkups(s, uid)
            return next(r.id for r in rows if r.title == title)

    return anyio.run(get_id)


def _fake_creds():
    """/checkups/{id}/analyze needs a truthy ``creds.anthropic_key`` — patch
    ``load_credentials`` directly rather than round-tripping through real Fernet
    encryption (which needs a configured APP_SECRET_KEY the test env doesn't set)."""
    return patch("app.routers.checkups.load_credentials",
                return_value=type("C", (), {"anthropic_key": "test-key"})())


def test_analyze_route_stores_and_shows_text(auth_client):
    from app.analysis import reports
    from app.analysis.client import CallStats

    auth_client.post(
        "/checkups",
        data={"date": "2026-07-15", "title": "Аналіз на аналіз",
              "result_name": ["Феритин"], "result_value": ["45"],
              "result_unit": ["нг/мл"], "result_ref": ["30-400"]},
    )
    uid = _user_id("t@example.com")
    cid = _get_id_by_title(uid, "Аналіз на аналіз")

    def fake_with_stats(context, api_key=None):
        return "🔬 усе в нормі", CallStats(kind="checkup", model="m")

    with _fake_creds(), patch.object(reports, "checkup_with_stats", fake_with_stats):
        r = auth_client.post(f"/checkups/{cid}/analyze", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/checkups/{cid}?analyzed=1"

    detail = auth_client.get(f"/checkups/{cid}").text
    assert "усе в нормі" in detail
    assert "Розібрати ще раз" in detail   # button relabels once analysis exists


def test_analyze_route_no_claude_key_redirects(auth_client):
    auth_client.post("/checkups", data={"date": "2026-07-15", "title": "Без ключа"})
    uid = _user_id("t@example.com")
    cid = _get_id_by_title(uid, "Без ключа")

    with patch("app.routers.checkups.load_credentials",
              return_value=type("C", (), {"anthropic_key": None})()):
        r = auth_client.post(f"/checkups/{cid}/analyze", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/checkups/{cid}?err=nokey"


def test_analyze_route_analyst_error_redirects(auth_client):
    from app.analysis import reports
    from app.analysis.service import AnalystError

    auth_client.post("/checkups", data={"date": "2026-07-15", "title": "Помилка API"})
    uid = _user_id("t@example.com")
    cid = _get_id_by_title(uid, "Помилка API")

    def failing_with_stats(context, api_key=None):
        raise AnalystError("боом")

    with _fake_creds(), patch.object(reports, "checkup_with_stats", failing_with_stats):
        r = auth_client.post(f"/checkups/{cid}/analyze", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/checkups/{cid}?err=analyze"


def test_editing_a_checkup_clears_stale_analysis(auth_client):
    from app.analysis import reports
    from app.analysis.client import CallStats

    auth_client.post("/checkups", data={"date": "2026-07-15", "title": "Стара версія"})
    uid = _user_id("t@example.com")
    cid = _get_id_by_title(uid, "Стара версія")

    def fake_with_stats(context, api_key=None):
        return "стара інтерпретація", CallStats(kind="checkup", model="m")

    with _fake_creds(), patch.object(reports, "checkup_with_stats", fake_with_stats):
        auth_client.post(f"/checkups/{cid}/analyze")
    assert "стара інтерпретація" in auth_client.get(f"/checkups/{cid}").text

    auth_client.post(f"/checkups/{cid}", data={"date": "2026-07-16", "title": "Нова версія"})
    detail = auth_client.get(f"/checkups/{cid}").text
    assert "стара інтерпретація" not in detail
    assert "Проаналізувати результати" not in detail  # no results yet, so no button at all


def test_checkups_are_isolated_per_user(client):
    _seed_user(email="alice@example.com", password="pw", is_admin=False)
    _seed_user(email="bob@example.com", password="pw", is_admin=False)

    client.post("/login", data={"email": "alice@example.com", "password": "pw"})
    client.post("/checkups", data={"date": "2026-07-01", "title": "Alice's checkup"})
    client.post("/logout")

    client.post("/login", data={"email": "bob@example.com", "password": "pw"})
    bob_list = client.get("/checkups").text
    assert "Alice's checkup" not in bob_list

    import anyio

    from app.db import checkups as checkups_db
    from app.db.base import async_session_maker

    aid = _user_id("alice@example.com")

    async def alice_checkup_id():
        async with async_session_maker() as s:
            rows = await checkups_db.list_checkups(s, aid)
            return next(r.id for r in rows if r.title == "Alice's checkup")

    cid = anyio.run(alice_checkup_id)
    # bob (still logged in) can't view or delete alice's record by guessing its id
    r = client.get(f"/checkups/{cid}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/checkups"
    client.post(f"/checkups/{cid}/delete")

    async def still_there():
        async with async_session_maker() as s:
            return await checkups_db.get_checkup(s, aid, cid)

    assert anyio.run(still_there) is not None
