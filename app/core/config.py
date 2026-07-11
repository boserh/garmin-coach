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
    # PERF-05: a process-wide, polite request pattern to Garmin's unofficial API
    # (post-Cloudflare an aggressive pattern risks an account ban, not just a 429).
    # GARMIN_RPS caps requests/sec across all threads (0 disables the limiter);
    # GARMIN_RETRIES is how many times a 429 is retried with exponential backoff.
    GARMIN_RPS: float = 3.0
    GARMIN_RETRIES: int = 2

    # --- Claude ---
    ANTHROPIC_API_KEY: Optional[str] = None
    # PERF-04b: Claude calls run on their own small thread pool (kept off anyio's
    # shared pool, which Garmin fetches/logins use) so LLM latency can't starve it.
    CLAUDE_MAX_WORKERS: int = 4

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

    # --- Disk cache (immutable Garmin assets; day-level cache lives in the DB,
    # the Claude dedup cache in the llm_cache table) ---
    # Per-key files under this directory (PERF-02 — cross-process safe).
    GARMIN_CACHE_DIR: str = "garmin_cache"
    # The legacy single-file cache: seeded into GARMIN_CACHE_DIR once, then renamed.
    GARMIN_CACHE_FILE: str = "garmin_cache.json"

    # --- Adaptive plan (EP-02) ---
    # Weekly review: hour (Europe/Warsaw) + day-of-week it runs on. python-telegram-bot's
    # JobQueue.run_daily ``days`` convention is 0=Sunday..6=Saturday.
    PLAN_ADAPT_HOUR: int = 20
    PLAN_ADAPT_WEEKLY_DOW: int = 0  # Sunday
    # Morning one-off nudge fires only when today's readiness score is below this AND
    # today's plan session is tempo/intervals/long.
    PLAN_ADAPT_READINESS_MIN: int = 50

    # --- Weekly digest (EP-07) ---
    # Sunday-evening retrospective (volume/compliance vs last week, recovery/fitness
    # trends, honest progress-to-goal). Same run_daily days convention as the adaptive
    # job (0=Sunday); scheduled before the adaptation review so the recap lands first.
    DIGEST_HOUR: int = 19
    DIGEST_WEEKLY_DOW: int = 0  # Sunday

    # --- Weather-aware planning (EP-13) ---
    # A daily check (Europe/Warsaw hour) that proposes moving a key session off an
    # extreme-weather day. Gated on a stored location + active plan + plan_adapt_enabled;
    # silent (zero Claude calls) when no key session hits an extreme day.
    WEATHER_PLAN_HOUR: int = 6
    WEATHER_DECISION_DAYS: int = 3       # only propose for sessions within N days ahead
    WEATHER_HEAT_FEELS_C: float = 30     # feels-like max °C at/above → heat conflict
    WEATHER_RAIN_PROB_PCT: float = 70    # precip probability % at/above → rain conflict
    WEATHER_WIND_KMH: float = 40         # max wind km/h at/above → wind conflict


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
