# Bihun

[![CI](https://github.com/boserh/garmin-coach/actions/workflows/ci.yml/badge.svg)](https://github.com/boserh/garmin-coach/actions/workflows/ci.yml)

**Bihun** — a personal Garmin → Claude analyzer with a **shared core** reused by two front-ends: a
**Telegram bot** and a **FastAPI web layer**. It pulls health and training data,
aggregates it into compact daily summaries, sends those to Claude for analysis, and
persists history/cost in a database — growing from a "smart daily report" into an
adaptive AI training coach (plan generation, weekly adaptation, plan-vs-actual
matching, health/injury radars, and more).

The project is designed for personal use. Development and testing are done on macOS,
with a Raspberry Pi 4 (4 GB RAM) as the target deployment platform (systemd services).

```text
Telegram bot / Web API
    ↓
app.garmin.service  (fetch + aggregation, DB-backed cache, per-user)
    ↓
Claude API (app.analysis)
    ↓
Telegram reply / JSON response   +   history & cost in the DB
```

See `CLAUDE.md` for the full module map, architecture, and per-feature design notes
(`docs/backlog/`); this README stays a lighter installation/overview doc.

## Features

* Daily recovery analysis, morning automated reports (optionally weather-aware) —
  per-user timezone, once-a-day guard
* On-demand reports via Telegram commands and the web (`/report.json`, `/deep`)
* `/ask` — a tool-use agent answering follow-up questions over your **entire** stored
  history (not just recent reports), with a recent-thread context window
* Per-activity analysis (`/activities` to list, `/activity <id>` to analyze pace, HR,
  power, grade-adjusted pace and effort); writeup saved and shown on the web detail page
* Training plans: pick a goal on the web (`/plan`) or in the bot, get a generated dated
  program (running, and optional cycling sessions); edit it in plain language
  (`/plan додай легкий біг сьогодні`) with a confirm step; strength sessions per
  weekday (a saved Garmin workout or a free-text description)
* Adaptive plan: a weekly review proposes adjustments from the last week's data; a
  morning nudge eases today's session on low readiness; weather-aware day-swaps
* Garmin Calendar sync: the plan's upcoming workouts are pushed to Garmin Connect as
  structured workouts, kept in sync automatically (daily job + on edits); per-user
  on/off toggle, sync status + errors surfaced on `/plan`
* Plan-vs-actual matching (session- and step-level), personal records (all-time, per
  distance/type), race pack (T-7 narrated pacing/fueling, T-3 prep checklist, T-1
  evening brief) for a goal with a target date
* Health/injury radar: proactive recovery-anomaly and injury-risk DMs, quiet
  calibration period, cooldown-guarded
* Post-run check-ins (RPE + pain) feeding adaptation/digest/injury radar
* Personal baselines ("today vs your own norm"), correlation insights (`/insights`),
  compare-past-self (`/compare`), quarterly/yearly Wrapped (`/wrapped`)
* Weekly digest, evening sleep-debt nudge (with a concrete recommended bedtime once
  there's enough sleep-timing history), shoe-mileage tracker with wear/replace DMs
* Multisport weekly load budget, forward-looking load forecast, cycling sessions
  directly in the plan
* Offline backfill from a Garmin GDPR export (daily metrics, activities, pace/HR
  series from FIT files) — no API calls, no rate limits; your own data export (`/me/export`)
* Multi-user: per-user encrypted Garmin/Claude/Telegram credentials, self-registration
  with admin approval, remote MFA re-login through `/settings`
* Web dashboard (mobile-first): readiness, trends, upcoming plan, recent activities,
  monthly AI cost; a web chat with the coach (`/chat`)
* Admin: raw DB browser (`/ui`), cache inspection/invalidation (`/admin/cache`,
  hit-rate by kind + disk-cache sizes), job-run log, backup-freshness monitoring
* Aggressive data aggregation to minimize token usage/API cost; response caching
  (cross-process, DB-backed) avoids duplicate Claude calls entirely
* Database history (SQLite/Postgres) for trends, cost tracking, and day-level caching

## Project Structure

```text
app/                 shared core + web layer
  core/              config (pydantic-settings), logging, crypto, session auth, tz
  db/                async SQLAlchemy engine, ORM models, session, user/checkup/supplement queries
  garmin/            providers, low-level client (+ disk cache), service (aggregation),
                     repository/ (core/plans/state/stats), schemas, mfa (web MFA flow),
                     plan_sync (calendar sync), workout_export (plan → Garmin workout DTOs),
                     exercises/exercise_names, export_import (GDPR-export backfill),
                     matching (plan-vs-actual), token_info
  analysis/          client/cache/reports/plans (Claude calls, split by concern) + prompts
  routers/           auth, dashboard, health, reports, history, plan, chat, checkups,
                     settings, admin (/ui, /admin/cache, /admin/jobs)
  weather.py         Open-Meteo geocode + forecast
  charts.py          inline-SVG chart helpers
  race.py, gap.py, injury.py, health.py, baselines.py, multisport.py,
  loadforecast.py, subjective.py, compare.py, wrapped.py, correlations.py,
  goal.py, fueling.py, sleepnudge.py, records.py, stepmatch.py,
  checkup_reminders.py, gear.py     pure per-feature logic modules (zero/near-zero LLM)
  cli.py             admin CLI (create-user, backfills, push-plan, plan-adapt trigger, …)
  main.py            FastAPI app factory (create_app)
bot/                 Telegram front-end
  handlers.py        /report /ask /deep /activities /activity /records /costs /gear
                     /compare /wrapped /insights /risk /health /goal /race /plan
                     /checkups /sick /deploy (admin) /test_* …
  jobs.py            morning_job, plan_sync_job, plan_adapt_job, weather_plan_job,
                     weekly_digest_job, sleep_nudge_job
  main.py            product-bot entrypoint (python -m bot.main)
  admin_main.py      separate admin-bot entrypoint (/deploy + /test_* only)
alembic/             database migrations
deploy/              systemd units (garmin-bot, garmin-web, garmin-admin-bot, backups)
tests/               pytest suite
```

The aggregation in `app/garmin/service.py` is the most important part: raw Garmin
responses are collapsed into compact daily summaries before being sent to the LLM,
which dramatically reduces token usage and cost. Past immutable days are served from
the database instead of re-hitting Garmin; immutable assets (exercise sets, workout
details, activity series) are cached per-key on disk under `GARMIN_CACHE_DIR`.

Both front-ends share `app.garmin` and `app.analysis` — no duplicated logic.

## Installation

Create a virtual environment:

Python **3.12+** is required (the Garmin auth engine, `python-garminconnect`, needs
it; the Raspberry Pi runs 3.13). A venv created with an older interpreter has to be
recreated, not upgraded in place.

```bash
python3 -m venv venv
source venv/bin/activate
```

Install the project (editable, with dev extras — dependencies come from `pyproject.toml`):

```bash
./venv/bin/python -m pip install -e ".[dev]"
```

## Configuration

Create a `.env` file:

```env
# Required for auth: Fernet master key (encrypts stored creds + signs sessions)
APP_SECRET_KEY=...
# The single Telegram bot identity (global)
TELEGRAM_BOT_TOKEN=xxxxxxxx
# Optional separate admin/system bot identity: /deploy + /test_* commands
TELEGRAM_ADMIN_BOT_TOKEN=xxxxxxxx

# Seed-only: imported per-user by `create-user --seed-env`, then managed in /settings
GARMIN_EMAIL=your_email
GARMIN_PASSWORD=your_password
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_CHAT_ID=123456789
```

Credentials are **per user, stored encrypted in the database**; the `.env` Garmin/
Claude/Telegram values are only a one-time seed for the first account. Generate the
master key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Configuration is read from the environment and `.env` by
`app.core.config.Settings` (pydantic-settings) — the single typed source for all
variables below.

### Optional environment variables

The most commonly touched ones — see `app/core/config.py` and `CLAUDE.md` for the
full list (per-user timezones, login rate limiting, weather thresholds, health/injury
radar tuning, fueling/sleep-nudge/gear-wear thresholds, and more):

| Variable | Default | Purpose |
| --- | --- | --- |
| `GARMIN_PROVIDER` | `gconn` | Garmin auth engine: `gconn` (native `python-garminconnect`) or `garth` (rollback — needs `pip install -e ".[garth]"`) |
| `GARMIN_RPS` | `3.0` | process-wide Garmin request-rate cap (req/s); `0` disables |
| `DATABASE_URL` | `sqlite+aiosqlite:///./garmin.db` | DB; switch to `postgresql+asyncpg://...` by env alone |
| `LOG_FILE` | `bot.log` | Log file path |
| `LOG_LEVEL` | `INFO` | Root log level (`DEBUG` shows skip-reason logs) |
| `GARMIN_CACHE_DIR` | `garmin_cache` | Per-key disk cache dir for immutable Garmin assets |
| `CLAUDE_MAX_WORKERS` | `4` | Dedicated Claude thread-pool size |
| `INJURY_RADAR` | `True` | Master on/off for the injury-risk advisory |
| `HEALTH_ALERTS` | `True` | Master on/off for proactive recovery-anomaly alerts |
| `SLEEP_NUDGE` | `True` | Master on/off for the evening sleep-debt nudge |
| `GEAR_WEAR_KM` | `700` | Mileage threshold for the "replace your shoes" DM (`0` disables) |
| `DEPLOY_ENABLED` | `False` | Master on/off for the admin-only `/deploy` bot command |

`llm_cache`/`bot_state` are DB tables now — there's no `CLAUDE_CACHE_FILE`/`STATE_FILE`
env var; that state is shared automatically between the bot and web processes.

## Garmin Authentication

Each user connects Garmin at `/settings` (email + password, stored encrypted). If
Garmin asks for MFA, the page shows a code-entry form — the whole flow is remote,
no terminal needed. The resulting session token is stored per user in the DB and
reused automatically, so subsequent logins are silent. If a stored token expires
and MFA is needed again, the bot and the JSON endpoints reply with a friendly
"finish the login in /settings" instead of a generic error. A clear invalid-credentials
failure (not MFA, not a transient blip) is detected and stops retrying against Garmin
until the user updates their password in `/settings` — a DM + a dashboard/settings
banner explain why.

Auth runs on `python-garminconnect`'s native client (curl_cffi TLS impersonation). The
previous engine, `garth`, is deprecated upstream and kept only as a rollback:
`pip install -e ".[garth]"` plus `GARMIN_PROVIDER=garth`. The two session formats are not
interchangeable — switching engines costs each user one fresh login (their credentials are
already stored, so it happens by itself, MFA aside).

## Running

Use the virtual environment interpreter explicitly (the system Python won't find
the installed packages).

```bash
# Apply migrations (once, and after model changes):
./venv/bin/python -m alembic upgrade head

# Create the first admin (seeds creds from .env, claims existing data):
./venv/bin/python -m app.cli create-user --email me@example.com --admin --seed-env

# Start the web API:
./venv/bin/python -m uvicorn app.main:create_app --factory

# Start the Telegram bot:
./venv/bin/python -m bot.main

# Tests + lint:
./venv/bin/python -m pytest -q
./venv/bin/python -m ruff check app bot tests
```

Then log in at `/login`; manage credentials at `/settings`, users at `/admin/users`.

The web app also creates its tables on startup, so it runs zero-config before the
first `alembic upgrade head`.

### Admin CLI

`./venv/bin/python -m app.cli <command> --email …`:

* `create-user [--admin] [--seed-env] [--backfill-month]` — create a web-login user;
  `--seed-env` encrypts the `.env` creds into it and claims pre-existing data;
  `--backfill-month` fetches the last 30 days of Garmin data right away
* `import-garth-token [--path ~/.garth]` — seed a user's Garmin session from a garth token
  dir (rollback path only: the native engine can't read a garth blob)
* `import-export --path [--since] [--overwrite]` — backfill daily metrics +
  activities from a Garmin GDPR export folder (offline, no API)
* `import-fit-series --path [--since]` — fill runs' pace/HR series from the
  export's FIT files
* `backfill-series` / `backfill-auto-activities` / `backfill-records` /
  `backfill-strength-snapshots` — re-fetch series / auto-detected activities /
  personal records / strength progression for already-stored data (idempotent)
* `push-plan [--days 14] [--dry-run] [--date]` / `unpush-plan [--date]` — manually
  push/remove the active plan's workouts on the Garmin calendar
* `trigger-plan-adapt` — on-demand weekly-adaptation review
* `token-expiry` — per-user Garmin session-token deadline
* `list-workouts` — print the user's saved Garmin workout ids/names

### Web endpoints

* `GET/POST /login` · `GET /logout` · `GET/POST /register` — cookie-session auth +
  self-signup (new accounts await admin approval before they can log in)
* `GET /health` — liveness (public)
* `GET /dashboard` — mobile-first overview (readiness, trends, upcoming plan, recent
  activities, monthly cost); the post-login landing page for non-admins
* `GET /status` — the logged-in user's Garmin auth, DB stats, last morning report, cost
* `GET /report.json` — daily report (Sonnet), login required
* `GET /deep?q=...` — deep analysis (Opus), login required
* `GET /history?days=N` — HRV/sleep/stress/Body Battery trend from the DB, login required
* `GET/POST /plan` — training-plan setup form / generated plan view, login required;
  `POST /plan/archive`, `POST /plan/adjust-level`, `POST /plan/season`,
  `GET /plan/archive` (list archived), `GET /plan/{id}` (read-only view of a past plan)
* `GET/POST /chat` + `POST /chat/confirm` — web chat with the coach over the same
  agent `/ask` uses, plus plan-edit proposals
* `GET/POST /checkups`, `/checkups/supplements` — health checkups + supplement tracking
* `GET /me/export` — streamed ZIP export of everything your account owns
* `GET /settings` — manage your own Garmin/Claude/Telegram credentials + password
* `GET /me` — browse your own metrics / activities / reports (per-user, with charts)
* `GET /admin/users` — list/create/approve/activate/delete users (admin only);
  `POST /admin/users/{id}/impersonate` starts a **read-only** borrowed session so an
  admin can see a user's own pages, `POST /impersonate/stop` hands the session back
* `GET /admin/cache` — llm_cache + Garmin disk-cache stats/hit-rate + purge actions (admin only)
* `GET /admin/jobs` — recent scheduled-job run log (admin only)
* `GET /ui` — raw DB browser across all users (admin only)

Auth is a signed cookie session established at `/login`; there are no API tokens.
Credentials are per user and encrypted at rest.

## Garmin Data Sources

Current implementation uses the following Garmin Connect endpoints (non-exhaustive —
see `app/garmin/client.py` for the full set, including gear, calendar/workout push,
and activity-series fetches):

### Sleep

```text
/wellness-service/wellness/dailySleepData/{userName}
```

### HRV

```text
/hrv-service/hrv/{date}
```

### Stress

```text
/wellness-service/wellness/dailyStress/{date}
```

### Body Battery

```text
/wellness-service/wellness/bodyBattery/reports/daily
```

### Activities

```text
/activitylist-service/.../activities
```

### Strength Training Sets

```text
/activity-service/activity/{id}/exerciseSets
```

### Garmin Calendar (plan push)

```text
/calendar-service/...
```

### Workout Details

```text
/workout-service/workout/{id}
```

## Important Notes

### Garmin Access Is Unofficial

The project relies on `python-garminconnect`, which uses unofficial Garmin Connect APIs.

Garmin does not support this approach, and endpoints may change without notice. A
process-wide rate limiter + 429 backoff (`GARMIN_RPS`/`GARMIN_RETRIES`) exists
specifically to reduce the risk of aggressive request patterns triggering a ban.

### Resting Heart Rate Recovery Metrics

The dedicated resting-heart-rate endpoint is unavailable because Garmin returns HTTP 403 responses for it.

Recovery analysis therefore relies primarily on:

* HRV average
* HRV status
* Sleep quality
* Stress
* Body Battery

### Plan Sync

Training plans generated in this app are synchronized into Garmin Calendar as
structured workouts (rolling window), kept in sync automatically.

### Synchronization Awareness

The payload includes synchronization flags such as:

```text
synced_today
has_data
```

This allows Claude to distinguish between:

* Missing Garmin synchronization
* Poor recovery metrics

### Pace Conversion

Garmin workout pace targets are stored in meters per second.

The aggregation layer converts them into minutes per kilometer before analysis (and
into grade-adjusted pace where elevation data is available).

### Cost

The aggregation layer dramatically reduces token usage.

Typical Sonnet report cost is approximately $0.02–0.03 per report; `/deep` and plan
generation (Opus) cost more per call but run far less often.

Identical requests are served from a cross-process dedup cache, so repeated reports on
the same data cost nothing (see Caching and Persistence). Every Claude call is logged
(tokens, cost, ok/error) to the database for per-user cost tracking (`/costs`, `/me`).

Avoid sending raw Garmin data to the LLM.

### Model State

Claude is stateless — long-term comparisons (personal baselines, "today vs normal",
weekly digests, correlation insights, Wrapped) are computed locally in pure Python from
stored history and included in the prompt payload, not remembered by the model.

## Caching and Persistence

Three cache layers, all shared between the bot and web processes via the database (or
a disk directory, for immutable Garmin assets) — no per-process JSON files.

### Claude dedup cache (`llm_cache` DB table)

To avoid paying for identical Claude requests:

* The cache key is a hash of the meaningful payload (daily metrics, recent activities,
  planned runs), the current date, the question, the model, and (for reports) the
  previous-day report fed as context. The volatile `generated` timestamp is excluded,
  so fresh Garmin data invalidates the cache automatically.
* Each report kind/model pair is cached separately (`report`/`deep`/`morning`/`ask`/
  `activity`/plan-generation/adaptation/digest/race/… × the model used).
* `/ask` keys on the recent reports plus the recent `/ask` thread and the question instead.
* One-week TTL, purged lazily on write. A hit logs `CLAUDE CACHE HIT`.
* Inspect/purge from `/admin/cache` (hit-rate by kind, row counts, size) — no SSH needed.

### Garmin disk cache (`GARMIN_CACHE_DIR`, default `garmin_cache/`)

Immutable assets keyed on stable Garmin IDs, one JSON file per key, to cut request volume:

* `exercise:v3:<id>` — a completed activity's exercise sets (365-day TTL; immutable).
* `workout:v2:<id>` — planned-workout details: name, coach description, steps (7-day TTL; plans can be edited).
* `series:v3:<id>` / `splits:v1:<id>` — a run's per-point pace/HR series and lap splits;
  the series also carries elevation, running dynamics (cadence / ground-contact time /
  vertical oscillation, NF-25) and coordinates (NF-33) when the watch reports them
  (365-day TTL; immutable). Entries written under the older `series:v2` key stay
  readable — a channel a series doesn't carry reads as absent, never as zero.
* `gear:v2:<id>` / `gear_link:v1:<id>` — gear roster/mileage and activity→gear links.

A hit logs `GARMIN CACHE <key>`. Raw Garmin codes are stored; exercise names are mapped
to Ukrainian at read time, so labels can change without invalidating the cache.

### Database (`garmin.db` by default)

Day-level caching, history, and cost tracking live in the database:

* `DailyMetric` — one row per day; past days are served from here instead of Garmin (today is always refetched). Doubles as the trend source for `/history`. An `extra` JSON column also stores the scalar metrics we fetch but don't model as columns (resting HR, SpO2, respiration, skin-temp deviation, HRV detail, Training Readiness + ACWR load, daily steps/intensity minutes/floors, VO2max, race-time predictions and endurance score, sleep start/end timing). Plan generation calibrates targets to the latest race predictions / VO2max.
* `ActivityRecord` — one row per activity (idempotent on `activity_id`); runs also store a downsampled pace/HR `series` rendered as charts on the activity detail page, plus an optional `analysis` (Claude's `/activity` writeup) and plan-vs-actual step-match data.
* `ReportLog` — one row per Claude call (tokens, cost, ok/error, the asked `question` and the delivered `report_text`).
* `BotState` — key/value, including per-user guard state for every once-a-day/once-per-item nudge.
* `TrainingPlan` + `PlannedWorkout` — a generated training program (one active per user) and its dated sessions; created from `/plan`, adjusted via the bot/web chat.
* `PersonalRecord`, `HealthCheckup`, `Supplement` — user-scoped supporting data.

Backend is set by `DATABASE_URL`: SQLite (zero-config) by default, Postgres by env
var alone. Schema is managed with Alembic (`alembic upgrade head`).

## Time Zones

Every user carries their own IANA `timezone` (default `Europe/Warsaw`) — morning
report windows, once-a-day guards, and evening nudges all run in that user's local
time, DST included, rather than one shared process timezone.

## Security

Web access requires a login (signed cookie session); credentials are per user and
encrypted at rest with the `APP_SECRET_KEY` Fernet key. The bot maps an incoming
chat to a user by its stored `telegram_chat_id` and ignores unknown chats.
Garmin credentials remain on the host machine/database and are never sent to Claude.

An admin can borrow a user's session from `/admin/users` to answer "what do you actually
see?" — bounded to reading: the borrowed session refuses every non-GET request, makes no
Garmin or Claude call (so it spends none of the user's budget), carries no admin rights,
and cannot be started against another admin. Every page shows whose session it is, and
start/stop are logged at WARNING.
