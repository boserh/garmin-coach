# Garmin → Claude

[![CI](https://github.com/boserh/garmin-coach/actions/workflows/ci.yml/badge.svg)](https://github.com/boserh/garmin-coach/actions/workflows/ci.yml)

A personal Garmin Connect analyzer with a **shared core** reused by two front-ends:
a **Telegram bot** and a **FastAPI web layer**. It pulls health and training data,
aggregates it into compact daily summaries, sends those to Claude for analysis, and
persists history/cost in a database.

The project is designed for personal use. Development and testing are done on macOS, with Raspberry Pi 4 (4 GB RAM) as the target deployment platform.

```text
Telegram bot / Web API
    ↓
app.garmin.service  (fetch + aggregation, DB-backed cache)
    ↓
Claude API (app.analysis)
    ↓
Telegram reply / JSON response   +   history & cost in the DB
```

See `CLAUDE.md` for the full module map and design notes.

## Features

* Daily recovery analysis
* Sleep, HRV, stress, Body Battery, activities, and workout analysis
* Runna workout plan integration through Garmin Calendar
* Morning automated reports — optionally weather-aware: set a location in `/settings`
  and the morning report factors today's heat/rain/wind into the run advice (Open-Meteo,
  no API key)
* On-demand reports via Telegram commands
* Follow-up questions (`/ask`) answered against your recent reports, with the last few
  minutes' `/ask` thread carried as conversation context so you can refine a question
* Deep analysis mode using a larger Claude model
* Per-activity analysis (`/activities` to list, `/activity <id>` to analyze pace, HR and
  effort); the writeup is saved and shown on the activity's web detail page
* Training plans: pick a goal on the web (`/plan`) and get a generated dated program;
  adjust it in plain language in the bot (`/plan додай легкий біг сьогодні`) with a confirm step
* Aggressive data aggregation to minimize token usage and API cost
* Response caching to avoid duplicate Claude API calls
* Web API (FastAPI) for reports, status, and history trends
* Database history (SQLite/Postgres) for trends, cost tracking, and day-level caching
* Persistent state so caches and morning-report status survive restarts

## Project Structure

```text
app/                 shared core + web layer
  core/              config (pydantic-settings), logging, web auth
  db/                async SQLAlchemy engine, ORM models, session
  garmin/            providers, low-level client, service (aggregation), repository, schemas
  analysis/          Claude analysis (service) + SYSTEM prompt
  routers/           /health, /status, /report.json, /deep, /history
  main.py            FastAPI app factory (create_app)
bot/                 Telegram front-end
  handlers.py        /report, /ask, /deep, /activities, /activity, /plan, /test_on, /test_off
  jobs.py            morning_job
  main.py            entrypoint (python -m bot.main)
alembic/             database migrations
tests/               pytest suite
```

The aggregation in `app/garmin/service.py` is the most important part: raw Garmin
responses are collapsed into compact daily summaries before being sent to the LLM,
which dramatically reduces token usage and cost. Past immutable days are served from
the database instead of re-hitting Garmin; immutable assets (exercise sets, workout
details) are cached on disk in `garmin_cache.json`.

Both front-ends share `app.garmin` and `app.analysis` — no duplicated logic.

## Installation

Create a virtual environment:

```bash
python -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Required packages include:

```text
anthropic
garth
python-telegram-bot[job-queue]
python-dotenv
```

## Configuration

Create a `.env` file:

```env
# Required for auth: Fernet master key (encrypts stored creds + signs sessions)
APP_SECRET_KEY=...
# The single Telegram bot identity (global)
TELEGRAM_BOT_TOKEN=xxxxxxxx

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

The application reads configuration from environment variables.

Example:

```python
os.environ["ANTHROPIC_API_KEY"]
```

### Optional environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `GARMIN_PROVIDER` | `garth` | Garmin backend: `garth` (working) or `gconn` (untested) |
| `GARTH_TOKEN_DIR` | `~/.garth` | Garmin token storage |
| `DATABASE_URL` | `sqlite+aiosqlite:///./garmin.db` | DB; switch to `postgresql+asyncpg://...` by env alone |
| `WEB_TOKEN` | `` (empty) | Shared secret for data/cost endpoints; empty disables auth |
| `LOG_FILE` | `bot.log` | Log file path |
| `LOG_LEVEL` | `INFO` | Root log level (`DEBUG` shows skip-reason logs) |
| `CLAUDE_CACHE_FILE` | `claude_cache.json` | Claude response cache |
| `GARMIN_CACHE_FILE` | `garmin_cache.json` | Disk cache for immutable Garmin assets |

The morning-report status is no longer a file — it lives in the `bot_state` table.

## Garmin Authentication

On the first run, Garmin authentication may require MFA verification.

Tokens are stored by garth and reused automatically.

Subsequent runs typically do not require manual login.

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

# Tests:
./venv/bin/python -m pytest -q
```

Then log in at `/login`; manage credentials at `/settings`, users at `/admin/users`.

The web app also creates its tables on startup, so it runs zero-config before the
first `alembic upgrade head`.

### Web endpoints

* `GET /login` · `GET /logout` · `GET /register` — cookie-session auth + self-signup
  (new accounts await admin approval before they can log in)
* `GET /health` — liveness (public)
* `GET /status` — the logged-in user's Garmin auth, DB stats, last morning report, cost
* `GET /report.json` — daily report (Sonnet), login required
* `GET /deep?q=...` — deep analysis (Opus), login required
* `GET /history?days=N` — HRV/sleep/stress trend from the DB, login required
* `GET/POST /plan` — training-plan setup form / generated plan view, login required
* `GET /settings` — manage your own Garmin/Claude/Telegram credentials + password
* `GET /me` — browse your own metrics / activities / reports (per-user, with charts)
* `GET /admin/users` — list/create/approve/activate/delete users (admin only)
* `GET /ui` — raw DB browser across all users (admin only)

Auth is a signed cookie session established at `/login`; there are no API tokens.
Credentials are per user and encrypted at rest.

## Garmin Data Sources

Current implementation uses the following Garmin Connect endpoints:

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

### Garmin Calendar / Runna

```text
/calendar-service/...
```

### Workout Details

```text
/workout-service/workout/{id}
```

## Important Notes

### Garmin Access Is Unofficial

The project relies on `garth`, which uses unofficial Garmin Connect APIs.

Garmin does not support this approach, and endpoints may change without notice.

### Resting Heart Rate Recovery Metrics

Resting heart rate recovery data is currently unavailable through garth because Garmin returns HTTP 403 responses.

Recovery analysis therefore relies primarily on:

* HRV average
* HRV status
* Sleep quality
* Stress
* Body Battery

### Runna Integration

Runna training plans are synchronized into Garmin Calendar.

The bot retrieves planned workouts from Garmin rather than from Google Calendar.

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

The aggregation layer converts them into minutes per kilometer before analysis.

### Cost

The aggregation layer dramatically reduces token usage.

Typical Sonnet report cost is approximately $0.02–0.03 per report.

Identical requests are served from the local cache, so repeated reports on the same data cost nothing (see Caching and Persistence).

Avoid sending raw Garmin data to the LLM.

### Model State

Claude is stateless.

If long-term comparisons are needed, baselines and historical data should be stored locally and included in the prompt payload.

Potential future improvements:

* Personal baseline tracking
* "Today vs normal" comparisons
* Weekly summaries
* Post-workout analysis

## Caching and Persistence

Three small JSON files persist state across restarts. All use atomic writes, prune expired entries on save, and tolerate an empty/corrupt file (they just start fresh).

### Claude dedup cache (`claude_cache.json`)

To avoid paying for identical Claude requests:

* The cache key is a hash of the meaningful payload (daily metrics, recent activities, planned runs), the current date, the question, the model, and the previous-day report fed as context. The volatile `generated` timestamp is excluded, so fresh Garmin data invalidates the cache automatically.
* `/report` (Sonnet) and `/deep` (Opus) are cached separately, since the model is part of the key.
* `/ask` keys on the recent reports plus the recent `/ask` thread and the question instead.
* One-week TTL. A hit logs `CLAUDE CACHE HIT`.

### Garmin disk cache (`garmin_cache.json`)

Immutable assets keyed on stable Garmin IDs, to cut request volume:

* `exercise:v2:<id>` — a completed activity's exercise sets (365-day TTL; immutable).
* `workout:v2:<id>` — planned-workout details: name, coach description, steps (7-day TTL; plans can be edited).
* `series:v1:<id>` — a run's per-point pace/HR series from `/details`, downsampled (365-day TTL; immutable).

A hit logs `GARMIN CACHE <key>`. Raw Garmin codes are stored; exercise names are mapped to Ukrainian at read time, so labels can change without invalidating the cache.

### Database (`garmin.db` by default)

Day-level caching, history, and cost tracking moved into the database:

* `DailyMetric` — one row per day; past days are served from here instead of Garmin (today is always refetched). Doubles as the trend source for `/history`. An `extra` JSON column also stores the scalar metrics we fetch but don't model as columns (resting HR, SpO2, respiration, skin-temp deviation, HRV detail, Training Readiness + ACWR load, daily steps/intensity minutes/floors, VO2max, race-time predictions and endurance score). Plan generation calibrates targets to the latest race predictions / VO2max.
* `ActivityRecord` — one row per activity (idempotent on `activity_id`); runs also store a downsampled pace/HR `series` rendered as charts on the activity detail page, plus an optional `analysis` (Claude's `/activity` writeup).
* `ReportLog` — one row per Claude call (tokens, cost, ok/error, the asked `question` and the delivered `report_text`).
* `BotState` — key/value, including the morning-report-sent date (replaces `state.json`).
* `TrainingPlan` + `PlannedWorkout` — a generated training program (one active per user) and its dated sessions; created from `/plan`, adjusted via the bot.

Backend is set by `DATABASE_URL`: SQLite (zero-config) by default, Postgres by env
var alone. Schema is managed with Alembic (`alembic upgrade head`).

Cache paths can be overridden via `CLAUDE_CACHE_FILE` and `GARMIN_CACHE_FILE`.

## Time Zones

Telegram JobQueue uses UTC by default.

To avoid scheduling offsets, configure:

```python
Defaults(
    tzinfo=ZoneInfo("Europe/Warsaw")
)
```

Otherwise scheduled reports may be shifted by two hours depending on daylight saving time.

## Security

The bot responds only to the configured Telegram chat ID.

Garmin credentials remain on the host machine and are never sent to Claude.
