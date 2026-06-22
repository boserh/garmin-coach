"""Decrypt a user's stored credentials into a plain runtime object.

Keeps decryption in one place: the ORM holds Fernet tokens, everything downstream
(provider, Anthropic client) wants plaintext. Only used inside the per-user runtime
context (see ``app.garmin.runtime``)."""
from dataclasses import dataclass
from typing import Optional

from app.core.crypto import decrypt
from app.db.models import User


@dataclass
class UserCredentials:
    user_id: int
    garmin_email: Optional[str] = None
    garmin_password: Optional[str] = None
    anthropic_key: Optional[str] = None
    garth_token: Optional[str] = None

    @property
    def has_garmin(self) -> bool:
        return bool((self.garmin_email and self.garmin_password) or self.garth_token)


def load_credentials(user: User) -> UserCredentials:
    """Decrypt a User's stored secrets. Raises if APP_SECRET_KEY is unset."""
    def dec(token):
        return decrypt(token) if token else None

    return UserCredentials(
        user_id=user.id,
        garmin_email=dec(user.garmin_email_enc),
        garmin_password=dec(user.garmin_password_enc),
        anthropic_key=dec(user.anthropic_key_enc),
        garth_token=dec(user.garth_token_enc),
    )
