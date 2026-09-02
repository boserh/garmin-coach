"""Shared http-transport plumbing for the MCP servers (coach + monitoring notify).

Two MCP processes now run from this repo — :mod:`app.mcp_server` (NF-08, read-only) and
:mod:`app.mcp_notify` (the write-only monitoring channel) — deliberately as separate
services on separate origins. What they share is the *transport*: the same OAuth 2.1
authorization server, the same consent screen, the same DNS-rebinding trap. That is
plumbing, not policy, so it lives here once instead of being copied into the second
server and drifting.

What they do NOT share is the scope: each server declares the one scope its own tools
need, and the SDK's ``RequireAuthMiddleware`` refuses a token that lacks it. A token
minted for the coach endpoint therefore cannot be replayed against the notify endpoint
(and vice versa) even though both are backed by the same grants table — which is the
whole point of running them apart.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)


def auth_kwargs(public_url: str, scope: str) -> dict:
    """The ``MCPServer(...)`` keyword arguments that turn OAuth on for one origin."""
    from app.mcp_oauth import DbOAuthProvider

    base = public_url.rstrip("/")
    return {
        "auth": AuthSettings(
            issuer_url=base,
            # RFC 8707: the token is bound to THIS resource, so one leaked to another
            # MCP server can't be replayed against us (and vice versa).
            resource_server_url=f"{base}/mcp",
            required_scopes=[scope],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[scope], default_scopes=[scope]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        "auth_server_provider": DbOAuthProvider(base, scope=scope),
    }


def register_consent(server) -> None:
    """Mount the consent screen — the page where an anonymous browser turns into an
    account, and the only reason any of the OAuth endpoints are safe to expose."""
    from app.mcp_oauth import consent_get, consent_post

    server.custom_route("/oauth/consent", methods=["GET"])(consent_get)
    server.custom_route("/oauth/consent", methods=["POST"])(consent_post)


def http_app(server, public_url: str, *, static: bool = True):
    """The ASGI app: the SDK's Starlette app (MCP endpoint, OAuth endpoints, both
    metadata documents, session-manager lifespan) plus our static files, which the
    consent page's stylesheet comes from.

    The transport-security settings are passed explicitly and not left to the SDK's
    default. That default keys off the *bind* address: binding to 127.0.0.1 — which is
    exactly right behind a tunnel — auto-enables DNS-rebinding protection with an
    allow-list of ``localhost``/``127.0.0.1``, and every real request then arrives with
    ``Host: <public hostname>`` and is refused. So the allow-list has to name the public
    origin instead, which keeps the protection AND lets the proxy through.
    """
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.routing import Mount
    from starlette.staticfiles import StaticFiles

    from app.templating import STATIC_DIR

    base = public_url.rstrip("/")
    app = server.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[urlparse(base).netloc],
            allowed_origins=[base],
        )
    )
    if static:
        # Appended last, so it can never shadow an MCP or OAuth route.
        app.routes.append(
            Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
        )
    return app


def require_public_url(value: Optional[str], *, var: str, transport: str = "http") -> str:
    """Fail fast, with the reason, when the issuer origin isn't configured.

    There is no safe default to guess: the issuer must match the origin clients actually
    connect to, or discovery fails in a way that looks like a client bug.
    """
    if not value:
        raise SystemExit(
            f"{var} must be set for --transport {transport} (the OAuth issuer — it has "
            "to match the public HTTPS origin clients connect to, e.g. "
            "https://mcp.example.com)."
        )
    return value
