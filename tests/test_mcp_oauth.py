"""NF-08 http transport: the OAuth 2.1 authorization server in front of the MCP endpoint.

The MCP protocol handshake itself is the SDK's to test. What is ours — and what these
cover — is everything that decides *whether a request gets in and whose data it sees*:
the consent screen's authentication, single-use codes, refresh rotation, the registration
cap, and the token→user binding every tool call is scoped by.

``mcp`` is an opt-in extra (CI installs ``.[dev]`` only), so the whole module skips when
it isn't installed — same rule as the browser guards.
"""
import base64
import hashlib
import secrets

import anyio
import pytest

pytest.importorskip("mcp")

from cryptography.fernet import Fernet  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import oauth  # noqa: E402
from app.db.base import async_session_maker, init_db  # noqa: E402
from tests.web_helpers import _seed_user, _user_id  # noqa: E402

PUBLIC_URL = "https://mcp.test"
REDIRECT_URI = "https://client.test/cb"


@pytest.fixture
def mcp_app(monkeypatch):
    """The http-transport ASGI app, with a real Fernet key (client documents are stored
    encrypted) and the schema created."""
    from app.core import crypto

    monkeypatch.setattr(crypto.settings, "APP_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_fernet", None)

    anyio.run(init_db)

    from app.mcp_server import _http_app, build_server

    # base_url matches the issuer: the SDK enables DNS-rebinding protection, so a
    # mismatched Host is refused before any handler runs (as it should be).
    with TestClient(
        _http_app(build_server(public_url=PUBLIC_URL), PUBLIC_URL), base_url=PUBLIC_URL
    ) as c:
        yield c


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


def _register_client(client, name="Claude") -> dict:
    r = client.post(
        "/register",
        json={
            "client_name": name,
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _authorize(client, reg: dict, challenge: str, state="st8"):
    """Run /authorize and return the ``req`` token of the parked request."""
    r = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": "garmin:read",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text
    location = r.headers["location"]
    assert "/oauth/consent?req=" in location
    return location.split("req=", 1)[1]


def _consent(client, req: str, email: str, password: str):
    return client.post(
        "/oauth/consent",
        data={"req": req, "action": "allow", "email": email, "password": password},
        follow_redirects=False,
    )


def _exchange(client, reg: dict, code: str, verifier: str):
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": reg["client_id"],
            "client_secret": reg.get("client_secret", ""),
            "code_verifier": verifier,
        },
    )


def _full_flow(client, email="mcpuser@example.com", password="pw"):
    """Register → authorize → consent → token. Returns (token payload, user id)."""
    _seed_user(email=email, password=password, is_admin=False)
    reg = _register_client(client)
    verifier, challenge = _pkce()
    req = _authorize(client, reg, challenge)
    redirect = _consent(client, req, email, password)
    assert redirect.status_code == 303, redirect.text
    code = redirect.headers["location"].split("code=", 1)[1].split("&")[0]
    tok = _exchange(client, reg, code, verifier)
    assert tok.status_code == 200, tok.text
    return tok.json(), _user_id(email), reg


def test_full_authorization_code_flow_issues_a_token_bound_to_the_user(mcp_app):
    payload, uid, _ = _full_flow(mcp_app)
    assert payload["token_type"].lower() == "bearer"
    assert payload["refresh_token"]

    from app.mcp_oauth import DbOAuthProvider

    async def check():
        return await DbOAuthProvider(PUBLIC_URL).load_access_token(payload["access_token"])

    token = anyio.run(check)
    assert token is not None
    # The whole point: the token names the user every tool call will be scoped to.
    assert token.subject == str(uid)


def test_consent_refuses_a_wrong_password(mcp_app):
    _seed_user(email="wrongpw@example.com", password="pw", is_admin=False)
    reg = _register_client(mcp_app)
    _, challenge = _pkce()
    req = _authorize(mcp_app, reg, challenge)

    r = _consent(mcp_app, req, "wrongpw@example.com", "not-the-password")
    assert r.status_code == 401
    assert "location" not in r.headers


def test_consent_refuses_an_unapproved_account(mcp_app):
    from app.core.crypto import hash_password
    from app.db import users

    async def seed():
        async with async_session_maker() as s:
            await users.create_user(
                s, email="unapproved@example.com", password_hash=hash_password("pw"),
                is_admin=False, is_approved=False,
            )

    anyio.run(seed)
    reg = _register_client(mcp_app)
    _, challenge = _pkce()
    req = _authorize(mcp_app, reg, challenge)

    # Correct password, but the account was never approved — the MCP endpoint must not
    # be a way around the approval gate the web login enforces.
    r = _consent(mcp_app, req, "unapproved@example.com", "pw")
    assert r.status_code == 401


def test_denying_consent_redirects_with_access_denied(mcp_app):
    _seed_user(email="denier@example.com", password="pw", is_admin=False)
    reg = _register_client(mcp_app)
    _, challenge = _pkce()
    req = _authorize(mcp_app, reg, challenge)

    r = mcp_app.post(
        "/oauth/consent", data={"req": req, "action": "deny"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "error=access_denied" in r.headers["location"]


def test_consent_without_an_explicit_allow_denies(mcp_app):
    """The choice rides on the pressed button's name/value; a body that lost it must fall
    to "deny", never to the branch that mints a code — even with a valid email+password."""
    _seed_user(email="lost-button@example.com", password="pw", is_admin=False)
    reg = _register_client(mcp_app)
    _, challenge = _pkce()
    req = _authorize(mcp_app, reg, challenge)

    r = mcp_app.post(
        "/oauth/consent",
        data={"req": req, "email": "lost-button@example.com", "password": "pw"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=access_denied" in r.headers["location"]
    assert "code=" not in r.headers["location"]


def test_authorization_code_cannot_be_replayed(mcp_app):
    _seed_user(email="replay@example.com", password="pw", is_admin=False)
    reg = _register_client(mcp_app)
    verifier, challenge = _pkce()
    req = _authorize(mcp_app, reg, challenge)
    redirect = _consent(mcp_app, req, "replay@example.com", "pw")
    code = redirect.headers["location"].split("code=", 1)[1].split("&")[0]

    assert _exchange(mcp_app, reg, code, verifier).status_code == 200
    # Second exchange of the same code must fail — a code that survives its exchange is
    # a replayable credential.
    assert _exchange(mcp_app, reg, code, verifier).status_code == 400


def test_pkce_verifier_must_match(mcp_app):
    _seed_user(email="pkce@example.com", password="pw", is_admin=False)
    reg = _register_client(mcp_app)
    _, challenge = _pkce()
    req = _authorize(mcp_app, reg, challenge)
    redirect = _consent(mcp_app, req, "pkce@example.com", "pw")
    code = redirect.headers["location"].split("code=", 1)[1].split("&")[0]

    other_verifier, _ = _pkce()
    assert _exchange(mcp_app, reg, code, other_verifier).status_code == 400


def test_refresh_rotates_and_retires_the_old_token(mcp_app):
    payload, _, reg = _full_flow(mcp_app, email="refresh@example.com")

    first = mcp_app.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": payload["refresh_token"],
            "client_id": reg["client_id"],
            "client_secret": reg.get("client_secret", ""),
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["refresh_token"] != payload["refresh_token"]

    # Reusing the spent refresh token must fail.
    again = mcp_app.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": payload["refresh_token"],
            "client_id": reg["client_id"],
            "client_secret": reg.get("client_secret", ""),
        },
    )
    assert again.status_code == 400


def test_token_stops_working_once_the_account_is_deactivated(mcp_app):
    payload, uid, _ = _full_flow(mcp_app, email="deact@example.com")

    from app.db import users
    from app.mcp_oauth import DbOAuthProvider

    async def deactivate_then_load():
        async with async_session_maker() as s:
            user = await users.get_by_id(s, uid)
            user.is_active = False
            await s.commit()
        return await DbOAuthProvider(PUBLIC_URL).load_access_token(payload["access_token"])

    # An admin switching an account off must not have to wait out the token's hour.
    assert anyio.run(deactivate_then_load) is None


def test_revoking_from_settings_kills_the_token(mcp_app):
    payload, uid, _ = _full_flow(mcp_app, email="revoker@example.com")

    from app.mcp_oauth import DbOAuthProvider

    async def listed():
        async with async_session_maker() as s:
            return await oauth.active_clients_for_user(s, uid)

    # The consent screen promises the access can be taken back; /settings lists it...
    assert anyio.run(listed) == ["Claude"]

    async def revoke_then_load():
        async with async_session_maker() as s:
            await oauth.revoke_user_grants(s, uid)
        return await DbOAuthProvider(PUBLIC_URL).load_access_token(payload["access_token"])

    # ...and revoking really does invalidate the live token, not just hide the row.
    assert anyio.run(revoke_then_load) is None
    assert anyio.run(listed) == []


def test_mcp_endpoint_refuses_an_unauthenticated_call(mcp_app):
    r = mcp_app.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401


def test_client_registration_is_capped(mcp_app, monkeypatch):
    from app.core.config import settings

    async def already() -> int:
        async with async_session_maker() as s:
            return await oauth.count_clients(s)

    monkeypatch.setattr(settings, "MCP_OAUTH_MAX_CLIENTS", anyio.run(already) + 1)
    _register_client(mcp_app, name="under-the-cap")

    over = mcp_app.post(
        "/register",
        json={"client_name": "over", "redirect_uris": [REDIRECT_URI]},
    )
    assert over.status_code == 400


def test_stored_tokens_are_hashed_not_plaintext(mcp_app):
    """A database copy must not hand over live tokens (see app.db.oauth)."""
    from sqlalchemy import select

    from app.db.models import OAuthGrant

    payload, _, _ = _full_flow(mcp_app, email="hashed@example.com")

    async def rows():
        async with async_session_maker() as s:
            return (await s.execute(select(OAuthGrant.token_hash))).scalars().all()

    stored = set(anyio.run(rows))
    assert payload["access_token"] not in stored
    assert payload["refresh_token"] not in stored
    assert oauth.hash_secret(payload["access_token"]) in stored


def test_bound_stdio_user_is_ignored_when_a_token_is_present():
    """Over http the token decides, never the process-wide binding — the two identity
    sources must not be able to disagree in the attacker's favour."""
    from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var
    from mcp.server.auth.provider import AccessToken

    import app.mcp_server as srv

    token = AccessToken(token="t", client_id="c", scopes=["garmin:read"], subject="42")
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        srv._user_id = 7
        assert srv._current_user_id() == 42
    finally:
        auth_context_var.reset(reset)
        srv._user_id = None


def test_no_user_at_all_is_an_error_not_a_default():
    import app.mcp_server as srv

    srv._user_id = None
    with pytest.raises(RuntimeError):
        srv._current_user_id()


def test_stdio_server_registers_every_tool_and_no_auth():
    """stdio is the unchanged NF-08 shape: same five read-only tools, no OAuth (there is
    no HTTP request to carry a token, and the SDK rejects auth settings without one)."""
    from app.mcp_server import _TOOLS, build_server

    server = build_server()
    names = {t.name for t in anyio.run(server.list_tools)}
    assert names == {fn.__name__ for fn in _TOOLS}
    assert server.settings.auth is None


def test_http_transport_refuses_to_start_without_a_public_url(monkeypatch):
    from app.core.config import settings
    from app.mcp_server import main

    monkeypatch.setattr(settings, "MCP_PUBLIC_URL", None)
    # No issuer means no verifiable OAuth metadata — guessing one would publish an
    # endpoint whose discovery silently points somewhere else.
    with pytest.raises(SystemExit, match="MCP_PUBLIC_URL"):
        main(["--transport", "http"])


def test_email_is_rejected_in_http_mode(monkeypatch):
    from app.core.config import settings
    from app.mcp_server import main

    monkeypatch.setattr(settings, "MCP_PUBLIC_URL", PUBLIC_URL)
    # Accepting and ignoring it would leave the operator believing the endpoint is
    # limited to one account.
    with pytest.raises(SystemExit, match="meaningless"):
        main(["--transport", "http", "--email", "me@example.com"])
