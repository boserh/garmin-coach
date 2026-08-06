"""Session-based web auth: the ``current_user`` dependency and login helpers.

A successful login stores ``user_id`` in the signed cookie session (starlette
``SessionMiddleware``, keyed by ``APP_SECRET_KEY``). ``current_user`` resolves that
back to a :class:`User`; when there is no valid session it raises
:class:`RequiresLogin`, which an app-level handler turns into a redirect to /login
(nicer than a 401 for the browser UI).

An admin-impersonated session (see ``app.core.impersonate``) stores the borrowed
account in that same ``user_id`` — so every route stays user-scoped without knowing
anything about it — plus the real admin's id alongside. ``current_user`` is where that
second key turns into the two guarantees the feature rests on: read-only, and no
outbound call on the user's dime.
"""
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.demo import IS_DEMO
from app.core.impersonate import (
    IMPERSONATING,
    IMPERSONATOR_KEY,
    SAFE_METHODS,
    ImpersonationReadOnly,
)
from app.db.models import User
from app.db.session import get_session


class RequiresLogin(Exception):
    """Raised by ``current_user`` when the request has no valid session."""


async def current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User:
    uid = request.session.get("user_id")
    if uid is not None:
        user = await session.get(User, uid)
        if user is not None:
            # The demo account's kill switch (see app.core.demo) — set for the rest of
            # this request so user_runtime/_get_client refuse any real network call,
            # even from a code path a router guard missed.
            IS_DEMO.set(user.is_demo)
            impersonated = request.session.get(IMPERSONATOR_KEY) is not None
            IMPERSONATING.set(impersonated)
            if impersonated and request.method not in SAFE_METHODS:
                # Every authenticated route depends on this function, so the read-only
                # rule holds for routes that don't exist yet too — the alternative
                # (each router remembering to check) is the bug waiting to happen.
                # POST /impersonate/stop reads the session directly and is unaffected.
                raise ImpersonationReadOnly()
            return user
    raise RequiresLogin()


async def require_admin(
    request: Request, user: User = Depends(current_user)
) -> User:
    # A borrowed session never carries admin rights, even in the case the impersonate
    # route already refuses (an admin borrowing another admin): admin pages span every
    # user's data, and "who did this" has to stay unambiguous.
    if request.session.get(IMPERSONATOR_KEY) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Адмінка недоступна в режимі перегляду — вийди з нього спочатку.",
        )
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only.")
    return user


def login_session(request: Request, user: User) -> None:
    from app.core.impersonate import clear as _clear_impersonation

    # A fresh sign-in replaces whatever was there — including a borrowed session left
    # over from an admin who closed the tab instead of pressing "вийти з режиму".
    _clear_impersonation(request.session)
    request.session["user_id"] = user.id


def logout_session(request: Request) -> None:
    request.session.clear()
