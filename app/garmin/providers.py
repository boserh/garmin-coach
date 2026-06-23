"""Garmin Connect backends behind a common interface.

Two providers, selected by ``GARMIN_PROVIDER``:

* ``garth``  — the working, battle-tested path (unofficial endpoints, token at
  ``~/.garth``, first run needs interactive MFA). Logic preserved verbatim from
  the old ``garmin_client.login`` / ``garth.connectapi`` usage.
* ``gconn``  — a thin wrapper over the ``garminconnect`` library. NOT yet tested
  against the live API; ported on a best-effort basis and intentionally left
  unmodified beyond what the interface requires. Do not rely on it in production.

Both expose ``login()``, ``connectapi(path, **kwargs)`` and a ``username``
property (the ``userName`` used to build the sleep endpoint URL).
"""
import os
import warnings
from contextvars import ContextVar
from functools import lru_cache
from typing import Optional

from app.core.config import settings

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")


class _GarthProvider:
    """The known-good provider. Mirrors the original module-level garth calls."""

    def __init__(self) -> None:
        import garth

        self._garth = garth
        self._token_dir = os.path.expanduser(settings.GARTH_TOKEN_DIR)

    def login(self) -> None:
        garth = self._garth
        try:
            garth.resume(self._token_dir)
            garth.client.username
            return
        except Exception:
            pass
        email = settings.GARMIN_EMAIL or os.environ["GARMIN_EMAIL"]
        password = settings.GARMIN_PASSWORD or os.environ["GARMIN_PASSWORD"]
        garth.login(email, password, prompt_mfa=lambda: input("MFA код: "))
        garth.save(self._token_dir)

    def connectapi(self, path: str, **kwargs):
        return self._garth.connectapi(path, **kwargs)

    @property
    def username(self) -> str:
        return self._garth.client.profile["userName"]


class _GConnProvider:
    """garminconnect-based provider. UNTESTED against the live API — kept as a
    straightforward port so it can be validated later without surprises."""

    def __init__(self) -> None:
        from garminconnect import Garmin

        self._Garmin = Garmin
        self._token_dir = os.path.expanduser(settings.GARTH_TOKEN_DIR)
        self._api = None

    def login(self) -> None:
        # Try resuming a stored token first, then fall back to a fresh login.
        try:
            self._api = self._Garmin()
            self._api.login(self._token_dir)
            return
        except Exception:
            pass
        email = settings.GARMIN_EMAIL or os.environ["GARMIN_EMAIL"]
        password = settings.GARMIN_PASSWORD or os.environ["GARMIN_PASSWORD"]
        self._api = self._Garmin(email=email, password=password)
        self._api.login()
        try:
            self._api.garth.dump(self._token_dir)
        except Exception:
            pass

    def connectapi(self, path: str, **kwargs):
        return self._api.connectapi(path, **kwargs)

    @property
    def username(self) -> str:
        return self._api.garth.profile["userName"]


class _UserGarthProvider:
    """Per-user garth provider backed by an isolated ``garth.Client`` (no shared
    global state). Resumes from a stored session token when present, otherwise logs
    in with email+password (no MFA) and exposes the fresh token via ``new_token`` so
    the caller can persist it. Garmin endpoints/usage match ``_GarthProvider``."""

    def __init__(self, creds) -> None:
        from garth import Client

        self._client = Client()
        self._creds = creds
        self._logged_in = False
        self.new_token: Optional[str] = None  # set after a fresh login, for persistence

    def login(self) -> None:
        if self._logged_in:
            return
        if self._creds.garth_token:
            try:
                self._client.loads(self._creds.garth_token)
                _ = self._client.username  # touch profile to validate the session
                self._logged_in = True
                return
            except Exception:
                pass  # stale/invalid token — fall back to a fresh login
        email, password = self._creds.garmin_email, self._creds.garmin_password
        if not email or not password:
            raise RuntimeError("No Garmin credentials configured for this user.")
        self._client.login(email, password)
        self._logged_in = True
        self.new_token = self._client.dumps()

    def connectapi(self, path: str, **kwargs):
        return self._client.connectapi(path, **kwargs)

    @property
    def username(self) -> str:
        return self._client.profile["userName"]


def build_user_provider(creds) -> _UserGarthProvider:
    """A fresh provider bound to one user's credentials (see ``credentials.py``)."""
    return _UserGarthProvider(creds)


# The provider in effect for the current request/command. When set (per-user
# runtime), it overrides the legacy global; the fetch layer reads it via
# ``get_provider`` with no signature changes. ContextVars propagate into the
# threadpool workers anyio uses, so blocking fetches see the right provider.
_current_provider: ContextVar = ContextVar("garmin_provider", default=None)


def set_current_provider(provider) -> object:
    return _current_provider.set(provider)


def reset_current_provider(token) -> None:
    _current_provider.reset(token)


@lru_cache
def _default_provider():
    """The legacy single-user provider from .env (back-compat / fallback)."""
    name = settings.GARMIN_PROVIDER.lower()
    if name == "gconn":
        return _GConnProvider()
    return _GarthProvider()


def get_provider():
    """Provider for the current context: the per-user one if set, else the legacy
    global. Fetch/aggregation code calls this and needs no per-user awareness."""
    return _current_provider.get() or _default_provider()
