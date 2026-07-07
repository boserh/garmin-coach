"""Secret handling: Fernet encryption for stored credentials + password hashing.

Credentials (Garmin email/password, Anthropic key, garth token) are encrypted at
rest with a single master key, ``settings.APP_SECRET_KEY`` — a Fernet key, e.g.::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

The same key signs cookie sessions (see ``app.main``). The Fernet instance is built
lazily so importing this module never requires a key (tests, CLI tooling); calling
:func:`encrypt`/:func:`decrypt` without one raises a clear error.

Login passwords are hashed with bcrypt — never encrypted (they must not be
recoverable). Encryption is for credentials we have to replay to upstream services.

bcrypt is deliberately slow (~100–300 ms); calling it straight from an async route
blocks the whole event loop, so async callers use the ``*_async`` variants that
offload to a thread (PERF-04a). The sync versions remain for sync callers (CLI).
"""
import asyncio

import bcrypt
from cryptography.fernet import Fernet

from app.core.config import settings

_fernet: Fernet = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = settings.APP_SECRET_KEY
        if not key:
            raise RuntimeError(
                "APP_SECRET_KEY is not set — cannot encrypt/decrypt credentials. "
                "Generate one: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a secret for storage. Returns a urlsafe token string."""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Decrypt a stored secret produced by :func:`encrypt`."""
    return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")


def hash_password(password: str) -> str:
    """Hash a login password with bcrypt. Returns the encoded hash string."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    """Check a password against a stored bcrypt hash (constant-time inside bcrypt)."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


async def hash_password_async(password: str) -> str:
    """Async wrapper for :func:`hash_password` — runs bcrypt off the event loop."""
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, hashed: str) -> bool:
    """Async wrapper for :func:`verify_password` — runs bcrypt off the event loop."""
    return await asyncio.to_thread(verify_password, password, hashed)
