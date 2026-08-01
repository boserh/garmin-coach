"""Web smoke tests for the /checkups tab (health checkups / lab results): create, list,
edit, delete, per-user isolation, and the on-demand Claude interpretation route."""
from unittest.mock import patch

import pytest
from starlette.websockets import WebSocketDisconnect

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
    assert "✏️ Редагувати" in detail  # edit form is a collapsed <details>, not always shown
    assert "oor-row" not in detail  # 45 is within 30-400 — nothing to flag


def test_checkup_detail_highlights_minor_out_of_range_result(auth_client):
    """A value just past the edge (30-400, value 15 -> 15 below lo out of a 370-wide
    range, ~4%) is flagged "minor" (orange), not "major" (red)."""
    import anyio

    from app.db import checkups as checkups_db
    from app.db.base import async_session_maker

    auth_client.post(
        "/checkups",
        data={
            "date": "2026-07-15", "title": "Незначне відхилення",
            "result_name": ["Феритин"], "result_value": ["15"],
            "result_unit": ["нг/мл"], "result_ref": ["30-400"],
        },
    )
    uid = _user_id("t@example.com")

    async def get_id():
        async with async_session_maker() as s:
            rows = await checkups_db.list_checkups(s, uid)
            return next(r.id for r in rows if r.title == "Незначне відхилення")

    cid = anyio.run(get_id)
    detail = auth_client.get(f"/checkups/{cid}").text
    assert "oor-row oor-minor" in detail and 'class="oor oor-minor"' in detail
    assert "oor-major" not in detail


def test_checkup_detail_highlights_major_out_of_range_result(auth_client):
    """A value well past the edge (30-400, value 1000) is flagged "major" (red)."""
    import anyio

    from app.db import checkups as checkups_db
    from app.db.base import async_session_maker

    auth_client.post(
        "/checkups",
        data={
            "date": "2026-07-15", "title": "Значне відхилення",
            "result_name": ["Феритин"], "result_value": ["1000"],
            "result_unit": ["нг/мл"], "result_ref": ["30-400"],
        },
    )
    uid = _user_id("t@example.com")

    async def get_id():
        async with async_session_maker() as s:
            rows = await checkups_db.list_checkups(s, uid)
            return next(r.id for r in rows if r.title == "Значне відхилення")

    cid = anyio.run(get_id)
    detail = auth_client.get(f"/checkups/{cid}").text
    assert "oor-row oor-major" in detail and 'class="oor oor-major"' in detail
    assert "oor-minor" not in detail


def test_checkup_detail_shows_and_serves_attachments(auth_client):
    import anyio

    from app.db import checkups as checkups_db
    from app.db.base import async_session_maker

    uid = _user_id("t@example.com")

    async def make_checkup_with_attachment():
        async with async_session_maker() as s:
            c = await checkups_db.create_checkup(s, uid, date="2026-07-15", title="З файлом")
            a = await checkups_db.add_attachment(
                s, c.id, filename="lab.jpg", media_type="image/jpeg", data=b"fake-jpg-bytes")
            return c.id, a.id

    cid, aid = anyio.run(make_checkup_with_attachment)

    detail = auth_client.get(f"/checkups/{cid}").text
    assert f"/checkups/{cid}/attachments/{aid}" in detail
    assert "lab.jpg" in detail

    r = auth_client.get(f"/checkups/{cid}/attachments/{aid}")
    assert r.status_code == 200
    assert r.content == b"fake-jpg-bytes"
    assert r.headers["content-type"] == "image/jpeg"
    assert "lab.jpg" in r.headers["content-disposition"]


def test_checkup_attachment_missing_id_redirects(auth_client):
    import anyio

    from app.db import checkups as checkups_db
    from app.db.base import async_session_maker

    uid = _user_id("t@example.com")

    async def make_checkup():
        async with async_session_maker() as s:
            c = await checkups_db.create_checkup(s, uid, date="2026-07-15", title="Без файлу")
            return c.id

    cid = anyio.run(make_checkup)
    r = auth_client.get(f"/checkups/{cid}/attachments/999999", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/checkups/{cid}"


def test_checkup_attachment_isolated_per_user(client):
    from app.db import checkups as checkups_db
    from app.db.base import async_session_maker

    _seed_user(email="alice2@example.com", password="pw", is_admin=False)
    _seed_user(email="bob2@example.com", password="pw", is_admin=False)
    alice_uid = _user_id("alice2@example.com")

    async def make_alice_attachment():
        async with async_session_maker() as s:
            c = await checkups_db.create_checkup(s, alice_uid, date="2026-07-15", title="Alice")
            a = await checkups_db.add_attachment(
                s, c.id, filename="secret.jpg", media_type="image/jpeg", data=b"alice-only")
            return c.id, a.id

    import anyio
    cid, aid = anyio.run(make_alice_attachment)

    client.post("/login", data={"email": "bob2@example.com", "password": "pw"})
    r = client.get(f"/checkups/{cid}/attachments/{aid}", follow_redirects=False)
    # bob doesn't own the checkup at all -> get_checkup returns None -> bounced to /checkups
    assert r.status_code == 303 and r.headers["location"] == "/checkups"


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


def test_upload_route_spawns_one_job_for_a_batch_of_files(auth_client):
    """Up to CHECKUP_UPLOAD_BATCH_MAX files in one submission become ONE job — one
    Claude call for the whole batch, not one per file."""
    from app.routers import checkups as checkups_router

    with _fake_creds(), patch.object(checkups_router, "_spawn_upload_job") as spawn:
        r = auth_client.post(
            "/checkups/upload",
            files=[
                ("file", ("lab1.jpg", b"bytes1", "image/jpeg")),
                ("file", ("lab2.pdf", b"bytes2", "application/pdf")),
            ],
            follow_redirects=False,
        )
    job_ids: list = []
    try:
        assert r.status_code == 303
        assert spawn.call_count == 1
        location = r.headers["location"]
        assert location.startswith("/checkups?jobs=")
        job_ids = location.split("=", 1)[1].split(",")
        assert len(job_ids) == 1
        job = checkups_router._upload_jobs[job_ids[0]]
        assert job.status == "queued"
        assert job.filenames == ["lab1.jpg", "lab2.pdf"]
    finally:
        for jid in job_ids:
            checkups_router._upload_jobs.pop(jid, None)


def test_upload_route_splits_more_than_batch_max_into_multiple_jobs(auth_client):
    from app.routers import checkups as checkups_router

    n = checkups_router.CHECKUP_UPLOAD_BATCH_MAX + 2
    files = [("file", (f"lab{i}.jpg", b"bytes", "image/jpeg")) for i in range(n)]
    with _fake_creds(), patch.object(checkups_router, "_spawn_upload_job") as spawn:
        r = auth_client.post("/checkups/upload", files=files, follow_redirects=False)
    job_ids: list = []
    try:
        assert spawn.call_count == 2  # BATCH_MAX + 2 -> one full batch, one partial
        job_ids = r.headers["location"].split("=", 1)[1].split(",")
        assert len(job_ids) == 2
        sizes = sorted(len(checkups_router._upload_jobs[jid].filenames) for jid in job_ids)
        assert sizes == [2, checkups_router.CHECKUP_UPLOAD_BATCH_MAX]
    finally:
        for jid in job_ids:
            checkups_router._upload_jobs.pop(jid, None)


def test_upload_route_bad_filetype_creates_error_job_without_spawning(auth_client):
    from app.routers import checkups as checkups_router

    with _fake_creds(), patch.object(checkups_router, "_spawn_upload_job") as spawn:
        r = auth_client.post(
            "/checkups/upload",
            files={"file": ("notes.txt", b"hello", "text/plain")},
            follow_redirects=False,
        )
    assert spawn.call_count == 0
    job_id = r.headers["location"].split("=", 1)[1]
    job = checkups_router._upload_jobs.pop(job_id)
    assert job.status == "error"
    assert "формат" in job.error.lower()


def test_upload_route_oversized_creates_error_job_without_spawning(auth_client):
    from app.routers import checkups as checkups_router

    big = b"x" * (15 * 1024 * 1024 + 1)
    with _fake_creds(), patch.object(checkups_router, "_spawn_upload_job") as spawn:
        r = auth_client.post(
            "/checkups/upload",
            files={"file": ("lab.jpg", big, "image/jpeg")},
            follow_redirects=False,
        )
    assert spawn.call_count == 0
    job_id = r.headers["location"].split("=", 1)[1]
    job = checkups_router._upload_jobs.pop(job_id)
    assert job.status == "error"


def test_upload_route_no_claude_key_redirects(auth_client):
    with patch("app.routers.checkups.load_credentials",
              return_value=type("C", (), {"anthropic_key": None})()):
        r = auth_client.post(
            "/checkups/upload",
            files={"file": ("lab.jpg", b"fake-image-bytes", "image/jpeg")},
            follow_redirects=False,
        )
    assert r.status_code == 303 and r.headers["location"] == "/checkups?err=nokey"


def test_checkups_list_shows_upload_job_status(auth_client):
    from app.routers import checkups as checkups_router

    job = checkups_router.UploadJob(
        id="testjob1", user_id=_user_id("t@example.com"), filenames=["lab.jpg"],
        status="done", checkup_ids=[123],
    )
    checkups_router._upload_jobs[job.id] = job
    try:
        page = auth_client.get("/checkups?jobs=testjob1").text
        assert "lab.jpg" in page
        assert "/checkups/123?ocr=1" in page
    finally:
        checkups_router._upload_jobs.pop(job.id, None)


def test_checkups_list_ignores_other_users_jobs(auth_client):
    from app.routers import checkups as checkups_router

    other_uid = _user_id("t@example.com") + 999
    job = checkups_router.UploadJob(id="testjob2", user_id=other_uid, filenames=["secret.jpg"])
    checkups_router._upload_jobs[job.id] = job
    try:
        page = auth_client.get("/checkups?jobs=testjob2").text
        assert "secret.jpg" not in page
    finally:
        checkups_router._upload_jobs.pop(job.id, None)


def test_checkups_ws_rejects_unauthenticated(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/checkups/ws"):
            pass


def test_checkups_ws_accepts_authenticated_user(auth_client):
    with auth_client.websocket_connect("/checkups/ws"):
        pass


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
