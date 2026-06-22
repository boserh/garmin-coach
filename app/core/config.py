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

    # --- Web layer ---
    # Shared-secret for the data/cost endpoints. Empty string disables auth.
    WEB_TOKEN: str = ""

    # --- Database ---
    # Default SQLite runs zero-config on a Raspberry Pi; switch to Postgres by
    # setting DATABASE_URL=postgresql+asyncpg://... — no code changes needed.
    DATABASE_URL: str = "sqlite+aiosqlite:///./garmin.db"

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
