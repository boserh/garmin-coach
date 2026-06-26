"""Application settings — single typed source of truth, read from the environment.

Replaces the scattered ``os.environ[...]`` lookups across the old flat modules.
Values come from the process environment and an optional ``.env`` file.
"""
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Garmin ---
    GARMIN_EMAIL: Optional[str] = None
    GARMIN_PASSWORD: Optional[str] = None
    # Which backend talks to Garmin Connect: "garth" (working) or "gconn" (untested).
    GARMIN_PROVIDER: str = "garth"
    GARTH_TOKEN_DIR: str = "~/.garth"

    # --- Claude ---
    ANTHROPIC_API_KEY: Optional[str] = None

    # --- Telegram ---
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[int] = None
    # Bot's public @username (without @) — used to render a t.me/ link in the web
    # onboarding so users know which bot to message. Override via env if it changes.
    TELEGRAM_BOT_USERNAME: Optional[str] = "garmim_coach_bot"

    # --- Web layer ---
    # Shared-secret for the data/cost endpoints. Empty string disables auth.
    # Legacy: superseded by per-user login; kept as an optional fallback.
    WEB_TOKEN: str = ""

    # --- Auth / secrets ---
    # Master key for Fernet credential encryption AND cookie-session signing.
    # Generate with: Fernet.generate_key().decode()  (see app/core/crypto.py docstring)
    # Empty disables encryption/login plumbing (so existing single-user .env still runs).
    APP_SECRET_KEY: str = ""

    # --- Database ---
    # Default SQLite runs zero-config on a Raspberry Pi; switch to Postgres by
    # setting DATABASE_URL=postgresql+asyncpg://... — no code changes needed.
    DATABASE_URL: str = "sqlite+aiosqlite:///./garmin.db"
    # DB_ECHO=true logs every SQL statement (reads + writes) to the logs. Verbose;
    # turn on to watch DB activity, then `journalctl -u garmin-web -f`.
    DB_ECHO: bool = False

    # --- Logging ---
    LOG_FILE: str = "bot.log"
    LOG_LEVEL: str = "INFO"

    # --- Disk caches (immutable-asset fetches; day-level cache lives in the DB) ---
    CLAUDE_CACHE_FILE: str = "claude_cache.json"
    GARMIN_CACHE_FILE: str = "garmin_cache.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
