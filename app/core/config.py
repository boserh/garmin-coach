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
    # Which engine talks to Garmin Connect: "gconn" (default since OPS-10 — the
    # native python-garminconnect client) or "garth" (the deprecated pre-OPS-10 path,
    # kept as the rollback: pip install -e ".[garth]" + GARMIN_PROVIDER=garth).
    GARMIN_PROVIDER: str = "gconn"
    # PERF-05: a process-wide, polite request pattern to Garmin's unofficial API
    # (post-Cloudflare an aggressive pattern risks an account ban, not just a 429).
    # GARMIN_RPS caps requests/sec across all threads (0 disables the limiter);
    # GARMIN_RETRIES is how many times a 429 is retried with exponential backoff.
    GARMIN_RPS: float = 3.0
    GARMIN_RETRIES: int = 2
    # OPS-05: how many Garmin API failures in the last hour count as a "burst" worth a
    # one-a-day DM ("⚠️ Garmin API деградує"). 0 disables the burst DM (the /status
    # counters + dashboard banner still work). Expected 403s (known garth gaps) never
    # count toward the burst — see client._EXPECTED_ERROR_SUFFIXES.
    GARMIN_ERROR_BURST: int = 10
    # ST-18: how many recent stored days build_payload_cached will re-fetch if they turn out
    # incomplete (a day fixed at 7:05 with sleep but no HRV/readiness). Bounds the request
    # burst — only yesterday/day-before by default; older gaps are a manual /resync (ST-15).
    REFRESH_INCOMPLETE_DAYS: int = 2

    # --- Claude ---
    ANTHROPIC_API_KEY: Optional[str] = None
    # PERF-04b: Claude calls run on their own small thread pool (kept off anyio's
    # shared pool, which Garmin fetches/logins use) so LLM latency can't starve it.
    CLAUDE_MAX_WORKERS: int = 4

    # --- LLM budget circuit breaker (OPS-11) ---
    # Spend was measured (ReportLog.cost_usd, /costs) but never *capped*: a looping
    # adaptation, a retry storm, or plan generation on Opus with max_tokens=16000 fired
    # three times in a row had nothing but human discipline between it and the bill.
    # Enforced in app.analysis.budget from the single choke point every Claude call
    # goes through. Any of these set to 0 disables that particular ceiling (the tests
    # keep the defaults — a suite that spends $0 never approaches them).
    LLM_BUDGET_MONTH_USD: float = 25.0   # hard ceiling per calendar month, per user
    LLM_BUDGET_DAY_USD: float = 5.0      # hard ceiling per calendar day, per user
    LLM_BUDGET_WARN_PCT: float = 0.8     # DM once at this share of the monthly ceiling
    # Background work (morning report, digest, adaptation, auto-analysis) is switched off
    # at this share, before interactive commands are — an automatic job must not eat the
    # budget the human's own /report needs.
    LLM_BUDGET_SOFT_PCT: float = 0.9
    # Per-call ceiling on the *estimated* cost of one request (input estimate + the full
    # max_tokens output priced in). The real guard against a single Opus-16k call in a
    # loop, which no monthly average catches in time.
    LLM_MAX_CALL_USD: float = 2.0

    # --- Telegram ---
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    # Second, separate bot identity for the hidden system/admin commands (/deploy +
    # /test_*), run as its own process (bot.admin_main), off the main coaching bot.
    # Unset → bot.admin_main refuses to start.
    TELEGRAM_ADMIN_BOT_TOKEN: Optional[str] = None
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

    # Self-registration closes once this many accounts sit unapproved. The per-IP rate
    # limit above only slows a signup flood down — it resets every window, so a patient
    # script still fills the users table and buries the real applicants in the admin
    # queue. This is the standing ceiling: an admin approving or deleting the backlog is
    # what reopens the form. 0 disables the cap (unbounded signups).
    REGISTRATION_PENDING_MAX: int = 5

    # Secure-only session cookie + HSTS. Defaults ON: the deployed site is HTTPS, and
    # without the Secure flag the session cookie rides along on any plain-HTTP request
    # to the same host — readable by anyone on the path. Turn OFF only to develop over
    # http://localhost, where a Secure cookie is never stored and login just silently
    # fails to stick (the tests set it to false for the same reason).
    # HSTS is deliberately tied to the same switch: promising a browser "HTTPS only for
    # the next N seconds" is a one-way door, so it must never fire from a dev box.
    SESSION_HTTPS_ONLY: bool = True
    HSTS_MAX_AGE: int = 31536000       # 1 year; 0 omits the header entirely

    # --- Remote MCP server (NF-08, http transport) ---
    # The public HTTPS origin the MCP server is reached at, e.g. https://mcp.example.com.
    # It is the OAuth *issuer*, so it must match what the client actually connected to —
    # a mismatch fails discovery rather than degrading. Unset → `--transport http` refuses
    # to start (there is no safe default to guess), and stdio is unaffected.
    MCP_PUBLIC_URL: Optional[str] = None
    # Ceiling on dynamically registered OAuth clients (RFC 7591 registration is
    # unauthenticated by design — Claude registers itself — so it needs a bound; without
    # one it is an open row factory). Reached → registration is refused until an admin
    # clears the table.
    MCP_OAUTH_MAX_CLIENTS: int = 20

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

    # --- Lifestyle log (NF-28) ---
    # One tap in the same evening slot as the sleep nudge marks the day's everyday facts
    # (alcohol / late caffeine / late meal / stress / travel / feeling off). Zero LLM calls;
    # the tags become binary variables in NF-02's correlation engine, which until now could
    # only correlate what the watch itself reports. Off → the prompt is never sent (already
    # stored history stays, and /log keeps working).
    LIFESTYLE_LOG: bool = True

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

    # --- Auto sickness trigger (NF-18) ---
    # A streak of consecutive `missed` plan sessions (app.sickness, zero-LLM) PLUS an
    # actionable EP-08 health report turns into ONE ✅/❌ DM offering the NF-03 block
    # rebuild — the /sick flow for a user too ill to type /sick. Both conditions are
    # required: missed sessions alone are as likely to mean a business trip. The Claude
    # call happens only on ✅. Process-level on/off; per-user gates are alerts_enabled +
    # plan_adapt_enabled (it proposes a plan change).
    SICKNESS_AUTO: bool = True
    SICKNESS_MISSED_DAYS: int = 3    # consecutive missed sessions (last 7 days) to trigger
    SICKNESS_GUARD_DAYS: int = 7     # after a proposal (or its ❌), stay quiet this many days

    # --- Return-to-run protocol (NF-30) ---
    # Pain already reported on several recent runs turns into ONE ✅/❌ DM offering a
    # deterministic walk/run ladder (app.returntorun, zero LLM in the protocol itself — the
    # only paid call is the optional plan rebuild the user asks for on the way out). The
    # master switch exists because this is the feature that comes closest to the medical
    # boundary: the protocol describes load, never a diagnosis, and an owner who wants none
    # of it can turn it off outright.
    RETURN_TO_RUN: bool = True
    RETURN_PAIN_RUNS: int = 2        # pain on this many of the last RETURN_WINDOW_RUNS runs
    RETURN_WINDOW_RUNS: int = 5      # ...within this many most recent check-ins
    RETURN_GUARD_DAYS: int = 14      # after a proposal (or its ❌), stay quiet this many days

    # --- Backup freshness monitoring (OPS-08) ---
    # Where scripts/backup_db.py writes its rotated copies + the last_ok.json marker
    # app.backup_status reads (must match --dir when backup_db is invoked with a
    # non-default one, e.g. --dir /mnt/usb).
    BACKUP_DIR: str = "backups"
    # How many days of marker age count as "backups have stopped happening" — the
    # admin-only /status field + morning-tick DM threshold. 0 disables the DM (the
    # /status field still shows the raw age).
    BACKUP_WARN_DAYS: int = 3

    # --- Remote deploy from Telegram (OPS-03) ---
    # Master off-switch for the admin-only /deploy bot command (app.deploy: git pull +
    # systemctl restart via scripts/restart_services.sh). Off by default — flip it on
    # only once the sudoers grant (deploy/sudoers-garmin-deploy) is installed.
    DEPLOY_ENABLED: bool = False

    # --- Shoe mileage tracker (NF-15) ---
    # A pure-Python, zero-LLM tracker (app.gear): the morning tick refreshes the gear roster
    # + Garmin's own per-gear lifetime mileage (no local activity-gear linking — see the
    # module docstring for why) and warns once per pair past the wear threshold, again every
    # GEAR_REWARN_KM further. 0 disables the DM entirely (roster/mileage still refresh).
    GEAR_WEAR_KM: float = 700
    GEAR_REWARN_KM: float = 150

    # --- Intensity distribution (NF-24) ---
    # A pure-Python, zero-LLM read of HR time-in-zone (app.intensity): what share of weekly
    # TIME was actually easy, how much sat in the useless "grey zone", and how big the
    # anaerobic dose was. Before this, whole-session avg_hr was the only intensity signal in
    # the app — and it averages an interval session into a meaningless middle. Off → nothing
    # is fetched and every consumer stays silent (already-stored zones remain).
    INTENSITY_DISTRIBUTION: bool = True
    POLARIZATION_LOW_TARGET: float = 0.8   # target share of weekly time in zones 1-2
    GRAY_ZONE_MAX: float = 0.15            # above this share in zone 3 for several weeks → flag
    ANAEROBIC_WEEKLY_CAP: float = 8.0      # weekly sum of anaerobic training effect

    # --- Forward load forecast (NF-20) ---
    # A pure-Python, zero-LLM forecast (app.loadforecast): this ISO week's still-planned
    # sessions + the week's actual load so far, vs the trailing-4-weeks chronic average —
    # a forward-looking ACWR instead of a retrospective one. Display-only (/plan,
    # dashboard) + one context line in weekly plan adaptation.
    FORECAST_ACWR_WARN: float = 1.4    # forecast ACWR at/above this → yellow
    FORECAST_ACWR_HIGH: float = 1.6    # ...and at/above this → red


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
