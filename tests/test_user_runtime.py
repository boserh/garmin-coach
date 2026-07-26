"""Per-user provider, credential decryption, and runtime token persistence.

The provider under test is the native one (``_UserGConnProvider``, the default since
OPS-10); the garth twin it replaced is covered by ``test_garth_provider_rollback.py``,
which skips unless the rollback extra is installed.
"""
import json

import pytest
from cryptography.fernet import Fernet

from app.core import crypto
from app.db.models import User
from app.garmin import providers, runtime
from app.garmin.credentials import UserCredentials, load_credentials


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(crypto.settings, "APP_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_fernet", None)


def gconn_token(di_token="di", refresh="r") -> str:
    """A blob shaped like ``garminconnect.client.Client.dumps()``."""
    return json.dumps(
        {"di_token": di_token, "di_refresh_token": refresh, "di_client_id": "c"}
    )


class FakeGConnClient:
    """Stand-in for garminconnect.client.Client — no network."""

    def __init__(self):
        self.logged_in_with = None
        self._token = None

    def loads(self, token):
        if token == gconn_token(di_token="bad"):
            raise ValueError("stale token")
        self._token = token

    def login(self, email, password, prompt_mfa=None):
        self.logged_in_with = (email, password)
        self._token = gconn_token(di_token="fresh")

    def dumps(self):
        return self._token or gconn_token(di_token="fresh")

    def connectapi(self, path, **kwargs):
        if path == providers._PROFILE_PATH:
            return {"userName": "tester", "displayName": "Tester T"}
        return {"path": path, **kwargs}

    def post(self, _domain, path, **kwargs):
        kwargs.pop("api", None)
        return {"posted": path, **kwargs}

    def delete(self, _domain, path, **kwargs):
        kwargs.pop("api", None)
        return {"deleted": path}


@pytest.fixture
def fake_gconn(monkeypatch):
    monkeypatch.setattr(providers, "_gconn_client_cls", lambda: FakeGConnClient)


def test_provider_fresh_login_exposes_new_token(fake_gconn):
    creds = UserCredentials(user_id=1, garmin_email="e@x.com", garmin_password="p")
    p = providers.build_user_provider(creds)
    p.login()
    assert p.new_token == gconn_token(di_token="fresh")   # caller persists this
    assert p.username == "tester"
    assert p.display_name == "Tester T"


def test_provider_resumes_from_token_without_login(fake_gconn):
    creds = UserCredentials(user_id=1, garth_token=gconn_token())
    p = providers.build_user_provider(creds)
    p.login()
    assert p._client.logged_in_with is None
    assert p.new_token is None   # resumed, unchanged — nothing to persist


def test_provider_falls_back_when_token_stale(fake_gconn):
    creds = UserCredentials(user_id=1, garth_token=gconn_token(di_token="bad"),
                            garmin_email="e@x.com", garmin_password="p")
    p = providers.build_user_provider(creds)
    p.login()
    assert p.new_token == gconn_token(di_token="fresh")


def test_legacy_garth_token_triggers_one_fresh_login(fake_gconn, caplog):
    """OPS-10: a garth-format blob can't be converted (different token material), so
    the native engine ignores it and logs in once — quietly, not as an auth failure."""
    creds = UserCredentials(user_id=1, garth_token="eyJ0b2tlbiI6ICJnYXJ0aCJ9",
                            garmin_email="e@x.com", garmin_password="p")
    p = providers.build_user_provider(creds)
    with caplog.at_level("INFO", logger="garmin"):
        p.login()
    assert p._client.logged_in_with == ("e@x.com", "p")
    assert p.new_token == gconn_token(di_token="fresh")
    assert any("garth-format token" in r.message for r in caplog.records)
    # the loud "resume failed" warning is for a corrupt native blob, not this
    assert not any("resume failed" in r.message for r in caplog.records)


def test_refreshed_session_is_offered_for_persistence(fake_gconn):
    """The native client refreshes the DI token in place (and Garmin may rotate the
    refresh token with it) — no login happens, but the stored blob is now stale."""
    creds = UserCredentials(user_id=1, garth_token=gconn_token())
    p = providers.build_user_provider(creds)
    p.login()
    assert p.new_token is None
    p._client._token = gconn_token(di_token="refreshed")   # as _run_request would
    assert p.new_token == gconn_token(di_token="refreshed")


def test_resume_does_not_validate_with_network_call(monkeypatch):
    """A resumed token must NOT be validated with a live API call — a transient failure
    of such a call used to escalate to a full sso.garmin.com re-login, and a burst of
    those earns a Cloudflare 1015 ban (OPS-01). loads() succeeds → logged in, no fresh
    login, even if the profile fetch would blow up on the network."""
    calls = {"login": 0}

    class NetTouchClient(FakeGConnClient):
        def connectapi(self, path, **kwargs):  # simulate a rate-limited profile call
            raise RuntimeError("429 rate limited")

        def login(self, email, password, prompt_mfa=None):
            calls["login"] += 1
            super().login(email, password, prompt_mfa)

    monkeypatch.setattr(providers, "_gconn_client_cls", lambda: NetTouchClient)
    creds = UserCredentials(user_id=1, garth_token=gconn_token(),
                            garmin_email="e@x.com", garmin_password="p")
    p = providers.build_user_provider(creds)
    p.login()
    assert p._logged_in is True
    assert p.new_token is None      # resumed — no fresh login triggered
    assert calls["login"] == 0      # never hit the sso.garmin.com login path


def test_provider_without_credentials_raises(fake_gconn):
    p = providers.build_user_provider(UserCredentials(user_id=1))
    with pytest.raises(RuntimeError, match="No Garmin credentials"):
        p.login()


def test_connectapi_logs_in_lazily(fake_gconn):
    # ST-09: a flow that reaches Garmin without build_payload_cached (e.g. plan
    # generation's strength snapshot) must still authenticate — connectapi logs in itself.
    creds = UserCredentials(user_id=1, garth_token=gconn_token())
    p = providers.build_user_provider(creds)
    assert p._logged_in is False
    out = p.connectapi("/workout-service/workout/42")
    assert p._logged_in is True
    assert out == {"path": "/workout-service/workout/42"}


def test_write_calls_translate_gaths_method_kwarg(fake_gconn):
    """OPS-10: garth took the HTTP verb as a ``method=`` kwarg; the native connectapi is
    GET-only and would forward it into requests as a duplicate arg. push-plan/plan_sync
    write through this path, so the translation has to happen in the provider."""
    creds = UserCredentials(user_id=1, garth_token=gconn_token())
    p = providers.build_user_provider(creds)
    assert p.connectapi("/workout-service/workout", method="POST", json={"a": 1}) == {
        "posted": "/workout-service/workout", "json": {"a": 1},
    }
    assert p.connectapi("/workout-service/workout/9", method="DELETE") == {
        "deleted": "/workout-service/workout/9",
    }
    with pytest.raises(ValueError, match="Unsupported"):
        p.connectapi("/x", method="PATCH")


def test_write_translation_matches_the_real_client(monkeypatch):
    """The same translation against the REAL garminconnect client (transport stubbed at
    the lowest level): a fake can agree with a wrong idea of the library's API, this
    can't. Catches a signature drift on a version bump without touching the network."""
    from garminconnect.client import Client

    seen = []

    class Recording(Client):
        def _run_request(self, method, path, **kwargs):
            seen.append((method, path, kwargs))

            class Resp:
                @staticmethod
                def json():
                    return {"ok": path}

            return Resp()

    client = Recording()
    assert providers._gconn_connectapi(client, "/a/b") == {"ok": "/a/b"}
    assert seen[-1][:2] == ("GET", "/a/b")
    assert providers._gconn_connectapi(
        client, "/workout-service/workout", method="POST", json={"a": 1}
    ) == {"ok": "/workout-service/workout"}
    assert seen[-1] == ("POST", "/workout-service/workout", {"json": {"a": 1}})
    providers._gconn_connectapi(client, "/workout-service/workout/9", method="DELETE")
    assert seen[-1][:2] == ("DELETE", "/workout-service/workout/9")


def test_username_property_logs_in_lazily(fake_gconn):
    creds = UserCredentials(user_id=1, garmin_email="e@x.com", garmin_password="p")
    p = providers.build_user_provider(creds)
    assert p.username == "tester"      # triggers the fresh login
    assert p.new_token == gconn_token(di_token="fresh")


def test_profile_is_fetched_once_per_provider(fake_gconn):
    creds = UserCredentials(user_id=1, garth_token=gconn_token())
    p = providers.build_user_provider(creds)
    calls = {"n": 0}
    inner = p._client.connectapi

    def counting(path, **kwargs):
        if path == providers._PROFILE_PATH:
            calls["n"] += 1
        return inner(path, **kwargs)

    p._client.connectapi = counting
    assert p.username == "tester"
    assert p.display_name == "Tester T"
    assert calls["n"] == 1


def test_is_gconn_token_tells_the_two_formats_apart():
    assert providers.is_gconn_token(gconn_token()) is True
    assert providers.is_gconn_token("eyJ0b2tlbiI6ICJnYXJ0aCJ9") is False  # base64 garth
    assert providers.is_gconn_token('{"di_token": null}') is False        # empty session
    assert providers.is_gconn_token(None) is False
    assert providers.is_gconn_token("") is False


def test_build_user_provider_honours_rollback_switch(fake_gconn, monkeypatch):
    creds = UserCredentials(user_id=1, garth_token=gconn_token())
    assert isinstance(providers.build_user_provider(creds), providers._UserGConnProvider)
    monkeypatch.setattr(providers.settings, "GARMIN_PROVIDER", "garth")
    garth = pytest.importorskip("garth")  # the rollback extra isn't installed by default
    assert garth is not None
    assert isinstance(providers.build_user_provider(creds), providers._UserGarthProvider)


def test_get_provider_prefers_context(fake_gconn):
    sentinel = object()
    token = providers.set_current_provider(sentinel)
    try:
        assert providers.get_provider() is sentinel
    finally:
        providers.reset_current_provider(token)


def test_load_credentials_round_trip(key):
    user = User(
        id=7,
        email="x@e.com",
        password_hash="h",
        garmin_email_enc=crypto.encrypt("g@e.com"),
        garmin_password_enc=crypto.encrypt("garminpw"),
        anthropic_key_enc=crypto.encrypt("sk-ant"),
    )
    creds = load_credentials(user)
    assert creds.user_id == 7
    assert creds.garmin_email == "g@e.com"
    assert creds.garmin_password == "garminpw"
    assert creds.anthropic_key == "sk-ant"
    assert creds.garth_token is None
    assert creds.has_garmin is True


async def test_user_runtime_persists_fresh_token(session, key, monkeypatch):
    user = User(
        email="x@e.com", password_hash="h",
        garmin_email_enc=crypto.encrypt("g@e.com"),
        garmin_password_enc=crypto.encrypt("pw"),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    class FakeProvider:
        def __init__(self):
            self.new_token = None

    fake = FakeProvider()
    monkeypatch.setattr(providers, "build_user_provider", lambda creds: fake)

    async with runtime.user_runtime(session, user) as creds:
        assert creds.garmin_email == "g@e.com"
        assert providers.get_provider() is fake     # bound for the block
        fake.new_token = "minted-token"             # simulate a fresh login

    assert providers._current_provider.get() is None   # unbound after the block
    await session.refresh(user)
    assert crypto.decrypt(user.garth_token_enc) == "minted-token"
