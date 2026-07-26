"""Garmin Connect backends behind a common interface.

Two engines, selected by ``GARMIN_PROVIDER``:

* ``gconn`` — **the default since OPS-10**: the native ``python-garminconnect``
  client (``garminconnect.client.Client``, curl_cffi TLS impersonation). OPS-01's
  recon validated it against the live account from the production IP — login+MFA,
  token resume and every read endpoint we use: 0 FAIL.
* ``garth`` — the previous path (``garth==0.4.47``), deprecated upstream since
  2026-03-28 and kept **only as the rollback**: ``pip install -e ".[garth]"`` plus
  ``GARMIN_PROVIDER=garth`` restores the pre-OPS-10 behaviour byte for byte. garth
  is no longer a base dependency, so these classes raise ``ImportError`` when it
  isn't installed — that's the intended "rollback wasn't set up" signal.

Every provider exposes ``login()``, ``connectapi(path, **kwargs)`` and the
``username``/``display_name`` properties (the ids used to build endpoint URLs), so
``client.py`` stays engine-agnostic and needs no changes.
"""
import contextlib
import json
import logging
import os
import warnings
from contextvars import ContextVar
from functools import lru_cache
from typing import Optional

from app.core.config import settings

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

logger = logging.getLogger("garmin")

# Legacy single-user token dirs. Per-user tokens live encrypted in the DB
# (``_UserGConnProvider``/``_UserGarthProvider``); these only back the .env-seeded
# global fallback. It used to be the ``GARTH_TOKEN_DIR`` setting (CODE-03: dropped
# — nobody configured it away from the default). The two engines get separate dirs
# on purpose: their token files are NOT interchangeable (see ``is_gconn_token``),
# and writing one engine's session into the other's dir would break the rollback.
_LEGACY_TOKEN_DIR = os.path.expanduser("~/.garth")
_GCONN_TOKEN_DIR = os.path.expanduser("~/.garminconnect")


class _GarthProvider:
    """Legacy single-user garth provider — **rollback only** since OPS-10 (reachable
    with ``GARMIN_PROVIDER=garth`` once ``pip install -e ".[garth]"`` puts garth back).
    Left byte-for-byte as it was, so the fallback is the known-good code, not a port."""

    def __init__(self) -> None:
        import garth

        self._garth = garth
        self._token_dir = _LEGACY_TOKEN_DIR

    def login(self) -> None:
        garth = self._garth
        try:
            garth.resume(self._token_dir)  # local file read; no network validation touch
            return
        except Exception:
            # Only a missing/corrupt token dir lands here (resume is a local read).
            # We deliberately don't validate with a live API call — see the note in
            # _UserGarthProvider.login (avoids a transient blip escalating to a full
            # sso.garmin.com re-login → Cloudflare 1015 ban).
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

    @property
    def display_name(self) -> str:
        return self._garth.client.profile["displayName"]


def _gconn_client_cls():
    """Import seam for the native garminconnect client (patched in tests).

    We use ``garminconnect.client.Client`` directly rather than the ``Garmin``
    facade: the facade is a per-endpoint convenience API, while everything this
    project fetches goes through ``client.py``'s own path-based calls. The 0.2.x
    facade's ``api.garth`` is gone in 0.3.x anyway (OPS-01 run 1).
    """
    from garminconnect.client import Client

    return Client


# Garmin's own profile endpoint — the native client has no cached ``profile`` dict
# the way garth did, so the ids come from one lazy fetch per provider instance.
_PROFILE_PATH = "/userprofile-service/socialProfile"

# Keys of a native ``Client.dumps()`` blob (plain JSON). A garth blob is base64 of
# ``[oauth1, oauth2]`` — the two formats are NOT interchangeable (OPS-10).
_GCONN_TOKEN_KEYS = ("di_token", "di_refresh_token", "di_client_id")


def is_gconn_token(blob: Optional[str]) -> bool:
    """True if ``blob`` is a native garminconnect session (vs a legacy garth one).

    Cheap and local (a ``json.loads``), so a stored token from the *other* engine is
    recognised before we hand it to a client that would only raise on it.
    """
    if not blob:
        return False
    try:
        data = json.loads(blob)
    except Exception:
        return False
    return isinstance(data, dict) and any(data.get(k) for k in _GCONN_TOKEN_KEYS)


def _gconn_connectapi(client, path: str, **kwargs):
    """``connectapi`` with garth's calling convention on the native client.

    garth's ``connectapi(path, method="POST", json=...)`` took the HTTP method as a
    kwarg; the native ``Client.connectapi`` is GET-only and would forward a stray
    ``method=`` straight into ``requests.Session.request`` as a duplicate argument
    (a TypeError). Our write calls — ``client.create_workout``/``schedule_workout``/
    ``delete_workout`` (push-plan, plan_sync) — are exactly that shape, so the
    translation lives here, in the one place both engines are bridged.
    """
    method = str(kwargs.pop("method", "GET")).upper()
    if method == "GET":
        return client.connectapi(path, **kwargs)
    write = {"POST": "post", "PUT": "put", "DELETE": "delete"}.get(method)
    if write is None:
        raise ValueError(f"Unsupported Garmin request method: {method}")
    # api=True → the parsed JSON body (a 204 comes back as {}), matching garth.
    return getattr(client, write)("connectapi", path, api=True, **kwargs)


class _GConnBase:
    """Shared plumbing for both native providers: request translation + the profile
    ids. Subclasses own ``self._client`` and their ``login()``."""

    _client = None
    _profile_cache: Optional[dict] = None

    def login(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def connectapi(self, path: str, **kwargs):
        # Log in lazily: a flow reaching Garmin without a payload build (plan
        # generation's strength snapshot, ST-09) must still authenticate. login()
        # is idempotent and a plain loads() when a stored session exists.
        self.login()
        return _gconn_connectapi(self._client, path, **kwargs)

    def _profile(self) -> dict:
        if self._profile_cache is None:
            self._profile_cache = self.connectapi(_PROFILE_PATH) or {}
        return self._profile_cache

    @property
    def username(self) -> str:
        return self._profile()["userName"]

    @property
    def display_name(self) -> str:
        return self._profile()["displayName"]


class _GConnProvider(_GConnBase):
    """Legacy single-user native provider: ``.env`` credentials + a token dir on
    disk. Only the .env-seeded global fallback uses it (per-user runtimes get
    ``_UserGConnProvider``); interactive MFA on the console, like the garth twin."""

    def __init__(self) -> None:
        self._client = _gconn_client_cls()()
        self._token_dir = _GCONN_TOKEN_DIR
        self._logged_in = False
        self._profile_cache = None

    def login(self) -> None:
        if self._logged_in:
            return
        try:
            self._client.load(self._token_dir)  # local file read; no network touch
            self._logged_in = True
            return
        except Exception:
            # Only a missing/corrupt token file lands here — see the note in
            # _UserGConnProvider.login on why a resume is never network-validated.
            pass
        email = settings.GARMIN_EMAIL or os.environ["GARMIN_EMAIL"]
        password = settings.GARMIN_PASSWORD or os.environ["GARMIN_PASSWORD"]
        self._client.login(email, password, prompt_mfa=lambda: input("MFA код: "))
        self._logged_in = True
        with contextlib.suppress(Exception):
            self._client.dump(self._token_dir)


class _UserGarthProvider:
    """Per-user garth provider backed by an isolated ``garth.Client`` (no shared
    global state). Resumes from a stored session token when present, otherwise logs
    in with email+password (no MFA) and exposes the fresh token via ``new_token`` so
    the caller can persist it. Garmin endpoints/usage match ``_GarthProvider``.

    **Rollback only** since OPS-10 — ``_UserGConnProvider`` is what runs by default;
    this class is kept untouched so ``GARMIN_PROVIDER=garth`` restores the exact
    pre-migration behaviour (its stored garth-format tokens included)."""

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
                # NB: DON'T validate the resumed session with a live API call here.
                # loads() only restores the OAuth1/OAuth2 tokens (no network); a profile
                # touch would hit Garmin on EVERY login, and — worse — any transient
                # failure of that call (a 429 rate-limit, a network blip) would land in
                # the except below and escalate to a full sso.garmin.com re-login. A burst
                # of those is exactly what earns a Cloudflare 1015 IP ban (OPS-01). garth
                # refreshes the OAuth2 token from OAuth1 on demand; a genuinely dead token
                # (rare — OAuth1 lasts ~1 year) surfaces on the first real call and the
                # user re-connects via /settings. So we only reach the fallback when
                # loads() itself fails (a corrupt/unparseable stored token — local, no net).
                self._logged_in = True
                return
            except Exception as exc:
                # Corrupt/unparseable stored token — fall back to a fresh login. OPS-01
                # monitoring: if these start appearing for tokens that aren't ~1 year old,
                # Garmin likely broke the OAuth2 exchange — check for GARMIN AUTH FAIL
                # right after (the migration trigger).
                logger.warning(
                    "GARMIN AUTH: stored token resume failed for user %s (%r) — "
                    "falling back to fresh login", self._creds.user_id, exc,
                )
        email, password = self._creds.garmin_email, self._creds.garmin_password
        if not email or not password:
            raise RuntimeError("No Garmin credentials configured for this user.")
        from app.garmin.mfa import start_login  # local import: avoid a cycle at module load

        start_login(self._creds.user_id, self._client, email, password)
        self._logged_in = True
        self.new_token = self._client.dumps()

    def connectapi(self, path: str, **kwargs):
        # Ensure the garth client is authenticated. Most paths go through
        # build_payload_cached, which logs in first; but run_plan_generation (and any
        # other flow reaching Garmin without a payload build) never did, so the client
        # stayed an empty garth.Client() and every call blew up on
        # `assert self.oauth1_token` — silently emptying the strength snapshot (ST-09).
        # login() is guarded/idempotent and a plain loads() when a valid token exists.
        self.login()
        return self._client.connectapi(path, **kwargs)

    @property
    def username(self) -> str:
        self.login()
        return self._client.profile["userName"]

    @property
    def display_name(self) -> str:
        self.login()
        return self._client.profile["displayName"]


class _UserGConnProvider(_GConnBase):
    """Per-user native provider — the OPS-10 successor to ``_UserGarthProvider``.

    Same contract: isolated state per user, resume from the session blob stored
    (encrypted) in the DB, expose a freshly minted one via ``new_token`` so
    ``runtime.user_runtime`` can persist it. Two behaviours differ from the garth
    twin, both forced by the new engine:

    * **Token format.** The native ``dumps()`` is plain JSON of the DI bearer +
      refresh token, not garth's base64 ``[oauth1, oauth2]``. Old blobs can't be
      converted (they hold OAuth1 material the new engine doesn't use), so a user
      still carrying a garth token gets ONE silent fresh login — deliberate and
      cheap, since the alternative is a conversion that can only guess.
    * **``new_token`` is computed, not assigned.** The native client refreshes the
      DI token in-place (and Garmin may rotate the refresh token with it), so the
      session can change during a runtime without any login happening. Comparing
      the current dump against the loaded one catches both cases with no extra
      wiring in ``user_runtime``.
    """

    def __init__(self, creds) -> None:
        self._client = _gconn_client_cls()()
        self._creds = creds
        self._logged_in = False
        self._loaded_token: Optional[str] = None
        self._profile_cache = None

    def login(self) -> None:
        if self._logged_in:
            return
        stored = self._creds.garth_token
        if is_gconn_token(stored):
            try:
                self._client.loads(stored)
                # NB: DON'T validate the resumed session with a live API call here.
                # loads() only restores the stored tokens (no network); a profile touch
                # would hit Garmin on EVERY login, and any transient failure of that call
                # (a 429, a network blip) would escalate to a full login — a burst of
                # those is what earns a Cloudflare 1015 IP ban (OPS-01). The native client
                # refreshes an expired DI token from the refresh token on the first real
                # request, so a genuinely dead session surfaces there and the user
                # re-connects via /settings.
                self._loaded_token = stored
                self._logged_in = True
                return
            except Exception as exc:
                # A corrupt/unparseable stored token — local, no network. OPS-01
                # monitoring: if these appear for fresh tokens, check whether a
                # `GARMIN AUTH FAIL` follows (the auth engine breaking).
                logger.warning(
                    "GARMIN AUTH: stored token resume failed for user %s (%r) — "
                    "falling back to fresh login", self._creds.user_id, exc,
                )
        elif stored:
            logger.info(
                "GARMIN AUTH: user %s still holds a garth-format token — one-time "
                "fresh login on the native engine (OPS-10)", self._creds.user_id,
            )
        email, password = self._creds.garmin_email, self._creds.garmin_password
        if not email or not password:
            raise RuntimeError("No Garmin credentials configured for this user.")
        from app.garmin.mfa import start_login  # local import: avoid a cycle at module load

        start_login(self._creds.user_id, self._client, email, password)
        self._logged_in = True

    @property
    def new_token(self) -> Optional[str]:
        """The current session blob when it differs from the stored one (a fresh
        login, or an in-place DI-token refresh), else ``None``."""
        if not self._logged_in:
            return None
        try:
            blob = self._client.dumps()
        except Exception:  # a client that never authenticated has nothing to save
            return None
        if not is_gconn_token(blob) or blob == self._loaded_token:
            return None
        return blob


def build_user_provider(creds):
    """A fresh provider bound to one user's credentials (see ``credentials.py``).

    Native by default; ``GARMIN_PROVIDER=garth`` is the OPS-10 rollback switch.
    """
    if settings.GARMIN_PROVIDER.lower() == "garth":
        return _UserGarthProvider(creds)
    return _UserGConnProvider(creds)


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
    if settings.GARMIN_PROVIDER.lower() == "garth":
        return _GarthProvider()
    return _GConnProvider()


def get_provider():
    """Provider for the current context: the per-user one if set, else the legacy
    global. Fetch/aggregation code calls this and needs no per-user awareness."""
    return _current_provider.get() or _default_provider()
