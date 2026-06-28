# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal **Garmin → Claude** analyzer with a shared core reused by two front-ends:

- a **Telegram bot** (`bot/`) — commands + a scheduled morning report;
- a **FastAPI web layer** (`app/`) — JSON endpoints for reports, status, and history.

Both call the same services (`app.garmin`, `app.analysis`) over an async SQLAlchemy
database that stores history, caches immutable days, and tracks cost.

## Running

Always use the venv interpreter — the system Python is aliased and won't find the
installed packages:

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

### First-run bootstrap (multi-user)

Credentials are now **per user, stored encrypted in the DB** — `.env` Garmin/Claude
values are only a seed source. After installing:

```bash
# 1. Generate a master key and put it in .env as APP_SECRET_KEY:
./venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. Migrate (adds users + user_id columns):
./venv/bin/python -m alembic upgrade head

# 3. Create the first admin, seeding its creds from .env and claiming existing data:
./venv/bin/python -m app.cli create-user --email me@example.com --admin --seed-env
```

Then log in at `/login`, manage credentials at `/settings`, add users at `/admin/users`.

## Environment

`.env` (read by `app.core.config.Settings` via pydantic-settings):

```
APP_SECRET_KEY=        # Fernet key — encrypts creds + signs sessions (required for auth)
TELEGRAM_BOT_TOKEN=    # the (single) bot identity — global
# Seed-only (per-user after bootstrap; used by `create-user --seed-env`):
GARMIN_EMAIL=
GARMIN_PASSWORD=
ANTHROPIC_API_KEY=
TELEGRAM_CHAT_ID=
```

Optional, with defaults:

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_SECRET_KEY` | `` (empty) | Fernet master key: encrypts stored creds + signs cookie sessions |
| `GARMIN_PROVIDER` | `garth` | Garmin backend: `garth` (working) or `gconn` (untested) |
| `GARTH_TOKEN_DIR` | `~/.garth` | Legacy global garth token dir (per-user tokens live in the DB) |
| `DATABASE_URL` | `sqlite+aiosqlite:///./garmin.db` | DB; switch to `postgresql+asyncpg://...` by env alone |
| `WEB_TOKEN` | `` (empty) | Legacy shared secret; superseded by login (kept for compatibility) |
| `LOG_FILE` | `bot.log` | Log file path |
| `LOG_LEVEL` | `INFO` | Root level (`DEBUG` shows skip-reason logs) |
| `CLAUDE_CACHE_FILE` | `claude_cache.json` | Claude dedup cache |
| `GARMIN_CACHE_FILE` | `garmin_cache.json` | Disk cache for immutable Garmin assets |

`STATE_FILE` is gone — the morning-sent date lives in the `bot_state` table, per user.

## Authentication & multi-user

- **Users**: `users` table (login email + bcrypt hash, `is_admin`, encrypted
  Garmin/Claude creds + garth token, plaintext indexed `telegram_chat_id`). Web login
  is a signed cookie session (`SessionMiddleware`, signed by `APP_SECRET_KEY`).
- **Secrets**: `app.core.crypto` — Fernet encrypt/decrypt for creds, bcrypt for
  passwords. `app.garmin.credentials.load_credentials` decrypts a user into a runtime
  `UserCredentials`.
- **Per-user runtime**: `app.garmin.runtime.user_runtime(session, user)` binds that
  user's Garmin provider (a `garth.Client` resumed from the stored token, else
  email+password login — no MFA — saving a fresh token) via a ContextVar, and yields
  decrypted creds (so `run_analysis(..., api_key=creds.anthropic_key)` uses their key).
  All data reads/writes are scoped by `user_id`.
- **Registration**: `/register` is public — a self-signup creates an unapproved,
  non-admin user (`is_approved=False`) that **cannot log in** until an admin approves
  it at `/admin/users` (approve / delete buttons). Admin- and CLI-created users are
  approved on creation. Login lands admins on `/ui`, others on `/settings`.
- **Routes**: `/login`, `/logout`, `/register`, `/settings` (own creds +
  `POST /settings/password` to change password, verifying the current one),
  `/admin/users` (admin: list/create/approve/activate-deactivate/delete), `/me`
  (each user browses **their own** daily_metrics/activities/report_logs, with the
  HRV/sleep charts), `/ui` (raw DB browser — **admin only**). `/health` stays public;
  `/status`, `/report.json`, `/deep`, `/history` require login and act on the current
  user. The `/me` and `/ui` browsers share `index/table/detail.html` via a `base` var
  (`app.routers.me` is user-scoped; `app.routers.admin` spans all rows).
- **Active flag**: `is_active` (default True) is a separate admin off-switch from
  approval — a deactivated user keeps its data but can neither log in (403 "Акаунт
  деактивовано") nor receive bot reports (`_resolve_user` and `morning_job` require
  active + approved). Admins can't deactivate themselves.
- **Bot**: one global `TELEGRAM_BOT_TOKEN`; an incoming chat is mapped to a user by
  `telegram_chat_id` (`_resolve_user`). `morning_job` loops over every user with a
  chat id + Garmin creds, each guarded once-a-day via per-user `bot_state`.
- **CLI**: `python -m app.cli create-user [--admin] [--seed-env]` — `--seed-env`
  encrypts `.env` creds into the user and claims pre-existing (unowned) data rows.
  `import-garth-token --email` seeds a user's garth session from `~/.garth`;
  `backfill-series --email` fetches the pace/HR series for already-stored runs that
  predate the feature (fills nulls only, idempotent). `import-export --email --path
  [--since YYYY-MM-DD] [--overwrite]` backfills `daily_metrics` (+`extra`) **and**
  `activities` from a Garmin GDPR export folder offline (no API → no 429).
  `app.garmin.export_import` merges the per-date JSON (sleep, UDS daily summary,
  healthStatus for HRV, VO2max, race predictions, endurance, readiness) — the export uses
  different keys/units than the live API (sleep score = `overallScore`; HRV in
  `healthStatusData.metrics`; all-day stress avg+max in `allDayStress.aggregatorList`;
  activity distance in cm). Existing days are **merge-filled** (only NULL columns + missing
  `extra` keys; `--overwrite` is still null-safe — never writes a null over a value);
  activities insert only ids not already stored (summary only — no pace/HR series).
  `/history` caps at 365 days, so `--since` ~1y is plenty.

## Structure

```
app/
  main.py              create_app() factory; SessionMiddleware; RequiresLogin→/login; routers
  cli.py               admin CLI: create-user [--admin] [--seed-env]
  core/
    config.py          pydantic-settings Settings — the single source for all env vars
    logging.py         logging config (was logging_setup.py)
    security.py        verify_token dependency (legacy WEB_TOKEN; superseded by auth.py)
    crypto.py          Fernet encrypt/decrypt for creds + bcrypt password hashing
    auth.py            current_user / require_admin deps; session login/logout helpers
  db/
    base.py            async engine + sessionmaker + declarative Base; init_db/dispose_db
    session.py         get_session() request dependency
    models.py          ORM: User, DailyMetric, ActivityRecord, ReportLog, BotState (user-scoped)
    users.py           user queries: get_by_email / get_by_chat_id / create_user
  garmin/
    providers.py       legacy global + _UserGarthProvider + provider ContextVar
    credentials.py     load_credentials(user) → decrypted UserCredentials
    runtime.py         user_runtime(session, user): bind provider, persist fresh garth token
    client.py          low-level connectapi fetches + disk cache for immutable assets
    service.py         aggregation; build_payload (sync) + build_payload_cached (async, per-user)
    repository.py      user-scoped upserts/reads, ReportLog, per-user BotState
    schemas.py         Pydantic Payload / DailySummary / Activity / PlannedRun
    exercise_names.py  Garmin exercise NAME codes → readable Ukrainian
  analysis/
    service.py         analyze/ask/run_analysis/run_ask; per-key Anthropic client; dedup cache
    prompts.py         SYSTEM + SYSTEM_ASK prompts
  routers/
    auth.py            GET/POST /login, GET /logout
    settings.py        /settings (own creds), /admin/users (admin)
    health.py          GET /health (public), GET /status (login, per-user)
    reports.py         GET /report.json (Sonnet), GET /deep (Opus) — login, per-user
    history.py         GET /history?days=N — trends from DB, login, per-user
    plan.py            GET/POST /plan — training-plan setup form + view, login, per-user
    admin.py           /ui DB browser — admin only
  dependencies.py      shared deps (get_session, verify_token)
bot/
  main.py              builds the Application, registers handlers + job, run_polling
  handlers.py          /report, /ask, /deep, /activities, /activity, /plan (+edit), /test_*; _resolve_user, error handler
  jobs.py              morning_job loops users (Europe/Warsaw window; per-user once-a-day guard)
alembic/               migrations (async env.py wired to Base.metadata + DATABASE_URL)
tests/                 pytest: crypto, garmin service, routers (login), repository, user runtime
```

## Architecture and data flow

```
Telegram command (chat_id→user) / HTTP request (session→user)
  → async with user_runtime(session, user) as creds:   # binds user's garth provider
      → service.build_payload_cached(session, user.id, days, activity_limit)   [async]
          → provider.login() (per-user garth.Client; token resumed/persisted in DB)
          → past immutable days served from DB (repository.read_daily_metrics, user-scoped)
          → today + missing days fetched via Garmin (run_in_threadpool); activities, planned
          → persist_payload(): upsert daily + activities (idempotent, per user)
          → typed Payload (synced_today, last_data_date, daily[], recent_activities[], planned_runs[])
      → analysis.run_analysis(session, payload, user_id=…, api_key=creds.anthropic_key, …)
          → dedup cache check (hash of payload+date+question+model) — early return on hit
          → Sonnet (/report, morning) or Opus (/deep); AnalystError → user-visible message
          → ReportLog row written (user_id, tokens, cost, ok/error)
  → reply / JSON response
```

The aggregation in `app/garmin/service.py` is the cost-control layer — raw Garmin
responses are collapsed to ~12 fields/day and never sent to the LLM.

## Web endpoints

- `GET/POST /login`, `GET /logout`, `GET/POST /register` — cookie-session auth +
  self-registration (new users await admin approval before they can log in).
- `GET /health` — liveness (public, no auth).
- `GET /status` — the logged-in user's Garmin auth, DB stats, last morning report, cost.
- `GET /report.json` — daily report (Sonnet). Login; current user.
- `GET /deep?q=...` — deep analysis (Opus). Login; current user.
- `GET /history?days=N` — HRV/sleep/stress/body-battery trend from the DB. Login; current user.
- `GET/POST /plan` — training-plan setup form (no active plan) / plan view; `POST /plan/archive`
  (archive active), `GET /plan/archive` (list archived), `GET /plan/{id}` (read-only view of
  a past plan). Login; current user.
- `GET /settings` — manage own Garmin/Claude/Telegram creds (encrypted on save).
- `GET /admin/users` — list/create users (admin only).
- `GET /ui` + `GET /ui/{table}` + `/ui/{table}/{id}` — raw DB browser (whitelisted
  tables: users, daily_metrics, activities, report_logs, bot_state). **Admin only.**
  Templates in `app/templates/`.

Auth: a signed cookie session set at `/login` (no token headers). `current_user`
gates user endpoints; `require_admin` gates `/ui` and `/admin/users`. `WEB_TOKEN` is
legacy and no longer used by these routes.

## Database

- **Stack**: SQLAlchemy 2.0 async + Alembic. SQLite (`aiosqlite`) by default for
  zero-config on a Raspberry Pi; switch to Postgres (`asyncpg`) by setting
  `DATABASE_URL` only — no code changes.
- **Models**: `DailyMetric` (unique `date`, + `extra` JSON of unmodeled scalars),
  `ActivityRecord` (unique `activity_id`,
  `exercises` JSON + `series` JSON — per-point pace/HR for runs + `analysis` text —
  Claude's `/activity` writeup), `ReportLog` (cost/metrics + `question`/`report_text`),
  `BotState` (key/value), `TrainingPlan` (goal/params/intake/summary, one active per
  user) + `PlannedWorkout` (dated session: type/dist/description/status).
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

**HRV is the primary recovery signal** — `hrv_status = BALANCED` means recovered; a drop is
the main stress indicator. (The dedicated resting-HR endpoint 403s via garth, but RHR comes
free inside the sleep DTO — stored in `extra.resting_hr`; see below.)

**`DailyMetric.extra` (JSON)** — everything we fetch but don't model as a typed column,
kept as a compact scalar dict (no per-minute arrays). Built by `service._daily_extra` from
the sleep DTO (RHR, overnight HRV, body-battery change, skin-temp deviation, SpO2,
respiration, restless moments, sleep need/feedback), the HRV summary (weekly avg, 5-min
high, baseline band, feedback) and **Training Readiness** — the one extra fetch
(`client.fetch_training_readiness`, `/metrics-service/metrics/trainingreadiness/{date}`):
`readiness_score`/`level`/`feedback`, `recovery_time_h`, `acute_load`, and the ACWR
(acute:chronic load) `acwr_pct`/`acwr_feedback`. `_daily_extra_metrics` adds the rest from
four more endpoints (all keyed by the **displayName**, not the email — the earlier 403 was a
wrong-identifier bug): **user summary** (`fetch_user_summary` — steps, distance, calories,
moderate/vigorous intensity minutes, floors, min HR, body-battery high/low), **VO2max**
(`fetch_vo2max`), **race-time predictions** (`fetch_race_predictions` — 5K/10K/half/marathon
seconds) and **endurance score** (`fetch_endurance`). Persisted (in `_DAILY_FIELDS`) and
served from the day cache. Used by the reports: not yet; used by plan generation: **yes** —
`run_plan_generation` pulls the latest day's `fitness` (race predictions + VO2max +
endurance via `repository.get_latest_daily_extra`) into the `SYSTEM_PLAN` context so targets
and paces are calibrated to current fitness. A bulk historical backfill from the Garmin GDPR
export is a later step.

**Sync awareness**: `synced_today` / `has_data` / `last_data_date` distinguish "watch
hasn't synced" from "bad recovery." The morning job runs ~10s after startup, then every
20 min; the Europe/Warsaw window (07–12) and once-a-day guard live inside `morning_job`,
which logs its decision. The once-a-day guard persists in `bot_state`.

**Models**: `/report` + morning use `claude-sonnet-4-6`; `/deep` uses `claude-opus-4-8`.
Every call is logged to `ReportLog` (tokens, cost, ok/error).

**`/ask <question>`**: cheap follow-up Q&A (Sonnet) grounded in the last `ASK_DEFAULT_N`
(3) **daily** reports' text — no Garmin fetch, no payload. `run_ask` reads
`repository.get_recent_reports` (filtered to `kind="report"`, so `/deep` and prior
`/ask` answers don't pollute the daily context), **plus** `get_recent_asks` — this
user's `/ask` exchanges (question + answer) from the last `ASK_CONTEXT_MIN` (5) minutes,
so a follow-up can build on the previous one. Both go to `analyze_with_stats`' sibling
`ask_with_stats` (separate `SYSTEM_ASK` prompt; the recent thread arrives as `recent_qa`
and is part of the dedup-cache key), which logs a `ReportLog` row with `kind="ask"`.
Bot-only — no web endpoint.

**Stored question**: `ReportLog.question` records the asked prompt — for `/ask` (the
question), `/deep` (the user's question) and morning (its fixed prompt); `/report` leaves
it null (default daily prompt). Visible in the `/me` and `/ui` browsers, and what
`get_recent_asks` reads back for the conversation thread.

**Day-over-day continuity**: `run_analysis` (report/morning, not `/deep`) feeds the
**previous day's** daily report as `previous_report` context via
`repository.get_last_report` — which excludes today's reports and `/deep`+`/ask`. Excluding
today keeps the dedup-cache key stable across repeated same-day `/report` presses (so a
second press is a `CLAUDE CACHE HIT`, not a paid re-run).

**Exercise names**: `fetch_exercise_summary` reads Garmin's specific `name` code, maps it
to Ukrainian via `app/garmin/exercise_names.py` at return time (cache stays language-
neutral). Unknown codes are logged once (`EXERCISE unmapped: <CODE>`). Warm-up jog filtered.

**Run pace/HR series**: for running activities, `fetch_activity_series` pulls Garmin's
`/details` metrics, resolves the speed/HR/distance columns by descriptor key (indices
vary), converts m/s→min/km, downsamples to ~150 points, and stores them on
`ActivityRecord.series`. The `/ui` and `/me` browsers show a **minimal column set** on the
activities list (`admin.INDEX_COLS`) and render `series` as **pace + HR sparklines** on the
per-row detail page (`admin._run_series`/`_run_charts`) — the detail routes stay pure DB
reads (no Garmin call). Non-run activities have `series = null`. The detail charts also
carry per-point data (`s.pts`: x-fraction + raw value + distance) and a small inline
vanilla-JS hover handler in `detail.html` that shows the value (pace as m:ss, HR in уд)
and distance on mousemove — progressive enhancement; the SVG still renders without JS.

**`/activity` analysis**: `/activities` lists this user's last 5 activities (DB read, no
Garmin call) keyed by the short DB `id`; `/activity <id>` analyzes one. `run_activity_analysis`
builds a compact payload (`activity_payload`: summary + `_segments` — the run's series
collapsed to ~6 pace/HR segments so the LLM sees pacing and HR drift), calls Sonnet with
`SYSTEM_ACTIVITY`, **stores the text on `ActivityRecord.analysis`** (shown as a block on the
web detail page) and logs a `ReportLog` (kind="activity"). Shares the dedup cache
(`_activity_cache_key`). Works for any type; runs additionally get the segment detail.

**Training plans**: a user picks a goal + intake on the **web form** (`/plan`); we *prescribe*
a dated program (distinct from the Garmin-Calendar `planned_runs` we merely read). This is
the one place we need **structured LLM output**: `SYSTEM_PLAN` returns JSON validated by
`GeneratedPlan`/`PlanWorkout` (`_coerce_plan` slices to the outer `{...}`; one retry, else
`AnalystError`). `run_plan_generation` feeds compact context (recent runs + recovery trend),
persists a `TrainingPlan` + `PlannedWorkout` rows via `repository.create_plan` (archiving any
prior active plan), and logs `ReportLog(kind="plan")`. Adjustments are **free-text in the
bot**: `/plan <текст>` → `run_plan_edit` (`SYSTEM_PLAN_EDIT` → `PlanEdit` operations
add/move/modify/skip) returns a *proposed* change; the bot shows it with inline ✅/❌ buttons
(`plan_callback`, pending ops in `context.user_data`) and only `repository.apply_plan_ops`
on confirm. Plain `/plan` shows upcoming workouts. The shared `_complete` helper centralises
the Claude call for both. Recovery-adaptive behaviour (reports reacting to HRV/sleep) is not
wired yet. NB the prompt-for-JSON + Pydantic + one-retry choice avoids SDK tool-use, matching
the rest of the `messages.create` usage.

## Caching layers

- **Claude dedup** (`claude_cache.json`): `analyze()` keys on a hash of the meaningful
  payload (`daily`, `recent_activities`, `planned_runs`) + date + question + model +
  `previous_report`. The volatile `generated` timestamp is deliberately excluded —
  otherwise the key changes every minute and never hits (the main gotcha if you touch the
  key logic). `/ask` keys instead on the recent reports + `recent_qa` thread + question +
  model. 1-week TTL. Hit logs `CLAUDE CACHE HIT`.
- **Garmin disk cache** (`garmin_cache.json`): immutable ID-keyed assets only —
  `exercise:v2:<id>` (365d), `workout:v2:<id>` (7d; name + coach description + steps),
  and `series:v1:<id>` (365d; a run's per-point pace/HR from `/details`, downsampled).
Day-level caching moved to the DB.
- **DB day-level cache** (`DailyMetric`): past days served from the DB; today refetched.

## Logging

`app.core.logging.setup()` runs at process start (web factory and `bot.main`). Logs go to
`bot.log` (rotating, 5 × 1 MB) and stdout. Root level is `LOG_LEVEL`; noisy libraries
(httpx, telegram, apscheduler, uvicorn.access) are pinned WARNING. Run with
`LOG_LEVEL=DEBUG` to see skip-reason lines (e.g. `MORNING skip: outside window`).

Web requests are logged by an app-level HTTP middleware in `create_app` (logger `api`,
`GET /plan → 200 42ms`; `/health` skipped) instead of `uvicorn.access`, so they share the
project format. Per-Claude-call cost/tokens are logged (logger `claude`) **and** persisted
to `report_logs` (browsable at `/me/report_logs` and `/ui/report_logs`).

## TODO

- Validate the `gconn` provider against the live Garmin API.
- Deploy to Raspberry Pi 4 (systemd units for `bot.main` and `uvicorn`).
- Optional: dashboard/history visualization, remote MFA re-login flow.
