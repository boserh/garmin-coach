"""Shared FastAPI dependencies, re-exported from one place for the routers."""
from app.db.session import get_session

__all__ = ["get_session"]
