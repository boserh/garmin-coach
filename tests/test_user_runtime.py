"""Per-user provider, credential decryption, and runtime token persistence."""
import garth
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


class FakeGarthClient:
    """Stand-in for garth.Client — no network."""

    def __init__(self):
        self._profile = {"userName": "tester"}
        self.logged_in_with = None

    def loads(self, token):
        if token == "bad":
            raise ValueError("stale token")

    @property
    def username(self):
        return "tester"

    def login(self, email, password):
        self.logged_in_with = (email, password)

    def dumps(self):
        return "fresh-token"

    @property
    def profile(self):
        return self._profile

    def connectapi(self, path, **kwargs):
        return {"path": path}


@pytest.fixture
def fake_garth(monkeypatch):
    monkeypatch.setattr(garth, "Client", FakeGarthClient)


def test_provider_fresh_login_exposes_new_token(fake_garth):
    creds = UserCredentials(user_id=1, garmin_email="e@x.com", garmin_password="p")
    p = providers.build_user_provider(creds)
    p.login()
    assert p.new_token == "fresh-token"   # caller persists this
    assert p.username == "tester"


def test_provider_resumes_from_token_without_login(fake_garth):
    creds = UserCredentials(user_id=1, garth_token="good")
    p = providers.build_user_provider(creds)
    p.login()
    assert p.new_token is None   # resumed, no fresh login


def test_provider_falls_back_when_token_stale(fake_garth):
    creds = UserCredentials(user_id=1, garth_token="bad",
                            garmin_email="e@x.com", garmin_password="p")
    p = providers.build_user_provider(creds)
    p.login()
    assert p.new_token == "fresh-token"


def test_provider_without_credentials_raises(fake_garth):
    p = providers.build_user_provider(UserCredentials(user_id=1))
    with pytest.raises(RuntimeError, match="No Garmin credentials"):
        p.login()


def test_get_provider_prefers_context(fake_garth):
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
