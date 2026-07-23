"""Web smoke tests: login/logout/register, admin user management, password change."""
from unittest.mock import patch

import anyio

from app.db import users
from app.db.base import async_session_maker
from tests.web_helpers import _seed_user, _user_id


def test_login_flow(client):
    _seed_user()
    assert client.get("/login").status_code == 200
    bad = client.post(
        "/login", data={"email": "t@example.com", "password": "nope"},
        follow_redirects=False,
    )
    assert bad.status_code == 401
    ok = client.post(
        "/login", data={"email": "t@example.com", "password": "pw"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert ok.headers["location"] == "/ui"


def test_login_rate_limited_after_n_attempts(client):
    # The suite disables the limiter globally (conftest); swap in a low-limit one.
    from app.core.ratelimit import RateLimiter
    from app.routers import auth as auth_router

    clock = {"t": 0.0}
    limiter = RateLimiter(3, 300, now=lambda: clock["t"])
    with patch.object(auth_router, "_login_limiter", limiter):
        for _ in range(3):
            r = client.post(
                "/login", data={"email": "nobody@example.com", "password": "x"},
                follow_redirects=False,
            )
            assert r.status_code == 401  # wrong creds, but under the limit
        blocked = client.post(
            "/login", data={"email": "nobody@example.com", "password": "x"},
            follow_redirects=False,
        )
        assert blocked.status_code == 429
        # after the window slides, attempts are allowed again
        clock["t"] = 301
        again = client.post(
            "/login", data={"email": "nobody@example.com", "password": "x"},
            follow_redirects=False,
        )
        assert again.status_code == 401


def test_register_rate_limited(client):
    from app.core.ratelimit import RateLimiter
    from app.routers import auth as auth_router

    with patch.object(auth_router, "_register_limiter", RateLimiter(2, 300)):
        for i in range(2):
            r = client.post(
                "/register", data={"email": f"u{i}@example.com", "password": "secret1"},
                follow_redirects=False,
            )
            assert r.status_code == 200
        blocked = client.post(
            "/register", data={"email": "u2@example.com", "password": "secret1"},
            follow_redirects=False,
        )
        assert blocked.status_code == 429


def test_logout_get_does_not_clear_session(auth_client):
    # A stray GET /logout (e.g. cross-site <img>) must NOT sign the user out.
    r = auth_client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings"
    # still authenticated afterwards
    assert auth_client.get("/status").status_code == 200


def test_logout_post_clears_session(auth_client):
    r = auth_client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    # session gone → protected endpoint bounces to /login
    assert auth_client.get("/status", follow_redirects=False).status_code == 303


def test_logout_clears_session(auth_client):
    assert auth_client.get("/ui", follow_redirects=False).status_code == 200
    auth_client.post("/logout")
    assert auth_client.get("/ui", follow_redirects=False).status_code == 303


def test_register_creates_pending_user_and_blocks_login(client):
    r = client.post(
        "/register", data={"email": "newbie@example.com", "password": "secret1"},
        follow_redirects=False,
    )
    assert r.status_code == 200  # login page with an "awaiting approval" notice


    async def check():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, "newbie@example.com")
            assert u is not None
            assert u.is_approved is False
            assert u.is_admin is False

    anyio.run(check)

    # unapproved → cannot log in yet
    login = client.post(
        "/login", data={"email": "newbie@example.com", "password": "secret1"},
        follow_redirects=False,
    )
    assert login.status_code == 403


def test_register_rejects_duplicate_email(auth_client):
    # t@example.com already exists (the admin)
    r = auth_client.post(
        "/register", data={"email": "t@example.com", "password": "secret1"},
        follow_redirects=False,
    )
    assert r.status_code == 409


def test_admin_approves_then_user_can_login(auth_client):
    auth_client.post("/register", data={"email": "pend@example.com", "password": "secret1"})
    uid = _user_id("pend@example.com")

    r = auth_client.post(f"/admin/users/{uid}/approve", follow_redirects=False)
    assert r.status_code == 303

    # logging in here replaces the admin session cookie with the approved user's
    login = auth_client.post(
        "/login", data={"email": "pend@example.com", "password": "secret1"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/dashboard"  # non-admin lands on the dashboard


def test_admin_deletes_user(auth_client):
    auth_client.post("/register", data={"email": "del@example.com", "password": "secret1"})
    uid = _user_id("del@example.com")
    r = auth_client.post(f"/admin/users/{uid}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert _user_id("del@example.com") is None


def test_admin_deactivate_and_reactivate(auth_client):
    _seed_user(email="da@example.com", password="pw", is_admin=False)
    uid = _user_id("da@example.com")

    # deactivate → that user can no longer log in
    assert auth_client.post(
        f"/admin/users/{uid}/active", data={"active": "0"}, follow_redirects=False
    ).status_code == 303
    blocked = auth_client.post(
        "/login", data={"email": "da@example.com", "password": "pw"},
        follow_redirects=False,
    )
    assert blocked.status_code == 403  # "Акаунт деактивовано" — admin session intact

    # reactivate → login works again
    assert auth_client.post(
        f"/admin/users/{uid}/active", data={"active": "1"}, follow_redirects=False
    ).status_code == 303
    assert auth_client.post(
        "/login", data={"email": "da@example.com", "password": "pw"},
        follow_redirects=False,
    ).status_code == 303


def test_admin_cannot_deactivate_self(auth_client):
    uid = _user_id("t@example.com")
    auth_client.post(f"/admin/users/{uid}/active", data={"active": "0"},
                     follow_redirects=False)


    async def check():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, "t@example.com")
            assert u.is_active is True  # self-deactivation ignored

    anyio.run(check)


def test_change_password_requires_login(client):
    r = client.post(
        "/settings/password",
        data={"current_password": "x", "new_password": "yyyyyy"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_change_own_password(client):
    _seed_user(email="pw1@example.com", password="origpass", is_admin=False)
    client.post("/login", data={"email": "pw1@example.com", "password": "origpass"})

    wrong = client.post(
        "/settings/password",
        data={"current_password": "nope", "new_password": "newpass1"},
        follow_redirects=False,
    )
    assert wrong.headers["location"] == "/settings?pw=wrong"

    short = client.post(
        "/settings/password",
        data={"current_password": "origpass", "new_password": "abc"},
        follow_redirects=False,
    )
    assert short.headers["location"] == "/settings?pw=short"

    ok = client.post(
        "/settings/password",
        data={"current_password": "origpass", "new_password": "newpass1"},
        follow_redirects=False,
    )
    assert ok.headers["location"] == "/settings?pw=ok"

    # old password stops working, new one logs in
    client.post("/logout")
    assert client.post(
        "/login", data={"email": "pw1@example.com", "password": "origpass"},
        follow_redirects=False,
    ).status_code == 401
    assert client.post(
        "/login", data={"email": "pw1@example.com", "password": "newpass1"},
        follow_redirects=False,
    ).status_code == 303


def test_admin_users_create(auth_client):
    r = auth_client.post(
        "/admin/users",
        data={"email": "new@example.com", "password": "pw2"},
        follow_redirects=False,
    )
    assert r.status_code == 303


    async def check():
        async with async_session_maker() as s:
            assert await users.get_by_email(s, "new@example.com") is not None

    anyio.run(check)


def test_admin_users_forbidden_for_non_admin(client):
    _seed_user(email="plain@example.com", password="pw", is_admin=False)
    client.post("/login", data={"email": "plain@example.com", "password": "pw"})
    assert client.get("/admin/users").status_code == 403


