"""Admin impersonation (app.core.impersonate): an admin can borrow a user's session to
see what they see, and that borrowed session is read-only, spends nothing and holds no
admin rights.

The guards are what these tests are about — the happy path is two lines. conftest's
autouse fixtures already block the real Anthropic/Garmin clients, so a test asserting
"no call was made" checks the guard fires BEFORE that point (a Garmin attempt would
raise a different, louder error).
"""
import anyio
import pytest

from app.db.base import async_session_maker
from tests.web_helpers import _seed_user, _user_id


@pytest.fixture
def admin_and_user(client):
    """Logged-in admin + a seeded regular user who owns one stored day (so "whose data
    is this page showing" is answerable by a count). Returns the user's id."""
    from app.garmin import repository
    from app.garmin.schemas import DailySummary

    _seed_user(email="admin@example.com", password="pw", is_admin=True)
    _seed_user(email="joe@example.com", password="pw", is_admin=False)
    joe_id = _user_id("joe@example.com")

    async def seed_day():
        async with async_session_maker() as s:
            await repository.upsert_daily(
                s, joe_id, DailySummary(date="2026-06-20", hrv_avg=55, has_data=True))
            await s.commit()

    anyio.run(seed_day)
    r = client.post("/login", data={"email": "admin@example.com", "password": "pw"})
    assert r.status_code == 200
    return joe_id


def _start(client, user_id):
    return client.post(f"/admin/users/{user_id}/impersonate", follow_redirects=False)


def test_impersonate_switches_session_and_shows_the_bar(client, admin_and_user):
    r = _start(client, admin_and_user)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"

    page = client.get("/dashboard")
    assert page.status_code == 200
    # The bar names whose session this is, on the page itself — not only in the log.
    assert "Перегляд як" in page.text
    assert "joe@example.com" in page.text
    assert "/impersonate/stop" in page.text
    # And the pages are scoped to the impersonated user, not the admin (who has no days).
    assert client.get("/status").json()["history_days"] == 1


def test_impersonated_session_is_read_only(client, admin_and_user):
    _start(client, admin_and_user)
    r = client.post("/settings", data={"weather_location": "Kyiv"})
    assert r.status_code == 403
    assert r.json()["error"] == "impersonation_read_only"

    # …and nothing was written.
    def location():
        async def get():
            async with async_session_maker() as s:
                from app.db.models import User

                return (await s.get(User, admin_and_user)).weather_location

        return anyio.run(get)

    assert location() is None


def test_impersonated_session_has_no_admin_rights(client, admin_and_user):
    _start(client, admin_and_user)
    assert client.get("/admin/users").status_code == 403
    assert client.get("/ui").status_code == 403


def test_cannot_impersonate_an_admin_or_yourself(client):
    _seed_user(email="admin@example.com", password="pw", is_admin=True)
    _seed_user(email="admin2@example.com", password="pw", is_admin=True)
    client.post("/login", data={"email": "admin@example.com", "password": "pw"})

    for email in ("admin2@example.com", "admin@example.com"):
        r = _start(client, _user_id(email))
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/users?imp=denied"
        # session untouched: still the admin, still admin-capable
        assert client.get("/admin/users").status_code == 200


def test_non_admin_cannot_impersonate(client):
    _seed_user(email="joe@example.com", password="pw", is_admin=False)
    _seed_user(email="ann@example.com", password="pw", is_admin=False)
    client.post("/login", data={"email": "joe@example.com", "password": "pw"})
    r = _start(client, _user_id("ann@example.com"))
    assert r.status_code == 403


def test_stop_returns_the_admin_session(client, admin_and_user):
    _start(client, admin_and_user)
    r = client.post("/impersonate/stop", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/users"

    page = client.get("/admin/users")
    assert page.status_code == 200          # admin rights are back
    assert "Перегляд як" not in page.text   # and the bar is gone
    assert client.get("/status").json()["history_days"] == 0  # own data again


def test_stop_without_impersonating_is_a_no_op(client, admin_and_user):
    r = client.post("/impersonate/stop", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"
    assert client.get("/admin/users").status_code == 200


def test_stop_signs_out_when_the_admin_is_gone(client, admin_and_user):
    """Deleted/demoted mid-session: there is no admin session to hand back, so the
    borrowed one ends rather than standing."""
    # Its own admin — the demotion below is permanent in the shared test DB.
    _seed_user(email="doomed@example.com", password="pw", is_admin=True)
    client.post("/login", data={"email": "doomed@example.com", "password": "pw"})
    _start(client, admin_and_user)

    async def demote():
        async with async_session_maker() as s:
            from app.db import users as users_db

            admin = await users_db.get_by_email(s, "doomed@example.com")
            admin.is_admin = False
            await s.commit()

    anyio.run(demote)

    r = client.post("/impersonate/stop", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/login"


def test_status_reports_garmin_as_skipped(client, admin_and_user):
    """/status is the one GET that logs in to Garmin — under impersonation it must say
    it skipped, not report a fake auth error (and must not have tried)."""
    _start(client, admin_and_user)
    assert "impersonation" in client.get("/status").json()["garmin_login"]


def test_report_endpoint_refuses_instead_of_spending(client, admin_and_user):
    _start(client, admin_and_user)
    r = client.get("/report.json")
    assert r.status_code == 403
    assert r.json()["error"] == "impersonation_mode"


def test_user_runtime_refuses_under_impersonation():
    from app.core.impersonate import IMPERSONATING, ImpersonationUnavailable
    from app.garmin.runtime import user_runtime

    async def run():
        async with async_session_maker() as s:
            from app.db import users as users_db

            u = await users_db.create_user(
                s, email="runtime-imp@example.com", password_hash="x", is_admin=False,
            )
            token = IMPERSONATING.set(True)
            try:
                async with user_runtime(s, u):
                    pass
                raise AssertionError("user_runtime should have refused")
            except ImpersonationUnavailable:
                pass
            finally:
                IMPERSONATING.reset(token)

    anyio.run(run)


def test_get_client_refuses_under_impersonation():
    from app.analysis.client import AnalystError, _get_client
    from app.core.impersonate import IMPERSONATING

    token = IMPERSONATING.set(True)
    try:
        with pytest.raises(AnalystError) as e:
            _get_client("some-key")
        assert "перегляд" in str(e.value).lower()
    finally:
        IMPERSONATING.reset(token)


def test_logging_in_clears_a_stale_borrowed_session(client, admin_and_user):
    """An admin who closed the tab mid-impersonation and signed in again gets a clean
    session, not the borrowed one with their own id pasted over it."""
    _start(client, admin_and_user)
    client.post("/login", data={"email": "admin@example.com", "password": "pw"})
    page = client.get("/admin/users")
    assert page.status_code == 200
    assert "Перегляд як" not in page.text
