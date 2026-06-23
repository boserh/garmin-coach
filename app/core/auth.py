"""Session-based web auth: the ``current_user`` dependency and login helpers.

A successful login stores ``user_id`` in the signed cookie session (starlette
``SessionMiddleware``, keyed by ``APP_SECRET_KEY``). ``current_user`` resolves that
back to a :class:`User`; when there is no valid session it raises
:class:`RequiresLogin`, which an app-level handler turns into a redirect to /login
(nicer than a 401 for the browser UI).
"""
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

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
            return user
    raise RequiresLogin()


async def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only.")
    return user


def login_session(request: Request, user: User) -> None:
    request.session["user_id"] = user.id


def logout_session(request: Request) -> None:
    request.session.clear()
