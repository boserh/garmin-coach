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

    # --- Auth / secrets ---
    # Master key for Fernet credential encryption AND cookie-session signing.
    # Generate with: Fernet.generate_key().decode()  (see app/core/crypto.py docstring)
    # Empty disables encryption/login plumbing (so existing single-user .env still runs).
    APP_SECRET_KEY: str = ""

    # --- Web login hardening (SEC-01) ---
    # In-memory, per-process sliding-window rate limit on POST /login (keyed per-IP
    # AND per-email) and POST /register (per-IP). 0 disables it (the tests set it to
    # 0 so a fixture can log in repeatedly). See app.core.ratelimit for the trade-offs.
    LOGIN_RATE_LIMIT: int = 5          # max attempts per window before a 429
    LOGIN_RATE_WINDOW_S: int = 300     # window length in seconds (default 5 min)

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

    # --- Open-ended "keep improving" plan (rolling blocks) ---
    # The `general` goal has no target race: generation lays a block of PLAN_BLOCK_WEEKS
    # weeks. When the plan's last workout falls within PLAN_EXTEND_LEAD_DAYS the morning
    # tick asks (✅/❌) whether to add the next block — confirm-only, never auto-generated.
    # An explicit ❌ snoozes the nudge for PLAN_EXTEND_SNOOZE_DAYS; an ignored one re-asks
    # next morning.
    PLAN_BLOCK_WEEKS: int = 6
    PLAN_EXTEND_LEAD_DAYS: int = 10
    PLAN_EXTEND_SNOOZE_DAYS: int = 3

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

    # --- Heat/duration fueling advisor (NF-11) ---
    # A pure-Python calculator (app.fueling) folds fluid/carb/electrolyte guidance into the
    # morning report's context for TODAY's key session (tempo/intervals/long) only — no
    # extra Claude call, rides inside the existing daily report. Silent (no context key) for
    # a short/easy session or a cool day short enough not to need it.
    FUELING_MIN_DURATION_MIN: int = 45   # below this estimated duration, stay silent
    FUELING_HEAT_FEELS_C: float = 28     # feels-like max °C at/above → heat notes

    # --- Evening sleep-debt nudge (NF-16) ---
    # A pure-Python, zero-LLM check (app.sleepnudge) the evening before a heavy session:
    # only nudges when BOTH tomorrow is a key session AND recent sleep shows a debt signal.
    # Process-level on/off; per-user opt-out reuses User.alerts_enabled (same wellness-push
    # class as EP-08). The job's own run_daily hour stays on the process TZ in v1 (ST-14).
    SLEEP_NUDGE: bool = True
    SLEEP_NUDGE_HOUR: int = 21

    # --- Injury-risk radar (NF-04) ---
    # A pure-Python detector combines load-side signals (ACWR trend, repeated pain, RPE/pace
    # divergence, HRV/RHR drift) into a severity score; on a high score the morning tick sends
    # one advisory. Process-level on/off (personal app, single owner — no per-user column).
    INJURY_RADAR: bool = True
    INJURY_MIN_HISTORY_DAYS: int = 14    # quiet calibration: no warnings until this much history
    INJURY_GUARD_DAYS: int = 5           # at most one injury advisory per this many days

    # --- Proactive health alerts (EP-08) ---
    # A pure-Python detector flags sustained recovery anomalies (HRV/RHR/sleep/stress drifting
    # outside the user's PERSONAL baseline band for several days) and the morning tick pushes
    # one advisory, guarded per-rule. Thresholds are personal (NF-01 percentile bands), so the
    # cold-start is naturally quiet. Process-level on/off; per-user opt-out is User.alerts_enabled.
    HEALTH_ALERTS: bool = True
    HEALTH_MIN_HISTORY_DAYS: int = 7      # no alert until at least a week of history (cold-start)
    HEALTH_ALERT_COOLDOWN_DAYS: int = 3   # same alert kind at most once per this many days


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
