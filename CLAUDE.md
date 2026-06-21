# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal **Garmin → Claude** analyzer with a shared core reused by two front-ends:

- a **Telegram bot** (`bot/`) — commands + a scheduled morning report;
- a **FastAPI web layer** (`app/`) — JSON endpoints for reports, status, and history.

Both call the same services (`app.garmin`, `app.analysis`) over an async SQLAlchemy
database that stores history, caches immutable days, and tracks cost.

## Running

Always use the venv interpreter. NOTE: the venv was created at an old path, so the
`./venv/bin/pip` / `./venv/bin/alembic` wrapper shebangs are broken — invoke tools
through the working python binary with `-m`:

```bash
# Install (editable, with dev extras):
./venv/bin/python -m pip install -e ".[dev]"

# Create / migrate the database (run once, and after model changes):
./venv/bin/python -m alembic upgrade head

# Start the web API (factory + lifespan):
./venv/bin/python -m uvicorn app.main:create_app --factory

# Start the Telegram bot:
./venv/bin/python -m bot.main

# Tests + lint:
./venv/bin/python -m pytest -q
./venv/bin/python -m ruff check app bot tests
```

The web app also runs zero-config: `init_db()` in the lifespan creates tables on
startup, so `uvicorn` works even before `alembic upgrade head`. Alembic remains the
source of truth for schema changes.

## Environment

`.env` (read by `app.core.config.Settings` via pydantic-settings):

```
GARMIN_EMAIL=
GARMIN_PASSWORD=
ANTHROPIC_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Optional, with defaults:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GARMIN_PROVIDER` | `garth` | Garmin backend: `garth` (working) or `gconn` (untested) |
| `GARTH_TOKEN_DIR` | `~/.garth` | Garmin token storage |
| `DATABASE_URL` | `sqlite+aiosqlite:///./garmin.db` | DB; switch to `postgresql+asyncpg://...` by env alone |
| `WEB_TOKEN` | `` (empty) | Shared secret for data/cost endpoints; empty disables auth |
| `LOG_FILE` | `bot.log` | Log file path |
| `LOG_LEVEL` | `INFO` | Root level (`DEBUG` shows skip-reason logs) |
| `CLAUDE_CACHE_FILE` | `claude_cache.json` | Claude dedup cache |
| `GARMIN_CACHE_FILE` | `garmin_cache.json` | Disk cache for immutable Garmin assets |

`STATE_FILE` is gone — the morning-sent date now lives in the `bot_state` table.

## Structure

```
app/
  main.py              create_app() factory; lifespan = DB init/dispose; router registration
  core/
    config.py          pydantic-settings Settings — the single source for all env vars
    logging.py         logging config (was logging_setup.py)
    security.py        verify_token dependency (WEB_TOKEN; Bearer or X-Token; empty = off)
  db/
    base.py            async engine + sessionmaker + declarative Base; init_db/dispose_db
    session.py         get_session() request dependency
    models.py          ORM: DailyMetric, ActivityRecord, ReportLog, BotState
  garmin/
    providers.py       _GarthProvider (verbatim) / _GConnProvider (untested) via GARMIN_PROVIDER
    client.py          low-level connectapi fetches + disk cache for immutable assets
    service.py         aggregation; build_payload (sync) + build_payload_cached (async, DB-backed)
    repository.py      idempotent upserts, history reads, ReportLog, BotState (ORM↔schema mapping)
    schemas.py         Pydantic Payload / DailySummary / Activity / PlannedRun
    exercise_names.py  Garmin exercise NAME codes → readable Ukrainian
  analysis/
    service.py         analyze()/AnalystError; dedup cache; cost logging; ReportLog via run_analysis
    prompts.py         SYSTEM prompt (verbatim)
  routers/
    health.py          GET /health, GET /status
    reports.py         GET /report.json (Sonnet), GET /deep (Opus) — token-gated
    history.py         GET /history?days=N — trends from DB, token-gated
  dependencies.py      shared deps (get_session, verify_token)
bot/
  main.py              builds the Application, registers handlers + job, run_polling
  handlers.py          /report, /deep, /test_on, /test_off, owner _guard, error handler
  jobs.py              morning_job (Europe/Warsaw window; once-a-day guard in BotState)
alembic/               migrations (async env.py wired to Base.metadata + DATABASE_URL)
tests/                 pytest: garmin service (mocked provider), routers, repository
```

## Architecture and data flow

```
Telegram command / HTTP request
  → service.build_payload_cached(session, days, activity_limit)   [async]
      → provider.login() (garth token at ~/.garth)
      → past immutable days served from DB (repository.read_daily_metrics)
      → today + missing days fetched via Garmin (run_in_threadpool); activities, planned
      → persist_payload(): upsert daily + activities (idempotent)
      → typed Payload (synced_today, last_data_date, daily[], recent_activities[], planned_runs[])
  → analysis.run_analysis(session, payload, ...)
      → dedup cache check (hash of payload+date+question+model) — early return on hit
      → Sonnet (/report, morning) or Opus (/deep); AnalystError → user-visible message
      → ReportLog row written (tokens, cost, ok/error)
  → reply / JSON response
```

The aggregation in `app/garmin/service.py` is the cost-control layer — raw Garmin
responses are collapsed to ~12 fields/day and never sent to the LLM.

## Web endpoints

- `GET /health` — liveness (unauthenticated).
- `GET /status` — Garmin auth, DB stats, last morning report, total cost (unauthenticated; metadata only).
- `GET /report.json` — daily report (Sonnet). Token-gated.
- `GET /deep?q=...` — deep analysis (Opus). Token-gated.
- `GET /history?days=N` — HRV/sleep/stress/body-battery trend from the DB. Token-gated.

Auth: send `WEB_TOKEN` as `Authorization: Bearer <token>` or `X-Token: <token>`.
If `WEB_TOKEN` is empty, the gate is disabled.

## Database

- **Stack**: SQLAlchemy 2.0 async + Alembic. SQLite (`aiosqlite`) by default for
  zero-config on a Raspberry Pi; switch to Postgres (`asyncpg`) by setting
  `DATABASE_URL` only — no code changes.
- **Models**: `DailyMetric` (unique `date`), `ActivityRecord` (unique `activity_id`,
  `exercises` JSON), `ReportLog` (cost/metrics), `BotState` (key/value).
- **DB as cache**: past days already stored are served from the DB instead of
  re-hitting Garmin; today is always refetched (still syncing). `build_payload_cached`
  persists what it fetches, so history accumulates.
- **Migrations**: `./venv/bin/python -m alembic upgrade head`. To add a migration after
  changing models: `./venv/bin/python -m alembic revision --autogenerate -m "msg"`.

## Key design decisions

**Garmin provider**: `garth` is the working path (unofficial endpoints, token at
`~/.garth`, first run needs interactive MFA). A `gconn` provider over `garminconnect`
exists behind `GARMIN_PROVIDER=gconn` but is **untested against the live API** — do
not rely on it. Endpoint URLs and the m/s→min/km pace conversion are unchanged.

**HRV is the primary recovery signal** — Garmin returns 403 for resting HR via garth.
`hrv_status = BALANCED` means recovered; a drop is the main stress indicator.

**Sync awareness**: `synced_today` / `has_data` / `last_data_date` distinguish "watch
hasn't synced" from "bad recovery." The morning job runs ~10s after startup, then every
20 min; the Europe/Warsaw window (07–12) and once-a-day guard live inside `morning_job`,
which logs its decision. The once-a-day guard persists in `bot_state`.

**Models**: `/report` + morning use `claude-sonnet-4-6`; `/deep` uses `claude-opus-4-8`.
Every call is logged to `ReportLog` (tokens, cost, ok/error).

**Exercise names**: `fetch_exercise_summary` reads Garmin's specific `name` code, maps it
to Ukrainian via `app/garmin/exercise_names.py` at return time (cache stays language-
neutral). Unknown codes are logged once (`EXERCISE unmapped: <CODE>`). Warm-up jog filtered.

## Caching layers

- **Claude dedup** (`claude_cache.json`): `analyze()` keys on a hash of the meaningful
  payload (`daily`, `recent_activities`, `planned_runs`) + date + question + model. The
  volatile `generated` timestamp is deliberately excluded — otherwise the key changes
  every minute and never hits (the main gotcha if you touch the key logic). 1-week TTL.
  Hit logs `CLAUDE CACHE HIT`.
- **Garmin disk cache** (`garmin_cache.json`): immutable ID-keyed assets only —
  `exercise:v2:<id>` (365d) and `workout:<id>` (7d). Day-level caching moved to the DB.
- **DB day-level cache** (`DailyMetric`): past days served from the DB; today refetched.

## Logging

`app.core.logging.setup()` runs at process start (web factory and `bot.main`). Logs go to
`bot.log` (rotating, 5 × 1 MB) and stdout. Root level is `LOG_LEVEL`; noisy libraries
(httpx, telegram, apscheduler, uvicorn.access) are pinned WARNING. Run with
`LOG_LEVEL=DEBUG` to see skip-reason lines (e.g. `MORNING skip: outside window`).

## TODO

- Validate the `gconn` provider against the live Garmin API.
- Deploy to Raspberry Pi 4 (systemd units for `bot.main` and `uvicorn`).
- Optional: dashboard/history visualization, remote MFA re-login flow.
