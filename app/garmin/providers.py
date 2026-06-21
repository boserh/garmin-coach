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
from functools import lru_cache

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


@lru_cache
def get_provider():
    """Return the configured provider singleton."""
    name = settings.GARMIN_PROVIDER.lower()
    if name == "gconn":
        return _GConnProvider()
    return _GarthProvider()
