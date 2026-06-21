"""Web auth — a shared-secret token check for the data/cost endpoints.

The token may be supplied either as ``Authorization: Bearer <token>`` or as an
``X-Token: <token>`` header. When ``WEB_TOKEN`` is empty the check is skipped
entirely (handy for local development).
"""
import secrets
from typing import Optional

from fastapi import Header, HTTPException, status

from app.core.config import settings


def verify_token(
    authorization: Optional[str] = Header(default=None),
    x_token: Optional[str] = Header(default=None),
) -> None:
    """FastAPI dependency: raise 401 unless the request carries the shared secret."""
    expected = settings.WEB_TOKEN
    if not expected:
        return  # auth disabled

    supplied = x_token
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:]

    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid token.",
        )
