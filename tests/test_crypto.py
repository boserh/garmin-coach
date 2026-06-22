"""Credential encryption + password hashing round-trips (no key in the env needed —
the test supplies one)."""
import pytest
from cryptography.fernet import Fernet

from app.core import crypto


@pytest.fixture
def key(monkeypatch):
    """Give crypto a fresh Fernet key and reset its cached instance."""
    monkeypatch.setattr(crypto.settings, "APP_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_fernet", None)


def test_encrypt_round_trip(key):
    token = crypto.encrypt("garmin-pass-123")
    assert token != "garmin-pass-123"          # actually encrypted
    assert crypto.decrypt(token) == "garmin-pass-123"


def test_encrypt_is_nondeterministic(key):
    # Fernet embeds a random IV, so the same plaintext encrypts to different tokens
    assert crypto.encrypt("x") != crypto.encrypt("x")


def test_encrypt_without_key_raises(monkeypatch):
    monkeypatch.setattr(crypto.settings, "APP_SECRET_KEY", "")
    monkeypatch.setattr(crypto, "_fernet", None)
    with pytest.raises(RuntimeError, match="APP_SECRET_KEY"):
        crypto.encrypt("x")


def test_password_hash_and_verify():
    h = crypto.hash_password("s3cret")
    assert h != "s3cret"
    assert crypto.verify_password("s3cret", h)
    assert not crypto.verify_password("wrong", h)


def test_verify_password_tolerates_garbage_hash():
    assert crypto.verify_password("x", "not-a-bcrypt-hash") is False
