"""Storage for the MCP server's OAuth 2.1 authorization server (NF-08 http transport).

Everything the ``OAuthAuthorizationServerProvider`` in :mod:`app.mcp_oauth` needs to
persist, kept apart from it so the provider stays protocol logic and this stays SQL.

Two rules this module exists to enforce:

* **Secrets are hashed at rest.** Authorization codes and access/refresh tokens are only
  ever looked up by exact value, so storing SHA-256 instead of the value itself costs
  nothing and makes a stolen database copy useless for impersonation. (SHA-256 with no
  salt/stretching is right here and wrong for passwords: these are 256-bit random values,
  not guessable secrets, so there is nothing for a dictionary attack to chew on.)
* **Client documents are encrypted.** A registered client's record carries its
  ``client_secret``, which the SDK compares in plaintext — so we cannot hash it, and it
  goes under Fernet with everything else we can't hash (see app.core.crypto).
"""
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt, encrypt
from app.db.models import OAuthClient, OAuthGrant


def hash_secret(value: str) -> str:
    """The stored form of a code/token — see the module docstring."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# --- clients (RFC 7591 dynamic registration) ---------------------------------------


async def get_client(session: AsyncSession, client_id: str) -> Optional[dict]:
    row = (
        await session.execute(
            select(OAuthClient).where(OAuthClient.client_id == client_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return json.loads(decrypt(row.data_enc))


async def put_client(session: AsyncSession, client_id: str, data: dict) -> None:
    row = (
        await session.execute(
            select(OAuthClient).where(OAuthClient.client_id == client_id)
        )
    ).scalar_one_or_none()
    blob = encrypt(json.dumps(data, default=str))
    if row is None:
        session.add(OAuthClient(client_id=client_id, data_enc=blob))
    else:
        row.data_enc = blob
    await session.commit()


async def count_clients(session: AsyncSession) -> int:
    return (
        await session.execute(select(func.count()).select_from(OAuthClient))
    ).scalar_one()


# --- grants (pending requests, codes, tokens) --------------------------------------


async def create_grant(
    session: AsyncSession,
    *,
    kind: str,
    secret: str,
    client_id: str,
    ttl_s: float,
    user_id: Optional[int] = None,
    scopes: Optional[Sequence[str]] = None,
    data: Optional[dict] = None,
) -> None:
    session.add(
        OAuthGrant(
            kind=kind,
            token_hash=hash_secret(secret),
            client_id=client_id,
            user_id=user_id,
            scopes=list(scopes or []),
            expires_at=time.time() + ttl_s,
            data=data or {},
        )
    )
    await session.commit()


@dataclass(frozen=True)
class Grant:
    """A read-only snapshot of one grant row.

    Deliberately not the ORM object: ``consume_grant`` deletes the row it returns, and
    reading attributes off a deleted/expired instance afterwards is how you get a
    surprise refresh (or an ``ObjectDeletedError``) inside the token endpoint.
    """

    kind: str
    client_id: str
    user_id: Optional[int]
    scopes: list[str]
    expires_at: float
    data: dict


def _snapshot(row: OAuthGrant) -> Grant:
    return Grant(
        kind=row.kind,
        client_id=row.client_id,
        user_id=row.user_id,
        scopes=list(row.scopes or []),
        expires_at=row.expires_at,
        data=dict(row.data or {}),
    )


async def load_grant(
    session: AsyncSession, kind: str, secret: str
) -> Optional[Grant]:
    """A live grant of this kind, or None if it is unknown, revoked or expired.

    ``kind`` is part of the lookup on purpose: without it a refresh token could be
    presented where an access token is expected (and vice versa), which is exactly the
    token-substitution class of bug OAuth deployments keep rediscovering.
    """
    row = (
        await session.execute(
            select(OAuthGrant).where(
                OAuthGrant.token_hash == hash_secret(secret),
                OAuthGrant.kind == kind,
            )
        )
    ).scalar_one_or_none()
    if row is None or row.revoked or row.expires_at < time.time():
        return None
    return _snapshot(row)


async def consume_grant(
    session: AsyncSession, kind: str, secret: str
) -> Optional[Grant]:
    """Load a grant and delete it in the same breath — for the single-use kinds
    (authorization codes, and the parked request the consent page redeems). A code that
    survived its exchange is a replayable code."""
    row = (
        await session.execute(
            select(OAuthGrant).where(
                OAuthGrant.token_hash == hash_secret(secret),
                OAuthGrant.kind == kind,
            )
        )
    ).scalar_one_or_none()
    if row is None or row.revoked or row.expires_at < time.time():
        return None
    snap = _snapshot(row)
    await session.delete(row)
    await session.commit()
    return snap


async def revoke_grant(session: AsyncSession, secret: str) -> None:
    """Mark every kind of grant with this value revoked (RFC 7009 lets a client hand us
    either an access or a refresh token, and says to accept both)."""
    rows = (
        await session.execute(
            select(OAuthGrant).where(OAuthGrant.token_hash == hash_secret(secret))
        )
    ).scalars().all()
    for row in rows:
        row.revoked = True
    if rows:
        await session.commit()


async def revoke_user_grants(session: AsyncSession, user_id: int) -> int:
    """Drop every grant belonging to a user — the "disconnect Claude" button, and what
    an admin deactivating an account needs so a live token doesn't outlive the login."""
    rows = (
        await session.execute(select(OAuthGrant).where(OAuthGrant.user_id == user_id))
    ).scalars().all()
    for row in rows:
        await session.delete(row)
    if rows:
        await session.commit()
    return len(rows)


async def active_clients_for_user(session: AsyncSession, user_id: int) -> list[str]:
    """Human-readable names of the clients currently holding a live grant for this user —
    what /settings shows next to the "disconnect" button. A client whose grants have all
    expired is simply not connected any more, so it isn't listed."""
    client_ids = (
        await session.execute(
            select(OAuthGrant.client_id)
            .where(
                OAuthGrant.user_id == user_id,
                OAuthGrant.revoked.is_(False),
                OAuthGrant.expires_at >= time.time(),
            )
            .distinct()
        )
    ).scalars().all()
    names = []
    for cid in client_ids:
        try:
            data = await get_client(session, cid)
        except Exception:
            # A wrong/missing APP_SECRET_KEY must not take /settings down — the client id
            # is a fine stand-in for a display name (same rule as _safe_decrypt there).
            data = None
        names.append((data or {}).get("client_name") or cid)
    return sorted(set(names))


async def purge_expired(session: AsyncSession) -> int:
    """Delete grants that are past their expiry. Called opportunistically on issue (the
    same lazy-purge-on-write pattern the llm_cache uses) — no scheduled job to forget."""
    result = await session.execute(
        delete(OAuthGrant).where(OAuthGrant.expires_at < time.time())
    )
    await session.commit()
    return result.rowcount or 0
