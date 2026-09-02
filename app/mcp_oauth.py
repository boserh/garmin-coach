"""OAuth 2.1 authorization server for the remote MCP endpoint (NF-08 http transport).

Why this exists: over stdio the MCP server is a local child process of the client, and
"who is this" is answered by the ``--email`` it was launched with. Over HTTP that is
gone — the endpoint is on the public internet and every request must carry proof of who
it speaks for. Claude's web connector speaks OAuth 2.1 with dynamic client registration,
so that is what we serve. (Bearer-header connectors exist but are a gated beta; OAuth is
the path that works for everyone, and it is what the MCP spec settled on.)

The SDK (``mcp.server.auth``) already implements the *protocol*: the ``/authorize``,
``/token``, ``/register`` and ``/revoke`` endpoints, both metadata documents, PKCE
verification and redirect-URI matching. What it cannot know is our storage and our idea
of a user — that is the provider below, plus the consent screen.

**The consent screen is the whole security boundary.** ``authorize()`` is reached by an
anonymous browser: it parks the request and hands back a URL, and nothing is granted
until someone proves on that page that they own an account here (email + password, the
same bcrypt hashes the web login uses). Get that wrong and every other control is
decoration. Hence: a deactivated, unapproved or demo account is refused; the parked
request is single-use and short-lived; and the attempt is rate-limited per IP.

The issued token's ``subject`` is the user id. Every MCP tool call reads it back and
scopes its query to that user — see :mod:`app.mcp_server`.
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Optional

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.core.config import settings
from app.core.crypto import verify_password_async
from app.core.ratelimit import RateLimiter
from app.db import oauth, users
from app.db.base import async_session_maker
from app.templating import create_templates

logger = logging.getLogger("mcp.oauth")

templates = create_templates()

# The scopes this authorization server can mint. One per MCP service, and a service
# declares exactly one of them: the SDK's RequireAuthMiddleware rejects a token that
# lacks the scope the endpoint requires, so a coach token presented to the notify
# endpoint (or the reverse) is refused even though both read the same grants table.
#
# SCOPE is read-only because every tool behind it is read-only (see app.mcp_server);
# NOTIFY_SCOPE is write-only in the opposite sense — it grants no read of anything, only
# the ability to push a message into the deployment's monitoring channel
# (app.mcp_notify). Keeping them apart is what lets the read-only promise on the coach
# consent screen stay true.
SCOPE = "garmin:read"
NOTIFY_SCOPE = "notify:write"

# A parked authorization request: how long the user has to finish logging in.
PENDING_TTL_S = 600
# An authorization code is redeemed by the client within a round trip; anything longer is
# just a wider replay window. RFC 6749 §4.1.2 recommends "maximum of 10 minutes"; a
# machine client needs seconds.
CODE_TTL_S = 60
ACCESS_TTL_S = 3600
REFRESH_TTL_S = 30 * 24 * 3600

# Per-IP guard on the consent form — it takes an email and a password, so it is a login
# form, and it gets a login form's brute-force protection.
_consent_limiter = RateLimiter(settings.LOGIN_RATE_LIMIT, settings.LOGIN_RATE_WINDOW_S)

_RATE_LIMIT_MSG = "Забагато спроб. Зачекай кілька хвилин і спробуй знову."


def _new_secret() -> str:
    """A 256-bit URL-safe random value — used for codes, tokens and parked requests."""
    return secrets.token_urlsafe(32)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


class DbOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """The SDK's provider interface, backed by :mod:`app.db.oauth`.

    Every method opens its own session: these are called from the SDK's own endpoints,
    which have no request-scoped session of ours to borrow.
    """

    def __init__(self, public_url: str, *, scope: str = SCOPE):
        self.public_url = public_url.rstrip("/")
        # Which scope a request that names none defaults to — i.e. the one this
        # server's own tools need. Never a union: a client asking for nothing must not
        # come away holding both.
        self.scope = scope

    # --- client registration (RFC 7591) ---

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        async with async_session_maker() as session:
            data = await oauth.get_client(session, client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        async with async_session_maker() as session:
            if await oauth.count_clients(session) >= settings.MCP_OAUTH_MAX_CLIENTS:
                # Registration is unauthenticated by protocol design, so the ceiling is
                # the only thing between it and unbounded writes.
                logger.warning("MCP OAuth: client registration refused — cap reached")
                raise RegistrationError(
                    error="invalid_client_metadata",
                    error_description="Client registration limit reached on this server.",
                )
            await oauth.put_client(
                session, client_info.client_id, client_info.model_dump(mode="json")
            )
        logger.info(f"MCP OAuth: registered client {client_info.client_id}")

    # --- authorization ---

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the browser to our consent screen.

        No code is minted here: at this point the caller is an anonymous browser that has
        merely named a client id. The code is created only after the consent page has
        authenticated a real account.
        """
        req = _new_secret()
        async with async_session_maker() as session:
            await oauth.purge_expired(session)
            await oauth.create_grant(
                session,
                kind="pending",
                secret=req,
                client_id=client.client_id,
                ttl_s=PENDING_TTL_S,
                scopes=params.scopes or [self.scope],
                data={
                    "state": params.state,
                    "code_challenge": params.code_challenge,
                    "redirect_uri": str(params.redirect_uri),
                    "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                    "resource": params.resource,
                    "client_name": client.client_name or client.client_id,
                },
            )
        return f"{self.public_url}/oauth/consent?req={req}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        async with async_session_maker() as session:
            grant = await oauth.load_grant(session, "code", authorization_code)
        # A code issued to one client must never be redeemable by another, even if it
        # leaked — the SDK checks PKCE and the redirect URI, this checks the client.
        if grant is None or grant.client_id != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=grant.scopes,
            expires_at=grant.expires_at,
            client_id=grant.client_id,
            code_challenge=grant.data["code_challenge"],
            redirect_uri=AnyUrl(grant.data["redirect_uri"]),
            redirect_uri_provided_explicitly=grant.data["redirect_uri_provided_explicitly"],
            resource=grant.data.get("resource"),
            subject=str(grant.user_id),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        async with async_session_maker() as session:
            # consume_ not load_: a code that survives its own exchange is replayable.
            grant = await oauth.consume_grant(session, "code", authorization_code.code)
            if grant is None or grant.client_id != client.client_id:
                raise TokenError(
                    error="invalid_grant", error_description="Unknown authorization code."
                )
            tokens = await self._issue(session, grant.client_id, grant.user_id, grant.scopes)
        logger.info(f"MCP OAuth: issued tokens to {client.client_id} for user {grant.user_id}")
        return tokens

    # --- refresh ---

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[RefreshToken]:
        async with async_session_maker() as session:
            grant = await oauth.load_grant(session, "refresh", refresh_token)
        if grant is None or grant.client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=grant.client_id,
            scopes=grant.scopes,
            expires_at=int(grant.expires_at),
            subject=str(grant.user_id),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        async with async_session_maker() as session:
            # Rotation: the presented refresh token is spent here and replaced. A
            # long-lived, reusable refresh token is a password with no expiry.
            grant = await oauth.consume_grant(session, "refresh", refresh_token.token)
            if grant is None or grant.client_id != client.client_id:
                raise TokenError(
                    error="invalid_grant", error_description="Unknown refresh token."
                )
            # A refresh may narrow scopes but never widen them (RFC 6749 §6).
            granted = [s for s in (scopes or grant.scopes) if s in grant.scopes]
            tokens = await self._issue(session, grant.client_id, grant.user_id, granted)
        return tokens

    async def _issue(self, session, client_id: str, user_id, scopes) -> OAuthToken:
        access, refresh = _new_secret(), _new_secret()
        await oauth.purge_expired(session)
        await oauth.create_grant(
            session, kind="access", secret=access, client_id=client_id,
            user_id=user_id, scopes=scopes, ttl_s=ACCESS_TTL_S,
        )
        await oauth.create_grant(
            session, kind="refresh", secret=refresh, client_id=client_id,
            user_id=user_id, scopes=scopes, ttl_s=REFRESH_TTL_S,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL_S,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    # --- resource access ---

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        async with async_session_maker() as session:
            grant = await oauth.load_grant(session, "access", token)
            if grant is None or grant.user_id is None:
                return None
            # Re-checked on every call, not just at consent: a token must stop working
            # the moment an admin deactivates the account behind it, without waiting out
            # its hour. This is the only per-request DB read the tools don't already do.
            user = await users.get_by_id(session, grant.user_id)
        if user is None or not user.is_active or not user.is_approved:
            return None
        return AccessToken(
            token=token,
            client_id=grant.client_id,
            scopes=grant.scopes,
            expires_at=int(grant.expires_at),
            subject=str(grant.user_id),
        )

    async def revoke_token(self, token) -> None:
        value = getattr(token, "token", token)
        async with async_session_maker() as session:
            await oauth.revoke_grant(session, value)


# --- the consent screen -------------------------------------------------------------


def _consent_page(
    request: Request, *, pending: dict, req: str, scopes=None, error=None, status_code=200
):
    """Render the approval screen.

    ``scopes`` decides which promise the page makes. The read-only wording used to be
    hardcoded, which was fine while every tool behind every token was read-only — the
    moment a second server started minting NOTIFY_SCOPE, a hardcoded "нічого змінити він
    не може" would have been the page lying about the grant the user is signing.
    """
    return templates.TemplateResponse(
        request,
        "mcp_consent.html",
        {
            "req": req,
            "client_name": pending.get("client_name", "MCP-клієнт"),
            "notify": NOTIFY_SCOPE in (scopes or []),
            "error": error,
        },
        status_code=status_code,
    )


def _expired_page(request: Request) -> Response:
    return templates.TemplateResponse(
        request,
        "mcp_consent.html",
        {"req": None, "client_name": None, "notify": False, "error": None,
         "expired": True},
        status_code=400,
    )


async def consent_get(request: Request) -> Response:
    req = request.query_params.get("req", "")
    async with async_session_maker() as session:
        grant = await oauth.load_grant(session, "pending", req) if req else None
    if grant is None:
        return _expired_page(request)
    return _consent_page(request, pending=grant.data, req=req, scopes=grant.scopes)


async def consent_post(request: Request) -> Response:
    form = await request.form()
    req = str(form.get("req") or "")
    async with async_session_maker() as session:
        grant = await oauth.load_grant(session, "pending", req) if req else None
    if grant is None:
        return _expired_page(request)

    # "Відхилити" — hand the client the protocol's own answer rather than a dead end.
    # Fail CLOSED: only an explicit "allow" grants. The choice rides on the pressed
    # button's name/value, and a client that drops it (app.js's double-submit guard used
    # to, by disabling the button before the browser read it) must land here, not on the
    # branch that mints a code.
    if form.get("action") != "allow":
        async with async_session_maker() as session:
            await oauth.consume_grant(session, "pending", req)
        return RedirectResponse(
            construct_redirect_uri(
                grant.data["redirect_uri"],
                error="access_denied",
                error_description="The user denied the request.",
                state=grant.data.get("state"),
            ),
            status_code=303,
        )

    if not _consent_limiter.allow(f"ip:{_client_ip(request)}"):
        return _consent_page(
            request, pending=grant.data, req=req, scopes=grant.scopes,
            error=_RATE_LIMIT_MSG, status_code=429,
        )

    email = str(form.get("email") or "").strip().lower()
    password = str(form.get("password") or "")
    async with async_session_maker() as session:
        user = await users.get_by_email(session, email)
    # One message for every rejection: which of "no such account", "wrong password" or
    # "not approved yet" applies is exactly what an attacker would like to learn.
    ok = (
        user is not None
        and user.is_approved
        and user.is_active
        and not user.is_demo
        and await verify_password_async(password, user.password_hash)
    )
    if not ok:
        logger.warning(f"MCP OAuth: consent rejected for {email!r} from {_client_ip(request)}")
        return _consent_page(
            request, pending=grant.data, req=req, scopes=grant.scopes,
            error="Невірний email або пароль, або акаунт не підтверджено.",
            status_code=401,
        )

    code = _new_secret()
    async with async_session_maker() as session:
        # Spend the parked request in the same step that mints the code, so one approval
        # can never yield two codes.
        if await oauth.consume_grant(session, "pending", req) is None:
            return _expired_page(request)
        await oauth.create_grant(
            session,
            kind="code",
            secret=code,
            client_id=grant.client_id,
            user_id=user.id,
            scopes=grant.scopes,
            ttl_s=CODE_TTL_S,
            data={
                "code_challenge": grant.data["code_challenge"],
                "redirect_uri": grant.data["redirect_uri"],
                "redirect_uri_provided_explicitly": grant.data[
                    "redirect_uri_provided_explicitly"
                ],
                "resource": grant.data.get("resource"),
            },
        )
    logger.info(
        f"MCP OAuth: user {user.id} approved {grant.data.get('client_name')!r} "
        f"({time.strftime('%H:%M:%S')})"
    )
    return RedirectResponse(
        construct_redirect_uri(
            grant.data["redirect_uri"], code=code, state=grant.data.get("state")
        ),
        status_code=303,
    )
