"""OPS-10 rollback path: the garth provider still behaves as it did pre-migration.

garth is no longer a base dependency (it moved to the ``garth`` extra), so this module
skips unless the rollback is actually installed — exactly the situation in which these
assertions matter: someone has run ``pip install -e ".[garth]"`` and flipped
``GARMIN_PROVIDER=garth`` because the native engine broke.
"""
import pytest

from app.garmin import providers
from app.garmin.credentials import UserCredentials

garth = pytest.importorskip("garth", reason="rollback extra not installed")


class FakeGarthClient:
    """Stand-in for garth.Client — no network."""

    def __init__(self):
        self._profile = {"userName": "tester", "displayName": "Tester T"}
        self.logged_in_with = None

    def loads(self, token):
        if token == "bad":
            raise ValueError("stale token")

    def login(self, email, password, prompt_mfa=None):
        self.logged_in_with = (email, password)

    def dumps(self):
        return "fresh-token"

    @property
    def profile(self):
        return self._profile

    def connectapi(self, path, **kwargs):
        return {"path": path}


@pytest.fixture
def rollback(monkeypatch):
    monkeypatch.setattr(garth, "Client", FakeGarthClient)
    monkeypatch.setattr(providers.settings, "GARMIN_PROVIDER", "garth")


def test_rollback_provider_fresh_login_exposes_new_token(rollback):
    creds = UserCredentials(user_id=1, garmin_email="e@x.com", garmin_password="p")
    p = providers.build_user_provider(creds)
    assert isinstance(p, providers._UserGarthProvider)
    p.login()
    assert p.new_token == "fresh-token"
    assert p.username == "tester"


def test_rollback_provider_resumes_stored_garth_token(rollback):
    """The point of the rollback: the garth-format blobs still in the DB keep working."""
    p = providers.build_user_provider(UserCredentials(user_id=1, garth_token="good"))
    p.login()
    assert p._logged_in is True
    assert p.new_token is None


def test_rollback_provider_falls_back_when_token_stale(rollback):
    creds = UserCredentials(user_id=1, garth_token="bad",
                            garmin_email="e@x.com", garmin_password="p")
    p = providers.build_user_provider(creds)
    p.login()
    assert p.new_token == "fresh-token"
